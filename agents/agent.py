"""Pydantic AI Agent definition for incident investigation.

This module defines the core agent that investigates production errors by
exploring the codebase, identifying root causes, and applying fixes across
multiple files.
"""

from pydantic import BaseModel
from pydantic_ai import Agent

from agents.tools import (
    AgentDeps,
    grep,
    list_directory,
    read_file,
    search_and_replace,
    write_file,
)

# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------

class InvestigationResult(BaseModel):
    """The final structured output the agent must produce."""

    root_cause: str
    fix_description: str
    affected_files: list[str]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an expert Site Reliability Engineer and software debugger working inside \
an automated incident remediation system called AutoDuty.

You will be given an error report from a production application, including:
- The error type and traceback
- Recent log lines
- The file path where the error originated

You have access to the full repository via the tools below. Use them to:
1. **Explore** the codebase — read the error file, grep for related symbols, \
read imported modules, check config files, understand the data flow.
2. **Diagnose** the root cause — think step by step. Explain your reasoning \
as you go.
3. **Fix** the bug — use `search_and_replace` for surgical edits or \
`write_file` for larger rewrites. You may edit MULTIPLE files if needed.

Guidelines:
- Start by reading the file referenced in the traceback.
- Use `grep` to find usages of relevant symbols, constants, or types.
- Use `list_directory` to understand project structure when needed.
- Prefer `search_and_replace` over `write_file` for small, targeted fixes \
(it produces cleaner diffs).
- Preserve existing imports, exports, and code structure — only change what \
is necessary to fix the bug.
- When you are done making all edits, return your final analysis as the \
structured output.
- Be thorough but efficient. Real incidents need fast resolution.
"""


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

investigation_agent = Agent(
    deps_type=AgentDeps,
    output_type=InvestigationResult,
    system_prompt=SYSTEM_PROMPT,
    tools=[read_file, write_file, search_and_replace, grep, list_directory],
    retries=2,
)
