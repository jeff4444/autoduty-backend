"""Incident data model and in-memory store — Pydantic BaseModel edition."""

import uuid
from datetime import datetime, timezone
from typing import Optional, Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------
class FileEdit(BaseModel):
    """A single file edit made by the agent."""

    file_path: str
    original_content: str = ""
    new_content: str = ""
    unified_diff: str = ""  # git-style unified diff


class TerminalLogEntry(BaseModel):
    """One line of sandbox terminal output."""

    timestamp: str  # ISO-8601
    stream: str  # "stdout" | "stderr"
    data: str
    label: str = ""  # e.g. "original-buggy" or "fixed"


class AgentEvent(BaseModel):
    """A single event from the agent run (for replay / audit)."""

    timestamp: str
    type: str  # "thought", "tool_call", "tool_result", "status_change", "error"
    data: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Incident model
# ---------------------------------------------------------------------------
class Incident(BaseModel):
    """Represents a single incident through its lifecycle."""

    model_config = {"arbitrary_types_allowed": True}

    STATUSES: list[str] = Field(
        default=[
            "detected",
            "investigating",
            "fix_proposed",
            "simulating",
            "verified",
            "failed",
            "pr_created",
            "resolved",
        ],
        exclude=True,
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    error_type: str
    traceback: str
    logs: list[str] = []
    source_file: str
    repo_url: str
    branch: str = "main"
    status: str = "detected"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Source code sent inline from the monitored app (kept for backward compat)
    original_code: Optional[str] = None

    # Agent results
    root_cause: Optional[str] = None
    fix_description: Optional[str] = None
    affected_file: Optional[str] = None
    file_edits: list[FileEdit] = []

    # Legacy single-file fields (populated for backward compat from file_edits)
    fixed_code: Optional[str] = None

    # Sandbox
    sandbox_reproduced: Optional[bool] = None
    sandbox_fix_verified: Optional[bool] = None
    sandbox_output: Optional[str] = None
    sandbox_terminal_log: list[TerminalLogEntry] = []

    # Agent activity log (for replay)
    agent_events: list[AgentEvent] = []

    # GitHub PR
    pr_url: Optional[str] = None
    pr_branch: Optional[str] = None

    def update_status(self, new_status: str) -> None:
        if new_status not in self.STATUSES:
            raise ValueError(f"Invalid status: {new_status}")
        self.status = new_status
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "error_type": self.error_type,
            "source_file": self.source_file,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "root_cause": self.root_cause,
        }


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------
class IncidentStore:
    """In-memory incident storage. Swap for a DB later."""

    def __init__(self) -> None:
        self._incidents: dict[str, Incident] = {}

    def create(self, **kwargs: Any) -> Incident:
        incident = Incident(**kwargs)
        self._incidents[incident.id] = incident
        return incident

    def get(self, incident_id: str) -> Optional[Incident]:
        return self._incidents.get(incident_id)

    def list_all(self) -> list[dict]:
        return [
            inc.to_summary()
            for inc in sorted(
                self._incidents.values(),
                key=lambda i: i.created_at,
                reverse=True,
            )
        ]


# Global singleton
store = IncidentStore()
