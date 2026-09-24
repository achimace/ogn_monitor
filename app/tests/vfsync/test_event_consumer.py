"""AP-3: EventConsumer with a fake Redis PubSub."""

import asyncio
import json
from uuid import uuid4

import pytest

from app.vfsync.event_consumer import EventConsumer
from app.vfsync.models import TenantConfig

AF_ON = uuid4()
AF_OFF = uuid4()
TENANTS = [
    TenantConfig(airfield_id=AF_ON, slug="ohlstadt", enabled=True),
    TenantConfig(airfield_id=AF_OFF, slug="disabled", enabled=False),
]


class FakePubSub:
    def __init__(self, messages, fail_after=False):
        self.messages = messages
        self.fail_after = fail_after
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def psubscribe(self, pattern):
        self.subscribed.append(pattern)

    async def punsubscribe(self, pattern):
        self.unsubscribed.append(pattern)

    async def close(self):
        self.closed = True

    async def listen(self):
        for m in self.messages:
            yield m
        if self.fail_after:
            raise ConnectionError("redis gone")


class FakeRedis:
    def __init__(self, *pubsubs):
        self._pubsubs = list(pubsubs)
        self.created: list[FakePubSub] = []

    def pubsub(self):
        ps = self._pubsubs.pop(0)
        self.created.append(ps)
        return ps


class RecordingCoordinator:
    def __init__(self, fail_on: str | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.fail_on = fail_on

    async def on_event(self, tenant, event):
        if event.get("type") == self.fail_on:
            raise RuntimeError("boom")
        self.calls.append((tenant.slug, event))


def pmessage(channel: str, payload, raw=False) -> dict:
    data = payload if raw else json.dumps(payload)
    return {"type": "pmessage", "pattern": "event:*", "channel": channel, "data": data}


async def test_dispatches_enabled_slugs_only():
    ev = {"type": "takeoff", "flarm_id": "DDA5BA", "message": "", "data": {"takeoff_time": "x"}}
    ps = FakePubSub([
        {"type": "psubscribe", "pattern": "event:*", "channel": "event:*", "data": 1},
        pmessage("event:ohlstadt", ev),
        pmessage("event:disabled", ev),
        pmessage("event:unknown", ev),
        pmessage("beacon:ohlstadt", ev),
        {"type": "pmessage", "channel": b"event:ohlstadt", "data": json.dumps(ev).encode()},
    ])
    coord = RecordingCoordinator()
    activity = []
    consumer = EventConsumer(FakeRedis(ps), lambda: TENANTS, coord,
                             on_activity=lambda: activity.append(1), reconnect_delay_s=0)
    await consumer.run()

    assert [slug for slug, _ in coord.calls] == ["ohlstadt", "ohlstadt"]
    assert coord.calls[0][1] == ev
    assert consumer.events_handled == 2
    assert consumer.events_ignored == 2
    assert consumer.last_event_ts is not None
    assert activity == [1, 1]
    assert ps.subscribed == ["event:*"] and ps.unsubscribed == ["event:*"] and ps.closed


async def test_malformed_and_failing_messages_are_tolerated():
    ok = {"type": "landing_final", "flarm_id": "DDA5BA", "data": {}}
    bad = {"type": "takeoff", "flarm_id": "DDA5BA", "data": {}}
    ps = FakePubSub([
        pmessage("event:ohlstadt", "{not json", raw=True),
        pmessage("event:ohlstadt", [1, 2, 3]),
        pmessage("event:ohlstadt", bad),      # coordinator raises
        pmessage("event:ohlstadt", ok),
    ])
    coord = RecordingCoordinator(fail_on="takeoff")
    consumer = EventConsumer(FakeRedis(ps), lambda: TENANTS, coord, reconnect_delay_s=0)
    await consumer.run()
    assert [e["type"] for _, e in coord.calls] == ["landing_final"]
    assert consumer.events_failed == 3
    assert consumer.events_handled == 1


async def test_async_on_activity_is_awaited():
    seen = []

    async def on_activity():
        seen.append(True)

    ps = FakePubSub([pmessage("event:ohlstadt", {"type": "takeoff", "flarm_id": "A", "data": {}})])
    consumer = EventConsumer(FakeRedis(ps), lambda: TENANTS, RecordingCoordinator(),
                             on_activity=on_activity)
    await consumer.run()
    assert seen == [True]


async def test_tenants_provider_is_consulted_per_message():
    tenants: list[TenantConfig] = []
    ev = {"type": "takeoff", "flarm_id": "A", "data": {}}
    ps = FakePubSub([pmessage("event:ohlstadt", ev), pmessage("event:ohlstadt", ev)])
    coord = RecordingCoordinator()

    class Provider:
        def __call__(self):
            snapshot = list(tenants)         # first call: empty -> message ignored
            tenants.append(TENANTS[0])       # enabled from the second call on
            return snapshot

    consumer = EventConsumer(FakeRedis(ps), Provider(), coord)
    await consumer.run()
    assert len(coord.calls) == 1


async def test_reconnects_after_redis_error_and_stops_on_cancel():
    ev = {"type": "takeoff", "flarm_id": "A", "data": {}}
    first = FakePubSub([pmessage("event:ohlstadt", ev)], fail_after=True)
    hang = asyncio.Event()

    class HangingPubSub(FakePubSub):
        async def listen(self):
            yield pmessage("event:ohlstadt", ev)
            await hang.wait()

    second = HangingPubSub([])
    redis = FakeRedis(first, second)
    coord = RecordingCoordinator()
    consumer = EventConsumer(redis, lambda: TENANTS, coord, reconnect_delay_s=0)
    task = asyncio.create_task(consumer.run())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(coord.calls) == 2:
            break
    assert len(coord.calls) == 2
    assert first.closed and first.unsubscribed == ["event:*"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert second.closed
