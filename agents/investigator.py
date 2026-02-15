"""Investigation orchestrator — runs the Pydantic AI agent with streaming.

This module is the bridge between the FastAPI pipeline and the Pydantic AI agent.
It clones the repo, creates agent dependencies, runs the agent via iter(),
publishes events to the SSE bus, and collects file edits + diffs.
"""

import json
from datetime import datetime, timezone

from pydantic_ai import CallToolsNode, ModelRequestNode, UserPromptNode
from pydantic_ai.messages import TextPart, ToolCallPart
from pydantic_graph import End

from agents.agent import investigation_agent
from agents.repo_context import RepoContext
from agents.tools import AgentDeps
from config import Config
from models.incident import AgentEvent, Incident
from streaming.event_bus import EventBus
from utils.logger import get_logger

log = get_logger("investigator")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def investigate_incident(
    incident: Incident,
    event_bus: EventBus,
    model: str | None = None,
    retry_context: str | None = None,
    repo: RepoContext | None = None,
) -> RepoContext:
    """Run the full investigation: clone repo, run agent with streaming, collect diffs.

    Mutates the incident in place with root_cause, fix_description, file_edits, etc.

    Args:
        incident: The incident to investigate.
        event_bus: The event bus for streaming SSE events.
        model: Pydantic AI model string override (e.g. "anthropic:claude-sonnet-4-20250514").
        retry_context: Optional context from a previous failed sandbox run.
            When provided, the agent receives this as additional info to guide its fix.
        repo: An existing RepoContext from a previous attempt. If provided, cloning is
            skipped and the repo is reused (with edit tracking reset).

    Returns:
        The RepoContext used, so the caller can reuse it for retries or clean it up.
    """
    model = model or Config.AI_MODEL

    # 1. Clone the repository (or reuse existing clone on retry)
    if repo is None:
        await event_bus.publish(incident.id, {
            "type": "status_change",
            "status": "cloning",
            "message": f"Cloning {incident.repo_url} (branch: {incident.branch})...",
        })

        repo = RepoContext(
            repo_url=incident.repo_url,
            branch=incident.branch,
        )

        try:
            await repo.clone()
        except Exception as e:
            log.error("Failed to clone repo for incident %s: %s", incident.id, e)
            await event_bus.publish(incident.id, {
                "type": "error",
                "message": f"Failed to clone repository: {e}",
            })
            raise
    else:
        # Retry: reset edit tracking so new diffs are relative to current state
        repo.reset_edit_tracking()

    await event_bus.publish(incident.id, {
        "type": "status_change",
        "status": "investigating",
        "message": "Repository cloned. Starting investigation..." if retry_context is None
        else "Retrying investigation with sandbox feedback...",
    })

    # 2. Build the user prompt with error context
    user_prompt = _build_prompt(incident, retry_context=retry_context)

    # 3. Set up agent dependencies (includes incident for sandbox tool access)
    deps = AgentDeps(
        repo=repo,
        event_bus=event_bus,
        incident_id=incident.id,
        incident=incident,
        sandbox_runs_remaining=Config.MAX_SANDBOX_RUNS,
    )

    # 4. Run the agent using iter() to walk through each node
    try:
        async with investigation_agent.iter(
            user_prompt,
            deps=deps,
            model=model,
        ) as agent_run:
            async for node in agent_run:
                await _process_node(node, incident, event_bus)

        # 5. Extract results from the completed run
        result = agent_run.result
        if result is not None:
            output = result.output
            incident.root_cause = output.root_cause
            incident.fix_description = output.fix_description
            incident.affected_file = (
                output.affected_files[0] if output.affected_files else incident.source_file
            )
            # Populate sandbox verification fields from agent's own assessment
            incident.sandbox_reproduced = output.reproduction_confirmed
            incident.sandbox_fix_verified = output.fix_verified
        else:
            log.warning("Agent run for %s completed without a result", incident.id)
            incident.root_cause = "Investigation completed but no structured result produced"
            incident.fix_description = "Check agent events for details"
            incident.sandbox_reproduced = False
            incident.sandbox_fix_verified = False

        # 6. Compute diffs from all file edits
        incident.file_edits = repo.get_file_edits()

        # For backward compat: if there's a single edit, also populate fixed_code
        if len(incident.file_edits) == 1:
            incident.fixed_code = incident.file_edits[0].new_content
            if not incident.original_code:
                incident.original_code = incident.file_edits[0].original_content

        await event_bus.publish(incident.id, {
            "type": "investigation_complete",
            "root_cause": incident.root_cause,
            "fix_description": incident.fix_description,
            "affected_files": [e.file_path for e in incident.file_edits],
            "num_file_edits": len(incident.file_edits),
            "diffs": [
                {"file": edit.file_path, "unified_diff": edit.unified_diff}
                for edit in incident.file_edits
            ],
        })

        log.info(
            "Investigation complete for %s — root cause: %s, %d file(s) edited",
            incident.id,
            incident.root_cause,
            len(incident.file_edits),
        )

        return repo

    except Exception as e:
        log.error("Agent run failed for incident %s: %s", incident.id, e)
        await event_bus.publish(incident.id, {
            "type": "error",
            "message": f"Investigation failed: {e}",
        })
        raise


