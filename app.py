"""AutoDuty Backend — FastAPI server with SSE streaming."""

import asyncio
import json
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from agents.investigator import investigate_incident
from config import Config
from integrations.github_client import create_fix_pr
from models.incident import store
from sandbox.modal_runner import run_sandbox_verification
from streaming.event_bus import event_bus
from utils.logger import get_logger

log = get_logger("autoduty")

app = FastAPI(title="AutoDuty", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Runtime settings (mutable via /settings endpoint)
runtime_settings = {
    "ai_model": Config.AI_MODEL,
}


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------
class IncidentCreateRequest(BaseModel):
    error_type: str
    traceback: str
    source_file: str
    repo_url: str
    branch: str = "main"
    source_code: str = ""
    logs: list[str] = []


class SettingsUpdateRequest(BaseModel):
    ai_model: Optional[str] = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "ai_model": runtime_settings["ai_model"]}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@app.get("/settings")
async def get_settings():
    return runtime_settings


@app.post("/settings")
async def update_settings(data: SettingsUpdateRequest):
    if data.ai_model is not None:
        runtime_settings["ai_model"] = data.ai_model
        log.info("AI model switched to %s", data.ai_model)
    return runtime_settings


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------
@app.post("/incident", status_code=201)
async def create_incident(data: IncidentCreateRequest):
    """Receive an error report from the monitored application."""
    incident = store.create(
        error_type=data.error_type,
        traceback=data.traceback,
        logs=data.logs,
        source_file=data.source_file,
        repo_url=data.repo_url,
        branch=data.branch,
        source_code=data.source_code,
    )
    log.info("Incident %s created — %s in %s", incident.id, incident.error_type, incident.source_file)

    # Kick off the async investigation pipeline
    asyncio.create_task(_run_pipeline(incident.id))

    return incident.model_dump()


@app.get("/incidents")
async def list_incidents():
    """Return all incidents (summary view)."""
    return store.list_all()


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str):
    """Return full detail for a single incident."""
    incident = store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident.model_dump()


@app.post("/incidents/{incident_id}/approve")
async def approve_incident(incident_id: str):
    """Create a GitHub PR for a verified fix."""
    incident = store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    if incident.status not in ("verified", "fix_proposed"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot approve incident in status: {incident.status}",
        )

    try:
        pr_url = create_fix_pr(incident)
        incident.pr_url = pr_url
        incident.update_status("pr_created")
        log.info("Incident %s — PR created: %s", incident.id, pr_url)
        return {"pr_url": pr_url, "incident": incident.model_dump()}
    except Exception as e:
        log.error("Incident %s — PR creation failed: %s", incident.id, e)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# SSE Streaming
# ---------------------------------------------------------------------------
@app.get("/incidents/{incident_id}/stream")
async def stream_incident(incident_id: str):
    """Server-Sent Events endpoint for real-time agent updates.

    The frontend connects here immediately after creating an incident to
    receive live agent thoughts, tool calls, sandbox output, and diffs.
    """
    incident = store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    async def event_generator():
        # Send current status as first event
        yield {
            "event": "status",
            "data": json.dumps({
                "type": "status_change",
                "status": incident.status,
                "message": f"Current status: {incident.status}",
            }),
        }

        # Stream events as they come in
        async for event in event_bus.subscribe(incident_id):
            yield {
                "event": event.get("type", "message"),
                "data": json.dumps(event),
            }

        # Send a final "done" event
        yield {
            "event": "done",
            "data": json.dumps({"type": "done", "status": incident.status}),
        }

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# Pipeline (runs as async task)
# ---------------------------------------------------------------------------
def _format_sandbox_feedback(incident) -> str:
    """Build a retry context string from the sandbox terminal log and results."""
    lines = []
    lines.append(f"### Sandbox Results (attempt failed)")
    lines.append(f"- Bug reproduced: {incident.sandbox_reproduced}")
    lines.append(f"- Fix verified: {incident.sandbox_fix_verified}")
    lines.append("")

    if incident.sandbox_terminal_log:
        lines.append("### Sandbox Terminal Output")
        lines.append("```")
        for entry in incident.sandbox_terminal_log[-100:]:  # Last 100 lines
            prefix = "[stderr] " if entry.stream == "stderr" else ""
            lines.append(f"[{entry.label}] {prefix}{entry.data}")
        lines.append("```")

    if incident.sandbox_output:
        lines.append("")
        lines.append("### Raw Output")
        lines.append("```")
        lines.append(incident.sandbox_output[:2000])
        lines.append("```")

    return "\n".join(lines)


