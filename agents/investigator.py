"""Investigation agent — uses the LLM to diagnose root cause and generate a fix."""

import requests
from agents.llm_provider import generate_json
from utils.logger import get_logger

log = get_logger("investigator")

SYSTEM_PROMPT = """You are an expert Site Reliability Engineer and software debugger.
You will be given:
1. An error traceback from a Next.js / TypeScript application
2. The source code of the file that caused the error
3. Recent log lines

Your job is to:
- Identify the exact root cause of the error
- Write the corrected version of the ENTIRE source file
- The fixed code must keep ALL imports, ALL exports, and the same structure — only fix the bug

IMPORTANT: Preserve the existing imports and the `withAutoduty` wrapper exactly as-is.
Only change the lines that have the actual bug.

Respond with ONLY valid JSON (no markdown fencing, no extra text) in this exact schema:
{
    "root_cause": "A clear 1-2 sentence explanation of what went wrong",
    "fix_description": "A clear 1-2 sentence explanation of the fix",
    "affected_file": "The file path that needs to be changed",
    "fixed_code": "The COMPLETE corrected source file contents"
}
"""


def _fetch_file_from_github(repo_url: str, branch: str, file_path: str) -> str:
    """Fetch a file's raw content from GitHub (fallback if source_code not provided inline)."""
    try:
        raw_url = repo_url.replace("github.com", "raw.githubusercontent.com")
        url = f"{raw_url}/{branch}/{file_path}"
        log.info("Fetching source file from GitHub: %s", url)
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.text
        log.warning("Could not fetch file (status %s)", resp.status_code)
    except Exception as e:
        log.warning("GitHub fetch failed: %s", e)
    return ""


def investigate_incident(incident, provider: str = None):
    """Run investigation on an incident, populating its root_cause and fixed_code fields.

    Args:
        incident: An Incident model instance (mutated in place).
        provider: LLM provider name override.
    """
    # Use inline source code if available, otherwise try GitHub
    source_code = incident.original_code or ""
    if not source_code:
        source_code = _fetch_file_from_github(
            incident.repo_url, incident.branch, incident.source_file
        )
        incident.original_code = source_code

    user_prompt = f"""## Error Report

**Error Type:** {incident.error_type}
**Source File:** {incident.source_file}

### Traceback
```
{incident.traceback}
```

### Recent Logs
```
{chr(10).join(incident.logs[-50:])}
```

### Source Code ({incident.source_file})
```typescript
{source_code}
```

Analyze the error and provide the fix as JSON. The fixed_code MUST be the COMPLETE file with ALL original imports and exports preserved — only fix the buggy lines.
"""

    log.info("Sending investigation prompt to %s (source code length: %d chars)", provider or "default", len(source_code))
    result = generate_json(SYSTEM_PROMPT, user_prompt, provider=provider)

    incident.root_cause = result.get("root_cause", "Unknown")
    incident.fix_description = result.get("fix_description", "No description")
    incident.affected_file = result.get("affected_file", incident.source_file)
    incident.fixed_code = result.get("fixed_code", "")

    log.info("Investigation complete — root cause: %s", incident.root_cause)
