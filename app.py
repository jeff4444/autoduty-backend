"""AutoDuty Backend — Flask API server."""

import threading
from flask import Flask, request, jsonify
from flask_cors import CORS

from config import Config
from models.incident import store
from agents.investigator import investigate_incident
from sandbox.modal_runner import run_sandbox_verification
from integrations.github_client import create_fix_pr
from utils.logger import get_logger

log = get_logger("autoduty")

app = Flask(__name__)
CORS(app)

# Runtime settings (mutable via /settings endpoint)
runtime_settings = {
    "llm_provider": Config.LLM_PROVIDER,
}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "provider": runtime_settings["llm_provider"]})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@app.route("/settings", methods=["GET"])
def get_settings():
    return jsonify(runtime_settings)


@app.route("/settings", methods=["POST"])
def update_settings():
    data = request.get_json(force=True)
    if "llm_provider" in data:
        provider = data["llm_provider"]
        if provider not in ("gemini", "anthropic", "openai"):
            return jsonify({"error": f"Invalid provider: {provider}"}), 400
        runtime_settings["llm_provider"] = provider
        log.info("LLM provider switched to %s", provider)
    return jsonify(runtime_settings)


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------
@app.route("/incident", methods=["POST"])
def create_incident():
    """Receive an error report from the monitored application."""
    data = request.get_json(force=True)

    required = ["error_type", "traceback", "source_file", "repo_url"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"error": f"Missing fields: {missing}"}), 400

    incident = store.create(
        error_type=data["error_type"],
        traceback=data["traceback"],
        logs=data.get("logs", []),
        source_file=data["source_file"],
        repo_url=data["repo_url"],
        branch=data.get("branch", "main"),
        source_code=data.get("source_code", ""),
    )
    log.info("Incident %s created — %s in %s", incident.id, incident.error_type, incident.source_file)

    # Kick off the async investigation pipeline
    threading.Thread(
        target=_run_pipeline,
        args=(incident.id,),
        daemon=True,
    ).start()

    return jsonify(incident.to_dict()), 201


@app.route("/incidents", methods=["GET"])
def list_incidents():
    """Return all incidents (summary view)."""
    return jsonify(store.list_all())


@app.route("/incidents/<incident_id>", methods=["GET"])
def get_incident(incident_id: str):
    """Return full detail for a single incident."""
    incident = store.get(incident_id)
    if not incident:
        return jsonify({"error": "Incident not found"}), 404
    return jsonify(incident.to_dict())


@app.route("/incidents/<incident_id>/approve", methods=["POST"])
def approve_incident(incident_id: str):
    """Create a GitHub PR for a verified fix."""
    incident = store.get(incident_id)
    if not incident:
        return jsonify({"error": "Incident not found"}), 404
    if incident.status not in ("verified", "fix_proposed"):
        return jsonify({"error": f"Cannot approve incident in status: {incident.status}"}), 400

    try:
        pr_url = create_fix_pr(incident)
        incident.pr_url = pr_url
        incident.update_status("pr_created")
        log.info("Incident %s — PR created: %s", incident.id, pr_url)
        return jsonify({"pr_url": pr_url, "incident": incident.to_dict()})
    except Exception as e:
        log.error("Incident %s — PR creation failed: %s", incident.id, e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Pipeline (runs in background thread)
# ---------------------------------------------------------------------------
def _run_pipeline(incident_id: str):
    """Full remediation pipeline: Investigate → Sandbox → (wait for approval)."""
    incident = store.get(incident_id)
    if not incident:
        return

    provider = runtime_settings["llm_provider"]

    # Phase 1: Investigation
    try:
        incident.update_status("investigating")
        log.info("Incident %s — investigating with %s", incident.id, provider)
        investigate_incident(incident, provider=provider)
        incident.update_status("fix_proposed")
        log.info("Incident %s — fix proposed: %s", incident.id, incident.fix_description)
    except Exception as e:
        log.error("Incident %s — investigation failed: %s", incident.id, e)
        incident.update_status("failed")
        return

    # Phase 2: Sandbox verification
    try:
        incident.update_status("simulating")
        log.info("Incident %s — running sandbox verification", incident.id)
        result = run_sandbox_verification(incident)
        incident.sandbox_reproduced = result.get("reproduced", False)
        incident.sandbox_fix_verified = result.get("fix_verified", False)
        incident.sandbox_output = result.get("output", "")

        if incident.sandbox_fix_verified:
            incident.update_status("verified")
            log.info("Incident %s — fix VERIFIED in sandbox", incident.id)
        else:
            incident.update_status("fix_proposed")
            log.warning("Incident %s — sandbox verification failed, fix still proposed", incident.id)
    except Exception as e:
        log.error("Incident %s — sandbox failed: %s", incident.id, e)
        # Don't fail the whole pipeline — the fix is still proposed
        incident.sandbox_output = f"Sandbox error: {e}"
        incident.update_status("fix_proposed")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("Starting AutoDuty backend on port %s (provider: %s)", Config.FLASK_PORT, runtime_settings["llm_provider"])
    app.run(host="0.0.0.0", port=Config.FLASK_PORT, debug=Config.FLASK_DEBUG)
