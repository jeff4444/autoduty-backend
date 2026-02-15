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
async def _run_pipeline(incident_id: str):
    """Full remediation pipeline — the agent handles reproduction and verification internally.

    The agent has access to a `run_sandbox` tool that lets it execute arbitrary
    TypeScript scripts in an isolated Modal sandbox. It uses this to reproduce
    the bug, apply fixes, and verify them — all within a single agent run.
    """
    incident = store.get(incident_id)
    if not incident:
        return

    model = runtime_settings["ai_model"]
    repo = None

    # ---- Investigation (agent explores, reproduces, fixes, and verifies) ----
    try:
        incident.update_status("investigating")
        await event_bus.publish(incident_id, {
            "type": "status_change",
            "status": "investigating",
            "message": f"Starting investigation with {model}...",
        })
        log.info("Incident %s — investigating with %s", incident.id, model)

        repo = await investigate_incident(
            incident,
            event_bus,
            model=model,
        )

        # ---- Determine final status based on agent's sandbox results ----
        if incident.sandbox_fix_verified:
            incident.update_status("verified")
            await event_bus.publish(incident_id, {
                "type": "status_change",
                "status": "verified",
                "message": "Fix VERIFIED by agent in sandbox!",
            })
            log.info("Incident %s — fix VERIFIED in sandbox", incident.id)
        else:
            incident.update_status("fix_proposed")
            await event_bus.publish(incident_id, {
                "type": "status_change",
                "status": "fix_proposed",
                "message": f"Fix proposed (unverified): {incident.fix_description}",
            })
            log.info("Incident %s — fix proposed (unverified): %s", incident.id, incident.fix_description)

    except Exception as e:
        log.error("Incident %s — investigation failed: %s", incident.id, e)
        incident.update_status("failed")
        await event_bus.publish(incident_id, {
            "type": "status_change",
            "status": "failed",
            "message": f"Investigation failed: {e}",
        })

    finally:
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
