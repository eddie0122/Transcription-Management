"""In-process event bus broadcast to /ws/events subscribers.

Queues are bounded; slow consumers drop the oldest event rather than
blocking producers (the UI re-fetches job state on reconnect anyway).
"""
import asyncio
import contextlib
from typing import Any, Dict, List


class EventBus:
    def __init__(self) -> None:
        self._subscribers: List[asyncio.Queue] = []
        self._loop = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(q)

    def publish(self, event: Dict[str, Any]) -> None:
        """Safe to call from the event loop thread."""
        for q in list(self._subscribers):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    def publish_threadsafe(self, event: Dict[str, Any]) -> None:
        """Safe to call from worker threads."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.publish, event)


bus = EventBus()


def job_event(job: Dict[str, Any]) -> Dict[str, Any]:
    """Compact job progress payload for the UI."""
    return {
        "type": "job.updated",
        "job": {
            "id": job["id"],
            "kind": job["kind"],
            "display_name": job["display_name"],
            "status": job["status"],
            "stage": job["stage"],
            "progress": job["progress"],
            "progress_label": job["progress_label"],
            "error": job["error"],
            "expires_at": job.get("expires_at"),
            "artifacts": job.get("artifacts", []),
        },
    }
