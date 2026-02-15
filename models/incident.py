"""In-memory incident store and data model."""

import uuid
from datetime import datetime, timezone
from typing import Optional


class Incident:
    """Represents a single incident through its lifecycle."""

    STATUSES = [
        "detected",
        "investigating",
        "fix_proposed",
        "simulating",
        "verified",
        "failed",
        "pr_created",
        "resolved",
    ]

    def __init__(
        self,
        error_type: str,
        traceback: str,
        logs: list[str],
        source_file: str,
        repo_url: str,
        branch: str = "main",
        source_code: str = "",
    ):
        self.id = str(uuid.uuid4())[:8]
        self.error_type = error_type
        self.traceback = traceback
        self.logs = logs
        self.source_file = source_file
        self.repo_url = repo_url
        self.branch = branch
        self.status = "detected"
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.updated_at = self.created_at

        # Source code sent inline from the monitored app
        self.original_code: Optional[str] = source_code or None

        # Populated by the investigation agent
        self.root_cause: Optional[str] = None
        self.fix_description: Optional[str] = None
        self.fixed_code: Optional[str] = None
        self.affected_file: Optional[str] = None

        # Populated by the sandbox
        self.sandbox_reproduced: Optional[bool] = None
        self.sandbox_fix_verified: Optional[bool] = None
        self.sandbox_output: Optional[str] = None

        # Populated by GitHub integration
        self.pr_url: Optional[str] = None
        self.pr_branch: Optional[str] = None

    def update_status(self, new_status: str):
        if new_status not in self.STATUSES:
            raise ValueError(f"Invalid status: {new_status}")
        self.status = new_status
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "error_type": self.error_type,
            "traceback": self.traceback,
            "logs": self.logs,
            "source_file": self.source_file,
            "repo_url": self.repo_url,
            "branch": self.branch,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "root_cause": self.root_cause,
            "fix_description": self.fix_description,
            "original_code": self.original_code,
            "fixed_code": self.fixed_code,
            "affected_file": self.affected_file,
            "sandbox_reproduced": self.sandbox_reproduced,
            "sandbox_fix_verified": self.sandbox_fix_verified,
            "sandbox_output": self.sandbox_output,
            "pr_url": self.pr_url,
            "pr_branch": self.pr_branch,
        }

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


class IncidentStore:
    """In-memory incident storage for hackathon. Swap for DB later."""

    def __init__(self):
        self._incidents: dict[str, Incident] = {}

    def create(self, **kwargs) -> Incident:
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
