"""Pydantic AI tool functions for codebase interaction.

These tools give the agent the ability to explore and modify the cloned repository,
similar to how a developer uses an IDE.
"""

from dataclasses import dataclass

from pydantic_ai import RunContext

from agents.repo_context import RepoContext
from streaming.event_bus import EventBus
from utils.logger import get_logger

log = get_logger("tools")


@dataclass
class AgentDeps:
    """Dependencies injected into every agent tool call."""

    repo: RepoContext
    event_bus: EventBus
    incident_id: str


# ---------------------------------------------------------------------------
# Tool implementations — registered on the agent in agent.py
# ---------------------------------------------------------------------------

async def read_file(ctx: RunContext[AgentDeps], path: str) -> str:
    """Read the contents of a file in the repository.

    Args:
        path: Relative path from the repository root (e.g. "src/lib/constants.ts").
    """
    deps = ctx.deps
    await deps.event_bus.publish(deps.incident_id, {
        "type": "tool_call",
        "tool": "read_file",
        "args": {"path": path},
    })

    try:
        content = deps.repo.read_file(path)
        # Truncate tool result in event stream for readability
        preview = content[:500] + "..." if len(content) > 500 else content
        await deps.event_bus.publish(deps.incident_id, {
            "type": "tool_result",
            "tool": "read_file",
            "result": f"({len(content)} chars) {preview}",
        })
        return content
    except Exception as e:
        error_msg = str(e)
        await deps.event_bus.publish(deps.incident_id, {
            "type": "tool_result",
            "tool": "read_file",
            "result": f"Error: {error_msg}",
        })
        return f"Error reading file: {error_msg}"


async def write_file(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
    """Write content to a file in the repository. Creates the file if it doesn't exist.

    Args:
        path: Relative path from the repository root.
        content: The full file contents to write.
    """
    deps = ctx.deps
    await deps.event_bus.publish(deps.incident_id, {
        "type": "tool_call",
        "tool": "write_file",
        "args": {"path": path, "content_length": len(content)},
    })

    try:
        result = deps.repo.write_file(path, content)
        await deps.event_bus.publish(deps.incident_id, {
            "type": "tool_result",
            "tool": "write_file",
            "result": result,
        })
        return result
    except Exception as e:
        error_msg = str(e)
        await deps.event_bus.publish(deps.incident_id, {
            "type": "tool_result",
            "tool": "write_file",
            "result": f"Error: {error_msg}",
        })
        return f"Error writing file: {error_msg}"


async def search_and_replace(
    ctx: RunContext[AgentDeps], path: str, old: str, new: str
) -> str:
    """Find and replace the first occurrence of a string in a file.

    Args:
        path: Relative path from the repository root.
        old: The exact string to find (must match exactly, including whitespace).
        new: The replacement string.
    """
    deps = ctx.deps
    await deps.event_bus.publish(deps.incident_id, {
        "type": "tool_call",
        "tool": "search_and_replace",
        "args": {"path": path, "old": old[:120], "new": new[:120]},
    })

    try:
        result = deps.repo.search_and_replace(path, old, new)
        await deps.event_bus.publish(deps.incident_id, {
            "type": "tool_result",
            "tool": "search_and_replace",
            "result": result,
        })
        return result
    except Exception as e:
        error_msg = str(e)
        await deps.event_bus.publish(deps.incident_id, {
            "type": "tool_result",
            "tool": "search_and_replace",
            "result": f"Error: {error_msg}",
        })
        return f"Error in search_and_replace: {error_msg}"


async def grep(ctx: RunContext[AgentDeps], pattern: str, path: str = ".") -> str:
    """Search for a regex pattern across files in the repository.

    Args:
        pattern: A regex pattern to search for.
        path: Relative directory or file path to search in (defaults to repo root).
    """
    deps = ctx.deps
    await deps.event_bus.publish(deps.incident_id, {
        "type": "tool_call",
        "tool": "grep",
        "args": {"pattern": pattern, "path": path},
    })

    try:
        result = deps.repo.grep(pattern, path)
        await deps.event_bus.publish(deps.incident_id, {
            "type": "tool_result",
            "tool": "grep",
            "result": result[:1000] + "..." if len(result) > 1000 else result,
        })
        return result
    except Exception as e:
        error_msg = str(e)
        await deps.event_bus.publish(deps.incident_id, {
            "type": "tool_result",
            "tool": "grep",
            "result": f"Error: {error_msg}",
        })
        return f"Error in grep: {error_msg}"


async def list_directory(ctx: RunContext[AgentDeps], path: str = ".") -> str:
    """List files and directories at the given path in the repository.

    Args:
        path: Relative directory path (defaults to repo root).
    """
    deps = ctx.deps
    await deps.event_bus.publish(deps.incident_id, {
        "type": "tool_call",
        "tool": "list_directory",
        "args": {"path": path},
    })

    try:
        result = deps.repo.list_directory(path)
        await deps.event_bus.publish(deps.incident_id, {
            "type": "tool_result",
            "tool": "list_directory",
            "result": result,
        })
        return result
    except Exception as e:
        error_msg = str(e)
        await deps.event_bus.publish(deps.incident_id, {
            "type": "tool_result",
            "tool": "list_directory",
            "result": f"Error: {error_msg}",
        })
        return f"Error listing directory: {error_msg}"
