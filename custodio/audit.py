# Copyright (c) 2026 Leonardo Boquillon
# SPDX-License-Identifier: MIT
"""Audit log of what Custodio anonymized on each request.

This is the "see what was anonymized and what wasn't" surface. Each request
produces one :class:`AuditEvent`. Events are kept in a store (in-memory ring
buffer by default, or Redis when ``CUSTODIO_REDIS_URL`` is set) and pushed to
connected dashboards in real time through an in-process :class:`EventBus`.

Two backends, one interface:

* :class:`MemoryAuditStore` — bounded ``deque``; zero dependencies; per-process.
* :class:`RedisAuditStore`  — persists events in Redis and fans out live updates
  over Redis pub/sub, so multiple proxy workers/instances share one audit view.

Both publish every add/update to the shared :class:`EventBus`; the SSE endpoint
(`GET /custodio/stream`) turns that into a live feed for the dashboard.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import logging
from collections import deque
from dataclasses import asdict, dataclass, field
from threading import Lock

from .pii import EntityHit, PossibleMiss

logger = logging.getLogger("custodio")


@dataclass
class AuditEvent:
    id: str
    ts: float
    model: str
    endpoint: str
    stream: bool
    # what WAS anonymized
    entities: list[dict] = field(default_factory=list)
    entity_count: int = 0
    # what we're UNSURE about (low-confidence, not anonymized)
    possible_misses: list[dict] = field(default_factory=list)
    # sizes / timing / status
    chars_in: int = 0
    chars_out: int = 0
    status: int | None = None
    latency_ms: float | None = None
    # what came back and got restored
    response_placeholders: list[str] = field(default_factory=list)
    # exact text that left the machine (truncated), for eyeballing leaks
    anonymized_preview: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> dict:
        """Compact form for list views."""
        by_type: dict[str, int] = {}
        for e in self.entities:
            by_type[e["entity_type"]] = by_type.get(e["entity_type"], 0) + 1
        return {
            "id": self.id,
            "ts": self.ts,
            "model": self.model,
            "endpoint": self.endpoint,
            "stream": self.stream,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "entity_count": self.entity_count,
            "entities_by_type": by_type,
            "possible_miss_count": len(self.possible_misses),
        }


def entities_to_dicts(hits: list[EntityHit]) -> list[dict]:
    return [asdict(h) for h in hits]


def misses_to_dicts(misses: list[PossibleMiss]) -> list[dict]:
    return [asdict(m) for m in misses]


def _summary_of(event: dict) -> dict:
    """Compute a list-view summary from a full event dict."""
    by_type: dict[str, int] = {}
    for e in event.get("entities", []):
        t = e.get("entity_type", "?")
        by_type[t] = by_type.get(t, 0) + 1
    return {
        "id": event.get("id"),
        "ts": event.get("ts"),
        "model": event.get("model"),
        "endpoint": event.get("endpoint"),
        "stream": event.get("stream"),
        "status": event.get("status"),
        "latency_ms": event.get("latency_ms"),
        "entity_count": event.get("entity_count", 0),
        "entities_by_type": by_type,
        "possible_miss_count": len(event.get("possible_misses", [])),
    }


def _stats_of(events: list[dict]) -> dict:
    total_entities = sum(e.get("entity_count", 0) for e in events)
    by_type: dict[str, int] = {}
    for e in events:
        for ent in e.get("entities", []):
            t = ent.get("entity_type", "?")
            by_type[t] = by_type.get(t, 0) + 1
    return {
        "requests": len(events),
        "entities_anonymized": total_entities,
        "possible_misses": sum(len(e.get("possible_misses", [])) for e in events),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
    }


# --------------------------------------------------------------------------- #
# live push
# --------------------------------------------------------------------------- #
class EventBus:
    """In-process async fan-out of audit updates to SSE subscribers.

    ``publish`` is non-blocking and safe to call from the request handler; a
    slow/backed-up subscriber drops messages rather than stalling the proxy.
    """

    def __init__(self, queue_size: int = 256):
        self._subscribers: set[asyncio.Queue] = set()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, message: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Drop for this slow consumer; it will re-sync on reconnect.
                pass


# --------------------------------------------------------------------------- #
# store interface + backends
# --------------------------------------------------------------------------- #
class AuditStore:
    """Async audit store interface. Backends persist and broadcast events."""

    backend: str = "memory"

    async def start(self) -> None:  # optional lifecycle hooks
        pass

    async def aclose(self) -> None:
        pass

    async def add(self, event: AuditEvent) -> None:
        raise NotImplementedError

    async def update(self, event: AuditEvent) -> None:
        raise NotImplementedError

    async def list(self, limit: int = 100) -> builtins.list[dict]:
        raise NotImplementedError

    async def get(self, event_id: str) -> dict | None:
        raise NotImplementedError

    async def stats(self) -> dict:
        raise NotImplementedError


class MemoryAuditStore(AuditStore):
    """Bounded in-memory ring buffer. Default, zero-dependency backend."""

    backend = "memory"

    def __init__(
        self,
        capacity: int = 500,
        jsonl_path: str | None = None,
        bus: EventBus | None = None,
    ):
        self._events: deque[AuditEvent] = deque(maxlen=capacity)
        self._by_id: dict[str, AuditEvent] = {}
        self._lock = Lock()
        self._jsonl_path = jsonl_path
        self._bus = bus

    async def add(self, event: AuditEvent) -> None:
        with self._lock:
            if len(self._events) == self._events.maxlen and self._events:
                evicted = self._events[0]
                self._by_id.pop(evicted.id, None)
            self._events.append(event)
            self._by_id[event.id] = event
        self._publish(event)

    async def update(self, event: AuditEvent) -> None:
        # Same object is already in the buffer (mutated in place); persist the
        # finalized event to the durable log exactly once, then broadcast.
        self._append_jsonl(event)
        self._publish(event)

    def _publish(self, event: AuditEvent) -> None:
        if self._bus is not None:
            self._bus.publish({"type": "event", "event": event.to_dict()})

    def _append_jsonl(self, event: AuditEvent) -> None:
        if not self._jsonl_path:
            return
        try:
            with open(self._jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("audit jsonl write failed: %s", exc)

    async def list(self, limit: int = 100) -> builtins.list[dict]:
        with self._lock:
            events = list(self._events)[-limit:][::-1]
        return [e.summary() for e in events]

    async def get(self, event_id: str) -> dict | None:
        with self._lock:
            event = self._by_id.get(event_id)
        return event.to_dict() if event else None

    async def stats(self) -> dict:
        with self._lock:
            events = [e.to_dict() for e in self._events]
        return _stats_of(events)


class RedisAuditStore(AuditStore):
    """Persist events in Redis and fan out live updates over Redis pub/sub.

    Layout (``prefix`` defaults to ``custodio``):

    * ``prefix:event:<id>``  — JSON of one event (optional TTL)
    * ``prefix:index``       — list of ids, newest first, trimmed to capacity
    * ``prefix:stream``      — pub/sub channel; each message is an event id

    A background task subscribes to ``prefix:stream`` and forwards the freshest
    event to the in-process :class:`EventBus`, so every instance's SSE clients
    see updates from every instance.
    """

    backend = "redis"

    def __init__(
        self,
        client,
        capacity: int = 500,
        prefix: str = "custodio",
        ttl_seconds: int = 0,
        jsonl_path: str | None = None,
        bus: EventBus | None = None,
    ):
        self._r = client
        self._capacity = max(1, capacity)
        self._prefix = prefix
        self._ttl = ttl_seconds if ttl_seconds > 0 else None
        self._jsonl_path = jsonl_path
        self._bus = bus
        self._channel = f"{prefix}:stream"
        self._index_key = f"{prefix}:index"
        self._sub_task: asyncio.Task | None = None
        self._closing = False

    def _event_key(self, event_id: str) -> str:
        return f"{self._prefix}:event:{event_id}"

    async def start(self) -> None:
        self._sub_task = asyncio.create_task(self._subscribe_loop())

    async def aclose(self) -> None:
        self._closing = True
        if self._sub_task is not None:
            self._sub_task.cancel()
            try:
                await self._sub_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            await self._r.aclose()
        except Exception:  # noqa: BLE001
            pass

    async def add(self, event: AuditEvent) -> None:
        data = json.dumps(event.to_dict(), ensure_ascii=False)
        key = self._event_key(event.id)
        try:
            pipe = self._r.pipeline()
            pipe.set(key, data, ex=self._ttl)
            pipe.lpush(self._index_key, event.id)
            # Trim overflow ids and delete their event keys to bound memory.
            pipe.lrange(self._index_key, self._capacity, -1)
            pipe.ltrim(self._index_key, 0, self._capacity - 1)
            results = await pipe.execute()
            overflow = results[2] or []
            if overflow:
                await self._r.delete(*[self._event_key(i) for i in overflow])
            await self._r.publish(self._channel, event.id)
        except Exception as exc:  # noqa: BLE001 - audit is best-effort
            logger.warning("redis audit add failed: %s", exc)

    async def update(self, event: AuditEvent) -> None:
        data = json.dumps(event.to_dict(), ensure_ascii=False)
        try:
            await self._r.set(self._event_key(event.id), data, ex=self._ttl, xx=True)
            await self._r.publish(self._channel, event.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis audit update failed: %s", exc)
        self._append_jsonl(event)

    def _append_jsonl(self, event: AuditEvent) -> None:
        if not self._jsonl_path:
            return
        try:
            with open(self._jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("audit jsonl write failed: %s", exc)

    async def _load(self, event_id: str) -> dict | None:
        raw = await self._r.get(self._event_key(event_id))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    async def list(self, limit: int = 100) -> builtins.list[dict]:
        try:
            ids = await self._r.lrange(self._index_key, 0, max(0, limit - 1))
            if not ids:
                return []
            raws = await self._r.mget([self._event_key(i) for i in ids])
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis audit list failed: %s", exc)
            return []
        out: list[dict] = []
        for raw in raws:
            if not raw:
                continue
            try:
                out.append(_summary_of(json.loads(raw)))
            except (ValueError, TypeError):
                continue
        return out

    async def get(self, event_id: str) -> dict | None:
        try:
            return await self._load(event_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis audit get failed: %s", exc)
            return None

    async def stats(self) -> dict:
        try:
            ids = await self._r.lrange(self._index_key, 0, self._capacity - 1)
            if not ids:
                return _stats_of([])
            raws = await self._r.mget([self._event_key(i) for i in ids])
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis audit stats failed: %s", exc)
            return _stats_of([])
        events = []
        for raw in raws:
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except (ValueError, TypeError):
                continue
        return _stats_of(events)

    async def _subscribe_loop(self) -> None:
        """Bridge Redis pub/sub -> in-process bus, with reconnect."""
        backoff = 0.5
        while not self._closing:
            pubsub = None
            try:
                pubsub = self._r.pubsub()
                await pubsub.subscribe(self._channel)
                backoff = 0.5
                async for msg in pubsub.listen():
                    if self._closing:
                        break
                    if msg.get("type") != "message":
                        continue
                    event_id = msg.get("data")
                    if isinstance(event_id, bytes):
                        event_id = event_id.decode("utf-8", "replace")
                    event = await self._load(event_id)
                    if event is not None and self._bus is not None:
                        self._bus.publish({"type": "event", "event": event})
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                if self._closing:
                    break
                logger.warning("redis pub/sub bridge error: %s (reconnecting)", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:  # noqa: BLE001
                        pass


def _redact_url(url: str) -> str:
    """Drop any credentials from a connection URL before logging it."""
    try:
        from urllib.parse import urlsplit, urlunsplit

        p = urlsplit(url)
        netloc = p.hostname or ""
        if p.port:
            netloc = f"{netloc}:{p.port}"
        return urlunsplit((p.scheme, netloc, p.path, "", ""))
    except Exception:  # noqa: BLE001
        return "redis"


def create_audit_store(settings, bus: EventBus | None = None) -> AuditStore:
    """Build the configured audit store, falling back to memory on any problem."""
    if settings.redis_url:
        try:
            import redis.asyncio as redis_asyncio

            client = redis_asyncio.from_url(
                settings.redis_url, decode_responses=True
            )
            logger.info("Audit store: Redis (%s)", _redact_url(settings.redis_url))
            return RedisAuditStore(
                client,
                capacity=settings.audit_capacity,
                prefix=settings.redis_prefix,
                ttl_seconds=settings.redis_ttl_seconds,
                jsonl_path=settings.audit_jsonl_path,
                bus=bus,
            )
        except ModuleNotFoundError:
            logger.error(
                "CUSTODIO_REDIS_URL is set but the 'redis' package is not "
                "installed; install custodio[redis]. Falling back to memory."
            )
        except Exception as exc:  # noqa: BLE001 - message kept credential-free
            logger.error(
                "Redis audit store init failed (%s); using memory.",
                type(exc).__name__,
            )
    logger.info("Audit store: in-memory (capacity=%s)", settings.audit_capacity)
    return MemoryAuditStore(
        capacity=settings.audit_capacity,
        jsonl_path=settings.audit_jsonl_path,
        bus=bus,
    )
