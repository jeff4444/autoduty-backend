"""Modal Sandbox runner — reproduces bugs and verifies fixes in isolated containers.

Streams terminal output line-by-line to the SSE event bus so the frontend
can render a real-time terminal replay.
"""

import asyncio
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


def _build_test_script(code: str, label: str) -> str:
    """Build a self-contained TypeScript test script that exercises a route handler."""
    return f"""
// Minimal NextResponse polyfill for sandbox
class NextResponse extends Response {{
    static json(data, init) {{
        return new Response(JSON.stringify(data), {{
            ...init,
            headers: {{ 'Content-Type': 'application/json', ...(init?.headers || {{}}) }},
        }});
    }}
}}

// --- Source code under test (with imports replaced) ---
{code}

// --- Test harness ---
async function runTest() {{
    try {{
        if (typeof GET === 'function') {{
            const req = new Request('http://localhost/test');
            const res = await GET(req);
            console.log('[{label}] Status:', res.status);
            if (res.status >= 500) {{
                console.error('[{label}] Server error detected');
                process.exit(1);
            }}
        }} else if (typeof POST === 'function') {{
            const req = new Request('http://localhost/test', {{ method: 'POST' }});
            const res = await POST(req);
            console.log('[{label}] Status:', res.status);
            if (res.status >= 500) {{
                console.error('[{label}] Server error detected');
                process.exit(1);
            }}
        }} else {{
            console.log('[{label}] Module loaded successfully');
        }}
        console.log('[{label}] PASS');
    }} catch (err) {{
        console.error('[{label}] FAIL:', err.message);
        process.exit(1);
    }}
}}
runTest();
"""


def _strip_imports(code: str) -> str:
    """Strip Next.js-specific imports and the withAutoduty wrapper that won't work in sandbox."""
    lines = code.split("\n")
    cleaned = []
    for line in lines:
        # Skip Next.js and autoduty imports
        if 'from "next/' in line or "from 'next/" in line:
            continue
        if "from \"@/lib/error-reporter\"" in line or "from '@/lib/error-reporter'" in line:
            continue
        # Skip the export const GET = withAutoduty(...) line
        if line.strip().startswith("export const GET = withAutoduty") or line.strip().startswith(
            "export const POST = withAutoduty"
        ):
            continue
        cleaned.append(line)

    # Make the handler function exported directly
    result = "\n".join(cleaned)
    result = result.replace("async function handler(", "async function GET(")
    return result


def _escape_for_bash(s: str) -> str:
    """Escape a string for embedding inside single quotes in bash."""
    return s.replace("'", "'\\''")


async def _run_in_sandbox_streaming(
    script: str,
    label: str,
    incident: Incident,
    event_bus: EventBus,
) -> bool:
    """Run a TypeScript script in a Modal Sandbox, streaming output line-by-line.

    Returns True if the script exits with code 0.
    """
    await event_bus.publish(incident.id, {
        "type": "sandbox_status",
        "label": label,
        "message": f"Starting sandbox: {label}...",
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

    return exit_code == 0


async def run_sandbox_verification(
    incident: Incident,
    event_bus: EventBus,
) -> dict:
    """Run the buggy code and the fixed code inside a Modal sandbox.

    Streams terminal output to the event bus in real-time.

    Returns:
        dict with keys: reproduced (bool), fix_verified (bool), output (str)
    """
    # Determine source: use the first file edit or fall back to original/fixed code
    original_code = incident.original_code or ""
    fixed_code = incident.fixed_code or ""

    # If we have file_edits, use the first one as the primary code to test
    if incident.file_edits:
        primary_edit = incident.file_edits[0]
        original_code = primary_edit.original_content
        fixed_code = primary_edit.new_content

    if not original_code or not fixed_code:
        log.warning("Incident %s missing source/fix code, skipping sandbox", incident.id)
        await event_bus.publish(incident.id, {
            "type": "sandbox_status",
            "label": "skip",
            "message": "Skipped: missing original or fixed code",
        })
        return {
            "reproduced": False,
            "fix_verified": False,
            "output": "Skipped: missing original or fixed code",
        }

    log.info("Starting sandbox verification for incident %s", incident.id)

    try:
        # Strip Next.js imports that won't resolve in sandbox
        original_clean = _strip_imports(original_code)
        fixed_clean = _strip_imports(fixed_code)

        # Step 1: Test original (buggy) code — should FAIL
        await event_bus.publish(incident.id, {
            "type": "sandbox_phase",
            "phase": "reproduce",
            "message": "Running original (buggy) code — expecting failure...",
        })
        original_script = _build_test_script(original_clean, "ORIGINAL-BUGGY")
        original_passed = await _run_in_sandbox_streaming(
            original_script, "ORIGINAL-BUGGY", incident, event_bus
        )

        # Step 2: Test fixed code — should PASS
        await event_bus.publish(incident.id, {
            "type": "sandbox_phase",
            "phase": "verify",
            "message": "Running fixed code — expecting success...",
        })
        fixed_script = _build_test_script(fixed_clean, "FIXED")
        fix_passed = await _run_in_sandbox_streaming(
            fixed_script, "FIXED", incident, event_bus
        )

        result = {
            "reproduced": not original_passed,
            "fix_verified": fix_passed,
            "output": "\n".join(
                f"[{e.label}] [{e.stream}] {e.data}" for e in incident.sandbox_terminal_log
            ),
        }

        await event_bus.publish(incident.id, {
            "type": "sandbox_complete",
            "reproduced": result["reproduced"],
            "fix_verified": result["fix_verified"],
        })

        log.info(
            "Sandbox result for %s: reproduced=%s, verified=%s",
            incident.id,
            result["reproduced"],
            result["fix_verified"],
        )
        return result

    except Exception as e:
        log.error("Sandbox execution failed for %s: %s", incident.id, e)
        await event_bus.publish(incident.id, {
            "type": "sandbox_error",
            "message": f"Sandbox error: {str(e)}",
        })
        return {
            "reproduced": False,
            "fix_verified": False,
            "output": f"Sandbox error: {str(e)}",
        }
