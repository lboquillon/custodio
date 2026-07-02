"""Tests for the pluggable audit store (memory + Redis) and the live EventBus.

The Redis tests use fakeredis (an in-process, protocol-accurate fake), so they
need no running Redis server.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from custodio.audit import (  # noqa: E402
    AuditEvent,
    EventBus,
    MemoryAuditStore,
    RedisAuditStore,
)


def _event(i: int, entities=None, misses=None) -> AuditEvent:
    return AuditEvent(
        id=f"evt{i}",
        ts=1000.0 + i,
        model="claude-x",
        endpoint="/v1/messages",
        stream=False,
        entities=entities or [{"entity_type": "PERSON", "placeholder": "<PERSON_0>",
                               "original_masked": "J***", "score": 0.9, "original": None}],
        entity_count=len(entities) if entities is not None else 1,
        possible_misses=misses or [],
    )


# ------------------------------- memory ---------------------------------- #
async def test_memory_add_get_list_stats_and_bus():
    bus = EventBus()
    q = bus.subscribe()
    store = MemoryAuditStore(capacity=10, bus=bus)

    ev = _event(1)
    await store.add(ev)

    # live push on add
    msg = q.get_nowait()
    assert msg["type"] == "event" and msg["event"]["id"] == "evt1"

    got = await store.get("evt1")
    assert got["model"] == "claude-x"
    lst = await store.list()
    assert lst[0]["id"] == "evt1" and lst[0]["entities_by_type"] == {"PERSON": 1}
    stats = await store.stats()
    assert stats["requests"] == 1 and stats["entities_anonymized"] == 1

    # update finalizes + re-broadcasts
    ev.status = 200
    await store.update(ev)
    msg2 = q.get_nowait()
    assert msg2["event"]["status"] == 200
    assert (await store.get("evt1"))["status"] == 200


async def test_memory_capacity_eviction():
    store = MemoryAuditStore(capacity=3)
    for i in range(5):
        await store.add(_event(i))
    lst = await store.list(limit=100)
    ids = [e["id"] for e in lst]
    assert ids == ["evt4", "evt3", "evt2"]  # newest first, oldest two evicted
    assert await store.get("evt0") is None
    assert (await store.stats())["requests"] == 3


def test_event_bus_drops_when_full_instead_of_blocking():
    async def run():
        bus = EventBus(queue_size=1)
        q = bus.subscribe()
        bus.publish({"type": "event", "event": {"id": "a"}})
        bus.publish({"type": "event", "event": {"id": "b"}})  # dropped, no raise
        assert q.qsize() == 1
        bus.unsubscribe(q)
        # after unsubscribe, publishing reaches nobody and never raises
        bus.publish({"type": "event", "event": {"id": "c"}})
    asyncio.run(run())


# -------------------------------- redis ---------------------------------- #
def _fake_redis():
    pytest.importorskip("fakeredis")
    from fakeredis import aioredis
    return aioredis.FakeRedis(decode_responses=True)


async def test_redis_add_get_list_stats():
    store = RedisAuditStore(_fake_redis(), capacity=10, prefix="t")
    await store.add(_event(1))
    await store.add(_event(2))

    got = await store.get("evt2")
    assert got["model"] == "claude-x"
    lst = await store.list()
    assert [e["id"] for e in lst] == ["evt2", "evt1"]  # newest first
    stats = await store.stats()
    assert stats["requests"] == 2 and stats["entities_anonymized"] == 2
    await store.aclose()


async def test_redis_update_only_touches_existing():
    store = RedisAuditStore(_fake_redis(), capacity=10, prefix="t")
    ev = _event(1)
    await store.add(ev)
    ev.status = 201
    ev.latency_ms = 12.3
    await store.update(ev)
    got = await store.get("evt1")
    assert got["status"] == 201 and got["latency_ms"] == 12.3
    await store.aclose()


async def test_redis_capacity_trims_and_deletes_overflow():
    store = RedisAuditStore(_fake_redis(), capacity=3, prefix="t")
    for i in range(5):
        await store.add(_event(i))
    lst = await store.list(limit=100)
    assert [e["id"] for e in lst] == ["evt4", "evt3", "evt2"]
    assert await store.get("evt0") is None  # overflow key deleted, not orphaned
    assert (await store.stats())["requests"] == 3
    await store.aclose()


async def test_redis_pubsub_bridges_to_bus():
    bus = EventBus()
    store = RedisAuditStore(_fake_redis(), capacity=10, prefix="t", bus=bus)
    await store.start()
    q = bus.subscribe()
    try:
        await store.add(_event(7))
        msg = await asyncio.wait_for(q.get(), timeout=3.0)
        assert msg["type"] == "event" and msg["event"]["id"] == "evt7"
    finally:
        await store.aclose()


if __name__ == "__main__":
    import pytest as _p
    raise SystemExit(_p.main([__file__, "-q"]))
