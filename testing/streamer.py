"""Per-run event streamer.

Each run gets a Streamer. Test runner emits structured events
(suite_start, test_start, log, test_end, suite_complete) onto an
asyncio.Queue. The SSE endpoint consumes the queue and pushes the
events to the browser. Heartbeats keep the connection alive
through proxies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

# Cap per-run event buffer so a runaway test can't OOM us.
_MAX_BUFFER = 10_000

# How long completed runs stay around for late SSE subscribers.
_RETAIN_SECONDS = 600.0


@dataclass
class _Event:
    seq: int
    ts: float
    payload: dict[str, Any]


@dataclass
class Streamer:
    run_id: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    events: list[_Event] = field(default_factory=list)
    subscribers: list[asyncio.Queue[_Event]] = field(default_factory=list)
    cancelled: bool = False
    summary: dict[str, Any] | None = None

    def emit(self, event_name: str, **payload: Any) -> None:
        """Add an event and fan out to subscribers."""
        if len(self.events) >= _MAX_BUFFER:
            # Drop oldest log events but keep test_start/end/summary
            self.events = [
                e for e in self.events if e.payload.get("event") != "log"
            ][-_MAX_BUFFER // 2:]
        seq = len(self.events)
        ev = _Event(seq=seq, ts=time.time(), payload={"event": event_name, **payload})
        self.events.append(ev)
        for q in list(self.subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                logger.warning("streamer subscriber queue full; dropping events")

    def close(self, summary: dict[str, Any]) -> None:
        self.summary = summary
        self.finished_at = time.time()
        for q in list(self.subscribers):
            try:
                q.put_nowait(_Event(seq=-1, ts=time.time(), payload={"event": "__eof__"}))
            except asyncio.QueueFull:
                pass

    def request_cancel(self) -> None:
        self.cancelled = True

    def subscribe(self) -> asyncio.Queue[_Event]:
        q: asyncio.Queue[_Event] = asyncio.Queue(maxsize=2048)
        # Replay buffered events to the new subscriber so a late tab
        # catches up.
        for ev in self.events:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                break
        if self.finished_at is not None:
            try:
                q.put_nowait(_Event(seq=-1, ts=time.time(), payload={"event": "__eof__"}))
            except asyncio.QueueFull:
                pass
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[_Event]) -> None:
        try:
            self.subscribers.remove(q)
        except ValueError:
            pass


# Active and recently-completed streamers, keyed by run_id.
_STREAMERS: dict[str, Streamer] = {}
_LOCK = asyncio.Lock()


async def new_run() -> Streamer:
    """Allocate a fresh Streamer + run_id."""
    run_id = uuid.uuid4().hex[:12]
    s = Streamer(run_id=run_id)
    async with _LOCK:
        _STREAMERS[run_id] = s
        # Reap old completed runs
        cutoff = time.time() - _RETAIN_SECONDS
        stale = [
            rid
            for rid, st in _STREAMERS.items()
            if st.finished_at is not None and st.finished_at < cutoff
        ]
        for rid in stale:
            del _STREAMERS[rid]
    return s


def get(run_id: str) -> Streamer | None:
    return _STREAMERS.get(run_id)


async def sse_for(run_id: str) -> AsyncIterator[bytes]:
    """Yield SSE-encoded bytes for a run.

    Sends a heartbeat comment every 15s so reverse-proxies and load
    balancers don't drop idle connections.
    """
    s = get(run_id)
    if s is None:
        yield _sse({"event": "error", "message": f"unknown run_id: {run_id}"})
        return

    q = s.subscribe()
    try:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield b": heartbeat\n\n"
                continue
            if ev.payload.get("event") == "__eof__":
                yield _sse({"event": "eof"})
                break
            yield _sse(ev.payload)
    finally:
        s.unsubscribe(q)


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, default=str)}\n\n".encode("utf-8")
