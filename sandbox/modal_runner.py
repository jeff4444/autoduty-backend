"""Modal Sandbox runner — spins up an isolated environment to reproduce bugs and verify fixes.

Uses Modal's Sandbox API which can be called directly from any Python process
without needing to deploy a Modal app first.
"""

import modal
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

// --- Source code under test (with import replaced) ---
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
        if line.strip().startswith("export const GET = withAutoduty") or line.strip().startswith("export const POST = withAutoduty"):
            continue
        cleaned.append(line)

    # Make the handler function exported directly
    result = "\n".join(cleaned)
    result = result.replace("async function handler(", "async function GET(")
    return result


def run_sandbox_verification(incident) -> dict:
    """Run the buggy code and the fixed code inside a Modal sandbox.

    Returns:
        dict with keys: reproduced (bool), fix_verified (bool), output (str)
    """
    if not incident.original_code or not incident.fixed_code:
        log.warning("Incident %s missing source/fix code, skipping sandbox", incident.id)
        return {
            "reproduced": False,
            "fix_verified": False,
            "output": "Skipped: missing original or fixed code",
        }

    log.info("Starting sandbox verification for incident %s", incident.id)
    output_lines = []

    try:
        # Strip Next.js imports that won't resolve in sandbox
        original_clean = _strip_imports(incident.original_code)
        fixed_clean = _strip_imports(incident.fixed_code)

        # Step 1: Test original (buggy) code — should FAIL
        original_script = _build_test_script(original_clean, "ORIGINAL-BUGGY")
        original_passed = _run_in_sandbox(original_script, "original", output_lines)

        # Step 2: Test fixed code — should PASS
        fixed_script = _build_test_script(fixed_clean, "FIXED")
        fix_passed = _run_in_sandbox(fixed_script, "fixed", output_lines)

        result = {
            "reproduced": not original_passed,
            "fix_verified": fix_passed,
            "output": "\n".join(output_lines),
        }
        log.info("Sandbox result for %s: reproduced=%s, verified=%s", incident.id, result["reproduced"], result["fix_verified"])
        return result

    except Exception as e:
        log.error("Sandbox execution failed for %s: %s", incident.id, e)
        return {
            "reproduced": False,
            "fix_verified": False,
            "output": f"Sandbox error: {str(e)}",
        }


def _run_in_sandbox(script: str, label: str, output_lines: list) -> bool:
    """Run a TypeScript script in a Modal Sandbox. Returns True if it exits 0."""
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

    combined = f"[{label}] exit={exit_code}\n"
    if stdout:
        combined += f"stdout: {stdout.strip()}\n"
    if stderr:
        combined += f"stderr: {stderr.strip()}\n"
    output_lines.append(combined)

    return exit_code == 0


def _escape_for_bash(s: str) -> str:
    """Escape a string for embedding inside single quotes in bash."""
    return s.replace("'", "'\\''")