# ---------------------------------------------------------------------------
# Node processing — extract events from each step of the agent graph
# ---------------------------------------------------------------------------

async def _process_node(node, incident: Incident, event_bus: EventBus) -> None:
    """Process a single node from the agent iteration and publish events."""

    if isinstance(node, UserPromptNode):
        # Initial prompt node — nothing interesting to stream
        return

    if isinstance(node, ModelRequestNode):
        # The agent is sending a request to the model
        event = AgentEvent(
            timestamp=_now(),
            type="model_request",
            data={"message": "Sending request to model..."},
        )
        incident.agent_events.append(event)
        await event_bus.publish(incident.id, {
            "type": "model_request",
            "message": "Sending request to model...",
        })
        return

    if isinstance(node, CallToolsNode):
        # The model returned a response — extract text and tool calls
        response = node.model_response
        for part in response.parts:
            if isinstance(part, TextPart) and part.content:
                event = AgentEvent(
                    timestamp=_now(),
                    type="agent_thought",
                    data={"content": part.content},
                )
                incident.agent_events.append(event)
                # Publish flat so the frontend gets {type, content, timestamp}
                await event_bus.publish(incident.id, {
                    "type": "agent_thought",
                    "content": part.content,
                })

            elif isinstance(part, ToolCallPart):
                # Log the tool call (the actual tool execution + result is
                # published by the tool functions themselves via the event bus)
                args = part.args
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        args = {"raw": args[:200]}

                # Truncate large string values for display
                display_args = {}
                if isinstance(args, dict):
                    for k, v in args.items():
                        if isinstance(v, str) and len(v) > 120:
                            display_args[k] = v[:120] + "..."
                        else:
                            display_args[k] = v
                else:
                    display_args = {"raw": str(args)[:200]}

                event = AgentEvent(
                    timestamp=_now(),
                    type="tool_call",
                    data={
                        "tool": part.tool_name,
                        "args": display_args,
                        "tool_call_id": part.tool_call_id,
                    },
                )
                incident.agent_events.append(event)
                # Publish as a flat event (matching the shape from tools.py)
                await event_bus.publish(incident.id, {
                    "type": "tool_call",
                    "tool": part.tool_name,
                    "args": display_args,
                    "tool_call_id": part.tool_call_id,
                })
        return

    if isinstance(node, End):
        event = AgentEvent(
            timestamp=_now(),
            type="agent_complete",
            data={"message": "Agent finished investigation."},
        )
        incident.agent_events.append(event)
        await event_bus.publish(incident.id, {
            "type": "agent_complete",
            "message": "Agent finished investigation.",
        })
        return


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(incident: Incident, retry_context: str | None = None) -> str:
    """Build the user prompt from incident context."""
    logs_section = ""
    if incident.logs:
        logs_text = "\n".join(incident.logs[-50:])
        logs_section = f"""
### Recent Logs
```
{logs_text}
```
"""

    source_section = ""
    if incident.original_code:
        source_section = f"""
### Source Code ({incident.source_file})
```
{incident.original_code}
```
"""

    retry_section = ""
    if retry_context:
        retry_section = f"""

## RETRY — Previous Attempt Failed

Your previous fix did NOT pass sandbox verification. Below is the feedback from the \
sandbox run. Analyze what went wrong, then apply corrected fixes using the tools.

{retry_context}
"""

    return f"""## Error Report

**Error Type:** {incident.error_type}
**Source File:** {incident.source_file}
**Repository:** {incident.repo_url}
**Branch:** {incident.branch}

### Traceback
```
{incident.traceback}
```
{logs_section}
{source_section}
{retry_section}
Investigate this error. Start by reading the source file mentioned in the traceback, \
then explore related files as needed. Diagnose the root cause and apply fixes using \
the available tools. You may edit multiple files if the fix requires it.
"""
