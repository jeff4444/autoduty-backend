"""Per-incident async event bus for real-time SSE streaming."""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from utils.logger import get_logger

log = get_logger("event_bus")


class EventBus:
    """Central hub that manages per-incident event queues.

    Publishers call `publish(incident_id, event)` to push events.
    Subscribers call `subscribe(incident_id)` to get an async generator
    that yields events as they arrive (used by the SSE endpoint).
    """

    def __init__(self) -> None:
        # incident_id -> list of subscriber queues
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    async def publish(self, incident_id: str, event: dict[str, Any]) -> None:
        """Publish an event to all subscribers for a given incident."""
        # Add a timestamp if not already present
        if "timestamp" not in event:
            event["timestamp"] = datetime.now(timezone.utc).isoformat()

        queues = self._subscribers.get(incident_id, [])
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("Event queue full for incident %s, dropping event", incident_id)

    async def subscribe(self, incident_id: str) -> AsyncGenerator[dict[str, Any], None]:
        """Subscribe to events for an incident. Yields events as they arrive."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

        if incident_id not in self._subscribers:
            self._subscribers[incident_id] = []
        self._subscribers[incident_id].append(queue)

        log.info("New SSE subscriber for incident %s (total: %d)", incident_id, len(self._subscribers[incident_id]))

        try:
            while True:
                event = await queue.get()
                # A None sentinel signals the stream is done
                if event is None:
                    break
                yield event
        finally:
            # Clean up on disconnect
            self._subscribers[incident_id].remove(queue)
            if not self._subscribers[incident_id]:
                del self._subscribers[incident_id]
            log.info("SSE subscriber disconnected for incident %s", incident_id)

    async def close_stream(self, incident_id: str) -> None:
        """Signal all subscribers that the stream for this incident is finished."""
        queues = self._subscribers.get(incident_id, [])
        for q in queues:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def format_sse(self, event: dict[str, Any]) -> str:
        """Format an event dict as an SSE data line."""
        return json.dumps(event)


# Global singleton
event_bus = EventBus()
