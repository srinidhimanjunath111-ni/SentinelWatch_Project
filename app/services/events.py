"""In-process pub/sub bridging worker threads to Server-Sent Events clients.

Workers call ``publish`` only after their database commit, so a client that reacts to an
event can always read the row it refers to. Each client owns a bounded queue: when a slow
client falls behind it loses its oldest events, and nothing else slows down or grows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from app.timeutil import iso_z, utcnow

logger = logging.getLogger(__name__)

CLIENT_QUEUE_SIZE = 256
REPLAY_BUFFER_SIZE = 256
HEARTBEAT_SECONDS = 15.0


@dataclass(frozen=True)
class Event:
    id: int
    name: str
    data: dict[str, Any]

    def encode(self) -> str:
        return f"id: {self.id}\nevent: {self.name}\ndata: {json.dumps(self.data, default=str)}\n\n"


class _Subscriber:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
        self.dropped = 0

    def offer(self, event: Event | None) -> None:
        """Runs on the subscriber's event loop."""

        if self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
        self.queue.put_nowait(event)


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_id = 1
        self._subscribers: set[_Subscriber] = set()
        self._listeners: list[Callable[[Event], None]] = []
        self._recent: deque[Event] = deque(maxlen=REPLAY_BUFFER_SIZE)
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, name: str, data: dict[str, Any]) -> Event:
        """Thread-safe and non-blocking. A closed client loop is treated as a disconnect."""

        with self._lock:
            event = Event(self._next_id, name, data)
            self._next_id += 1
            self._recent.append(event)
            subscribers = list(self._subscribers)
            listeners = list(self._listeners)
        for subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.offer, event)
            except RuntimeError:
                self._discard(subscriber)
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # A broken observer must never disturb a worker.
                logger.exception("Event listener failed for %s", name)
        return event

    def add_listener(self, listener: Callable[[Event], None]) -> Callable[[], None]:
        """Register a synchronous observer called on the publishing thread."""

        with self._lock:
            self._listeners.append(listener)

        def remove() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return remove

    def _discard(self, subscriber: _Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def subscribe(self, last_event_id: int | None = None) -> _Subscriber:
        subscriber = _Subscriber(asyncio.get_running_loop())
        with self._lock:
            self._subscribers.add(subscriber)
            if last_event_id is not None:
                # A reconnecting browser sends Last-Event-ID; it receives what it missed
                # as far as the bounded buffer still holds it.
                for event in self._recent:
                    if event.id > last_event_id:
                        subscriber.offer(event)
        return subscriber

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        self._discard(subscriber)

    def close(self) -> None:
        """Ask every open stream to end so that server shutdown cannot hang on them."""

        with self._lock:
            self._closed = True
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.offer, None)
            except RuntimeError:
                self._discard(subscriber)

    def reopen(self) -> None:
        with self._lock:
            self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed


bus = EventBus()


def publish(name: str, data: dict[str, Any]) -> None:
    bus.publish(name, data)


async def stream_events(
    is_disconnected: Callable[[], Any],
    heartbeat: Callable[[], dict[str, Any]],
    *,
    last_event_id: int | None = None,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """Yield SSE frames until the client leaves or the server shuts down."""

    subscriber = bus.subscribe(last_event_id)
    # Poll for disconnect more often than the heartbeat so an idle stream is released quickly.
    poll_seconds = min(heartbeat_seconds, 1.0)
    idle = 0.0
    try:
        yield "retry: 3000\n\n"
        yield _heartbeat_frame(heartbeat)
        while not bus.closed:
            if await is_disconnected():
                return
            try:
                event = await asyncio.wait_for(subscriber.queue.get(), timeout=poll_seconds)
            except TimeoutError:
                idle += poll_seconds
                if idle >= heartbeat_seconds:
                    idle = 0.0
                    yield _heartbeat_frame(heartbeat)
                continue
            if event is None:
                return
            idle = 0.0
            yield event.encode()
    finally:
        bus.unsubscribe(subscriber)


def _heartbeat_frame(heartbeat: Callable[[], dict[str, Any]]) -> str:
    try:
        counters = heartbeat()
    except Exception:  # A database blip should not end a healthy stream.
        logger.exception("Heartbeat counters unavailable")
        counters = {}
    payload = {"at": iso_z(utcnow()), "active_runs": 0, "queued_runs": 0, **counters}
    return f"event: heartbeat\ndata: {json.dumps(payload)}\n\n"


def install_shutdown_hook() -> None:
    """End open streams when uvicorn receives a signal.

    Uvicorn waits for in-flight responses before running lifespan shutdown, so an SSE
    stream that only ends in lifespan shutdown would hold the server open indefinitely.
    """

    try:
        from uvicorn.server import Server
    except ImportError:
        return
    if getattr(Server.handle_exit, "_sentinelwatch", False):
        return
    original = Server.handle_exit

    def handle_exit(self, sig, frame):
        bus.close()
        return original(self, sig, frame)

    handle_exit._sentinelwatch = True  # type: ignore[attr-defined]
    Server.handle_exit = handle_exit  # type: ignore[method-assign]