async def _run_pipeline(incident_id: str):
    """Full remediation pipeline with retry loop.

    Flow: Investigate -> Sandbox -> if failed, feed logs back to agent and retry.
    Controlled by Config.MAX_RETRIES (from env var MAX_RETRIES, default 3).
    """
    incident = store.get(incident_id)
    if not incident:
        return

    model = runtime_settings["ai_model"]
    max_attempts = Config.MAX_RETRIES
    repo = None  # Will be set by the first investigate_incident call
    retry_context = None

    for attempt in range(1, max_attempts + 1):
        is_retry = attempt > 1

        # ---- Phase 1: Investigation ----
        try:
            incident.update_status("investigating")
            await event_bus.publish(incident_id, {
                "type": "status_change",
                "status": "investigating",
                "message": (
                    f"Retry {attempt}/{max_attempts} — re-investigating with sandbox feedback..."
                    if is_retry
                    else f"Starting investigation with {model} (attempt {attempt}/{max_attempts})..."
                ),
            })
            log.info(
                "Incident %s — investigating with %s (attempt %d/%d)",
                incident.id, model, attempt, max_attempts,
            )

            repo = await investigate_incident(
                incident,
                event_bus,
                model=model,
                retry_context=retry_context,
                repo=repo,
            )

            incident.update_status("fix_proposed")
            await event_bus.publish(incident_id, {
                "type": "status_change",
                "status": "fix_proposed",
                "message": f"Fix proposed: {incident.fix_description}",
            })
            log.info("Incident %s — fix proposed: %s", incident.id, incident.fix_description)

        except Exception as e:
            log.error("Incident %s — investigation failed (attempt %d): %s", incident.id, attempt, e)
            incident.update_status("failed")
            await event_bus.publish(incident_id, {
                "type": "status_change",
                "status": "failed",
                "message": f"Investigation failed: {e}",
            })
            # Clean up repo on fatal failure
            if repo:
                repo.cleanup()
            await event_bus.close_stream(incident_id)
            return

        # ---- Phase 2: Sandbox verification ----
        try:
            incident.update_status("simulating")
            await event_bus.publish(incident_id, {
                "type": "status_change",
                "status": "simulating",
                "message": f"Running sandbox verification (attempt {attempt}/{max_attempts})...",
            })
            log.info("Incident %s — running sandbox verification (attempt %d)", incident.id, attempt)

            result = await run_sandbox_verification(incident, event_bus)
            incident.sandbox_reproduced = result.get("reproduced", False)
            incident.sandbox_fix_verified = result.get("fix_verified", False)
            incident.sandbox_output = result.get("output", "")

            if incident.sandbox_fix_verified:
                incident.update_status("verified")
                await event_bus.publish(incident_id, {
                    "type": "status_change",
                    "status": "verified",
                    "message": "Fix VERIFIED in sandbox!",
                })
                log.info("Incident %s — fix VERIFIED in sandbox (attempt %d)", incident.id, attempt)
                break  # Success — exit retry loop

            # Not verified — prepare feedback for the next attempt
            log.warning(
                "Incident %s — sandbox verification failed (attempt %d/%d)",
                incident.id, attempt, max_attempts,
            )

            if attempt < max_attempts:
                retry_context = _format_sandbox_feedback(incident)
                await event_bus.publish(incident_id, {
                    "type": "status_change",
                    "status": "investigating",
                    "message": (
                        f"Fix did not pass sandbox (attempt {attempt}/{max_attempts}). "
                        "Feeding logs back to agent for retry..."
                    ),
                })
                # Clear terminal log for next attempt so it's clean
                incident.sandbox_terminal_log = []
            else:
                # Final attempt failed — mark as failed
                incident.update_status("failed")
                await event_bus.publish(incident_id, {
                    "type": "status_change",
                    "status": "failed",
                    "message": (
                        f"Sandbox verification failed after {max_attempts} attempts. "
                        "Unable to produce a working fix."
                    ),
                })

        except Exception as e:
            log.error("Incident %s — sandbox failed (attempt %d): %s", incident.id, attempt, e)
            incident.sandbox_output = f"Sandbox error: {e}"

            if attempt < max_attempts:
                retry_context = f"### Sandbox Error\n\nSandbox crashed: {e}\n\nPlease review your fix and try again."
                await event_bus.publish(incident_id, {
                    "type": "status_change",
                    "status": "investigating",
                    "message": f"Sandbox error on attempt {attempt}. Retrying...",
                })
            else:
                incident.update_status("failed")
                await event_bus.publish(incident_id, {
                    "type": "status_change",
                    "status": "failed",
                    "message": f"Sandbox error: {e}. Unable to verify fix.",
                })

    # Clean up the cloned repo
    if repo:
        repo.cleanup()

    # Signal stream completion
    await event_bus.close_stream(incident_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    log.info("Starting AutoDuty backend on port %s (model: %s)", Config.PORT, Config.AI_MODEL)
    uvicorn.run(app, host="0.0.0.0", port=Config.PORT)
