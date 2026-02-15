"""Modal Sandbox runner — executes arbitrary scripts in isolated containers.

The agent calls `run_single_sandbox()` directly via the `run_sandbox` tool,
giving it full control over what scripts to run, how to reproduce bugs,
and how to verify fixes.

Streams terminal output line-by-line to the SSE event bus so the frontend
can render a real-time terminal replay.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import modal

from models.incident import Incident, TerminalLogEntry
from streaming.event_bus import EventBus
from utils.logger import get_logger

log = get_logger("sandbox")

# Base image with Node.js for running TypeScript code
sandbox_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("curl")
    .run_commands(
        "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -",
        "apt-get install -y nodejs",
    )
    .run_commands("npm install -g tsx typescript")
)

app = modal.App.lookup("autoduty-sandbox", create_if_missing=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _escape_for_bash(s: str) -> str:
    """Escape a string for embedding inside single quotes in bash."""
    return s.replace("'", "'\\''")


# ---------------------------------------------------------------------------
# Sandbox result
# ---------------------------------------------------------------------------
@dataclass
class SandboxResult:
    """Structured result from a single sandbox execution."""

    stdout: str
    stderr: str
    exit_code: int
    label: str


# ---------------------------------------------------------------------------
# Core sandbox execution
# ---------------------------------------------------------------------------
async def run_single_sandbox(
    script: str,
    label: str,
    incident: Incident,
    event_bus: EventBus,
) -> SandboxResult:
    """Run a TypeScript script in a Modal Sandbox, streaming output line-by-line.

    This is the single entry point used by the agent's `run_sandbox` tool.

    Args:
        script: The full TypeScript source code to execute.
        label: Human-readable label for this run (e.g. "reproduce-bug", "verify-fix").
        incident: The incident to append terminal log entries to.
        event_bus: Event bus for streaming SSE events to the frontend.

    Returns:
        SandboxResult with stdout, stderr, and exit_code.
    """
    await event_bus.publish(incident.id, {
        "type": "sandbox_phase",
        "phase": label,
        "message": f"Running sandbox: {label}...",
    })

    # Run in a thread since Modal's sandbox API is synchronous
    loop = asyncio.get_event_loop()

    def _run_sandbox():
        sb = modal.Sandbox.create(
            "bash",
            "-c",
            f"echo '{_escape_for_bash(script)}' > /tmp/test.ts && tsx /tmp/test.ts",
            image=sandbox_image,
            app=app,
            timeout=60,
        )
        sb.wait()

        stdout = sb.stdout.read()
        stderr = sb.stderr.read()
        exit_code = sb.returncode
        return stdout, stderr, exit_code

    stdout, stderr, exit_code = await loop.run_in_executor(None, _run_sandbox)

    # Stream stdout lines
    if stdout:
        for line in stdout.strip().splitlines():
            entry = TerminalLogEntry(
                timestamp=_now(),
                stream="stdout",
                data=line,
                label=label,
            )
            incident.sandbox_terminal_log.append(entry)
            await event_bus.publish(incident.id, {
                "type": "sandbox_output",
                "label": label,
                "stream": "stdout",
                "data": line,
            })

    # Stream stderr lines
    if stderr:
        for line in stderr.strip().splitlines():
            entry = TerminalLogEntry(
                timestamp=_now(),
                stream="stderr",
                data=line,
                label=label,
            )
            incident.sandbox_terminal_log.append(entry)
            await event_bus.publish(incident.id, {
                "type": "sandbox_output",
                "label": label,
                "stream": "stderr",
                "data": line,
            })

    # Publish exit code
    exit_entry = TerminalLogEntry(
        timestamp=_now(),
        stream="stdout",
        data=f"[{label}] exit_code={exit_code}",
        label=label,
    )
    incident.sandbox_terminal_log.append(exit_entry)
    await event_bus.publish(incident.id, {
        "type": "sandbox_exit",
        "label": label,
        "exit_code": exit_code,
    })

    return SandboxResult(
        stdout=stdout or "",
        stderr=stderr or "",
        exit_code=exit_code,
        label=label,
    )
