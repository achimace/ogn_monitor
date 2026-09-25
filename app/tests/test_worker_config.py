"""Worker config loading (ignore list into AirfieldConfig) and the
tracker:config listener that triggers an immediate reload."""

import asyncio
from uuid import uuid4

import pytest

from app import worker
from app.tracking.redis_keys import TRACKER_CONFIG_CHANNEL

AF_A = uuid4()
AF_B = uuid4()


def _airfield_row(af_id, slug):
    return {
        "id": af_id, "slug": slug, "name": slug.title(), "latitude": 47.64,
        "longitude": 11.23, "elevation_m": 660.0, "home_radius_m": None,
        "ogn_filter_radius_km": None, "alarm_timeout_s": None,
        "signal_loss_timeout_s": None, "takeoff_speed_kmh": None,
        "takeoff_alt_offset_m": None, "tow_plane_flarm_ids": None,
        "winch_vs_threshold_ms": None, "landed_visible_minutes": None,
        "touch_go_max_ground_s": None, "silence_landing_s": None,
        "home_polygon_geojson": None,
    }


class FakeDb:
    def __init__(self, ignored_rows, fail_ignored=False):
        self.ignored_rows = ignored_rows
        self.fail_ignored = fail_ignored

    async def fetch(self, sql, *args):
        if "FROM airfield_ignored_aircraft" in sql:
            if self.fail_ignored:
                raise RuntimeError("relation does not exist")
            return list(self.ignored_rows)
        assert "FROM airfields" in sql
        return [_airfield_row(AF_A, "ohlstadt"), _airfield_row(AF_B, "other")]


# ---------------------------------------------------------------------------
# load_airfield_configs
# ---------------------------------------------------------------------------

async def test_configs_carry_per_airfield_ignore_sets(monkeypatch):
    db = FakeDb([
        {"airfield_id": AF_A, "flarm_id": "3e0abc"},
        {"airfield_id": AF_A, "flarm_id": "DDA5BA"},
        {"airfield_id": AF_B, "flarm_id": "3E0ABC"},
    ])
    monkeypatch.setattr(worker, "get_db", lambda: db)

    configs = await worker.load_airfield_configs()

    assert configs["ohlstadt"].ignored_flarm_ids == frozenset({"3E0ABC", "DDA5BA"})
    assert configs["other"].ignored_flarm_ids == frozenset({"3E0ABC"})
    assert isinstance(configs["ohlstadt"].ignored_flarm_ids, frozenset)


async def test_airfield_without_rows_has_empty_set(monkeypatch):
    monkeypatch.setattr(worker, "get_db", lambda: FakeDb([]))
    configs = await worker.load_airfield_configs()
    assert configs["ohlstadt"].ignored_flarm_ids == frozenset()
    assert configs["other"].ignored_flarm_ids == frozenset()


async def test_ignore_list_load_error_does_not_break_config_load(monkeypatch):
    monkeypatch.setattr(worker, "get_db", lambda: FakeDb([], fail_ignored=True))
    configs = await worker.load_airfield_configs()
    assert set(configs) == {"ohlstadt", "other"}
    assert configs["ohlstadt"].ignored_flarm_ids == frozenset()


# ---------------------------------------------------------------------------
# tracker_config_listener
# ---------------------------------------------------------------------------

class FakePubSub:
    def __init__(self, messages, fail_after=False):
        self.messages = messages
        self.fail_after = fail_after
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel):
        self.subscribed.append(channel)

    async def unsubscribe(self, channel):
        self.unsubscribed.append(channel)

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

    def pubsub(self):
        if not self._pubsubs:
            raise asyncio.CancelledError()      # scripted streams exhausted = shutdown
        return self._pubsubs.pop(0)


def msg(payload, kind="message") -> dict:
    return {"type": kind, "channel": TRACKER_CONFIG_CHANNEL, "data": payload}


class Reloader:
    def __init__(self, fail_first=False):
        self.calls = 0
        self.fail_first = fail_first

    async def __call__(self):
        self.calls += 1
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("db hiccup")


async def _run(redis, reload):
    with pytest.raises(asyncio.CancelledError):
        await worker.tracker_config_listener(redis, reload, reconnect_delay_s=0)


async def test_listener_reloads_on_every_message():
    ps = FakePubSub([msg("subscribe", kind="subscribe"), msg("ohlstadt"), msg(b"bytes")])
    reload = Reloader()
    await _run(FakeRedis(ps), reload)
    assert ps.subscribed == ["tracker:config"]
    assert reload.calls == 2                      # subscribe ack ignored
    assert ps.unsubscribed == ["tracker:config"] and ps.closed


async def test_listener_survives_failed_reload_and_reconnects():
    first = FakePubSub([msg("a"), msg("b")], fail_after=True)
    second = FakePubSub([msg("c")])
    reload = Reloader(fail_first=True)
    await _run(FakeRedis(first, second), reload)
    assert reload.calls == 3
    assert first.closed and second.closed


async def test_listener_stops_on_cancel():
    hang = asyncio.Event()

    class HangingPubSub(FakePubSub):
        async def listen(self):
            yield msg("ohlstadt")
            await hang.wait()

    ps = HangingPubSub([])
    reload = Reloader()
    task = asyncio.create_task(
        worker.tracker_config_listener(FakeRedis(ps), reload, reconnect_delay_s=0)
    )
    for _ in range(50):
        await asyncio.sleep(0.01)
        if reload.calls:
            break
    assert reload.calls == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ps.closed and ps.unsubscribed == ["tracker:config"]
