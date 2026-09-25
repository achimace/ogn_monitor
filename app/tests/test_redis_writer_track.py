"""RedisWriter.add_track_point: per-aircraft track stream (track:{slug}:{fid}).

Uses a fake redis client whose pipeline records the queued commands.
"""

import pytest
from redis.exceptions import ResponseError

from app.config import settings
from app.tracking.redis_writer import RedisWriter


class FakePipeline:
    def __init__(self, redis):
        self._redis = redis
        self.calls: list[tuple] = []

    def xadd(self, key, fields, id="*", maxlen=None, approximate=False, **kw):
        self.calls.append(("xadd", key, fields, id, maxlen, approximate))

    def expire(self, key, seconds):
        self.calls.append(("expire", key, seconds))

    async def execute(self):
        self._redis.executed.append(list(self.calls))
        if self._redis.fail_with is not None:
            raise self._redis.fail_with
        return [None] * len(self.calls)


class FakeRedis:
    def __init__(self, fail_with: Exception | None = None):
        self.executed: list[list[tuple]] = []
        self.fail_with = fail_with
        self.deleted: list[str] = []

    async def delete(self, *keys):
        self.deleted.extend(keys)
        return len(keys)

    def pipeline(self):
        return FakePipeline(self)


@pytest.fixture
def redis():
    return FakeRedis()


async def _add(writer, retention=86400, min_interval=5, **overrides):
    kw = dict(
        airfield_slug="test", flarm_id="DDA5BA", ts_ms=1_758_800_405_000,
        lat=47.6123456, lon=11.2345678, alt_m=1200.4, alt_agl_m=540.6,
        speed_kmh=95.4, vs_ms=1.24, track_deg=180.4, retention_s=retention,
        min_interval_s=min_interval,
    )
    kw.update(overrides)
    await writer.add_track_point(**kw)


async def test_xadd_uses_beacon_time_as_stream_id_and_rounds_fields(redis):
    await _add(RedisWriter(redis))

    assert len(redis.executed) == 1
    xadd, expire = redis.executed[0]
    assert xadd[0] == "xadd"
    assert xadd[1] == "track:test:DDA5BA"
    assert xadd[2] == {
        "lat": "47.61235", "lon": "11.23457", "alt": "1200", "agl": "541",
        "speed": "95", "vs": "1.2", "track": "180",
    }
    assert xadd[3] == "1758800405000-*"
    assert all(isinstance(v, str) for v in xadd[2].values())
    assert expire == ("expire", "track:test:DDA5BA", 86400)


async def test_expire_uses_given_retention(redis):
    await _add(RedisWriter(redis), retention=3600)
    assert redis.executed[0][1] == ("expire", "track:test:DDA5BA", 3600)


async def test_maxlen_guard_derived_from_retention_and_interval(redis):
    await _add(RedisWriter(redis), retention=86400, min_interval=5)
    xadd = redis.executed[0][0]
    assert xadd[4] == 86400 // 5
    assert xadd[5] is True  # approximate trimming

    await _add(RedisWriter(redis), retention=3600, min_interval=10)
    assert redis.executed[1][0][4] == 360


async def test_maxlen_ignores_settings(redis, monkeypatch):
    # the interval is a parameter, not read from settings
    monkeypatch.setattr(settings, "track_min_interval_s", 1)
    await _add(RedisWriter(redis), retention=86400, min_interval=5)
    assert redis.executed[0][0][4] == 86400 // 5


async def test_maxlen_is_at_least_one_even_with_zero_interval(redis):
    await _add(RedisWriter(redis), retention=0, min_interval=0)
    assert redis.executed[0][0][4] == 1


async def test_out_of_order_beacon_is_skipped_silently():
    redis = FakeRedis(fail_with=ResponseError(
        "ERR The ID specified in XADD is equal or smaller than the target stream top item"
    ))
    await _add(RedisWriter(redis))  # must not raise
    assert len(redis.executed) == 1


async def test_other_response_errors_propagate():
    redis = FakeRedis(fail_with=ResponseError(
        "MISCONF Redis is configured to save RDB snapshots, but it's currently "
        "unable to persist to disk."
    ))
    with pytest.raises(ResponseError, match="MISCONF"):
        await _add(RedisWriter(redis))


async def test_other_redis_errors_propagate():
    redis = FakeRedis(fail_with=ConnectionError("redis gone"))
    with pytest.raises(ConnectionError):
        await _add(RedisWriter(redis))


async def test_delete_track_removes_the_stream_key(redis):
    """DDB tracked=N eviction drops the whole 24 h track of the device."""
    await RedisWriter(redis).delete_track("test", "DDA5BA")
    assert redis.deleted == ["track:test:DDA5BA"]
    assert redis.executed == []  # plain DEL, no pipeline
