"""Immediate config reload: CLI tools publish on vfsync:config, the worker
listener reloads on every message (UAT T-04: no 5 min wait / restart)."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.config import settings
from app.vfsync import crypto, tools
from app.vfsync import main as vfmain
from app.vfsync.models import TenantConfig
from app.vfsync.redis_keys import VFSYNC_CONFIG_CHANNEL

AIRFIELD_ID = uuid4()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeDb:
    def __init__(self):
        self.executed: list[tuple] = []

    async def fetchrow(self, sql, *args):
        return {"id": AIRFIELD_ID}

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


class FakePublisher:
    def __init__(self, fail=False):
        self.fail = fail
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel, payload):
        if self.fail:
            raise ConnectionError("redis gone")
        self.published.append((channel, payload))


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(tools, "get_db", lambda: fake)
    return fake


@pytest.fixture
def cred_key(monkeypatch):
    key = crypto.generate_key()
    monkeypatch.setattr(settings, "vfsync_cred_key", key)
    return key


def _wire_redis(monkeypatch, publisher: FakePublisher) -> list[str]:
    """Route tools.init_redis/close_redis to the fake; returns the lifecycle log."""
    lifecycle: list[str] = []

    async def fake_init():
        lifecycle.append("init")
        return publisher

    async def fake_close():
        lifecycle.append("close")

    monkeypatch.setattr(tools, "init_redis", fake_init)
    monkeypatch.setattr(tools, "close_redis", fake_close)
    return lifecycle


COMMANDS = {
    "set-credentials": (tools.cmd_set_credentials, dict(
        username="tech", password_md5="5f4dcc3b5aa765d61d8327deb882cf99", password=None,
        appkey="k", cid=None, base_url=None,
    )),
    "enable": (tools.cmd_enable, dict(live=False)),
    "disable": (tools.cmd_disable, dict()),
}


# ---------------------------------------------------------------------------
# CLI tools
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", sorted(COMMANDS))
async def test_tools_publish_slug_after_write(cmd, db, cred_key, monkeypatch, capsys):
    fn, extra = COMMANDS[cmd]
    publisher = FakePublisher()
    lifecycle = _wire_redis(monkeypatch, publisher)

    await fn(SimpleNamespace(slug="ohlstadt", **extra))

    assert len(db.executed) == 1                      # the write happened
    assert publisher.published == [(VFSYNC_CONFIG_CHANNEL, "ohlstadt")]
    assert lifecycle == ["init", "close"]             # own connection, closed again
    assert "ohlstadt" in capsys.readouterr().out


@pytest.mark.parametrize("cmd", sorted(COMMANDS))
async def test_tools_survive_publish_failure(cmd, db, cred_key, monkeypatch):
    fn, extra = COMMANDS[cmd]
    lifecycle = _wire_redis(monkeypatch, FakePublisher(fail=True))

    await fn(SimpleNamespace(slug="ohlstadt", **extra))   # must not raise

    assert len(db.executed) == 1
    assert lifecycle == ["init", "close"]


async def test_tools_survive_missing_redis(db, cred_key, monkeypatch):
    async def fake_init():
        raise ConnectionError("no redis")

    monkeypatch.setattr(tools, "init_redis", fake_init)
    assert await tools.notify_config_changed("ohlstadt") is False
    await tools.cmd_disable(SimpleNamespace(slug="ohlstadt"))
    assert len(db.executed) == 1


# ---------------------------------------------------------------------------
# Worker listener
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
            # Scripted streams exhausted: simulate the worker shutdown.
            raise asyncio.CancelledError()
        return self._pubsubs.pop(0)


def msg(payload, kind="message") -> dict:
    return {"type": kind, "channel": VFSYNC_CONFIG_CHANNEL, "data": payload}


@pytest.fixture
def reload_calls(monkeypatch):
    calls: list[tuple] = []

    async def fake_reload(state, config, coordinator):
        calls.append((state, config, coordinator))
        if getattr(state, "fail_next", False):
            state.fail_next = False
            raise RuntimeError("db hiccup")

    monkeypatch.setattr(vfmain, "reload_config", fake_reload)
    return calls


async def _run_listener(redis, state):
    with pytest.raises(asyncio.CancelledError):
        await vfmain.config_change_listener(redis, state, "cfg", "coord", reconnect_delay_s=0)


async def test_listener_reloads_on_message(reload_calls):
    ps = FakePubSub([msg("subscribe", kind="subscribe"), msg("ohlstadt"), msg(b"bytes-slug")])
    state = SimpleNamespace()
    await _run_listener(FakeRedis(ps), state)

    assert ps.subscribed == [VFSYNC_CONFIG_CHANNEL]
    assert reload_calls == [(state, "cfg", "coord")] * 2   # subscribe ack ignored
    assert ps.unsubscribed == [VFSYNC_CONFIG_CHANNEL] and ps.closed


async def test_listener_survives_failed_reload_and_reconnects(reload_calls):
    first = FakePubSub([msg("a"), msg("b")], fail_after=True)
    second = FakePubSub([msg("c")])
    state = SimpleNamespace(fail_next=True)          # first reload raises
    await _run_listener(FakeRedis(first, second), state)

    assert len(reload_calls) == 3                    # a (failed), b, c after re-subscribe
    assert first.closed and second.closed


async def test_listener_stops_on_cancel(reload_calls):
    hang = asyncio.Event()

    class HangingPubSub(FakePubSub):
        async def listen(self):
            yield msg("ohlstadt")
            await hang.wait()

    ps = HangingPubSub([])
    task = asyncio.create_task(
        vfmain.config_change_listener(FakeRedis(ps), SimpleNamespace(), "cfg", "coord",
                                      reconnect_delay_s=0)
    )
    for _ in range(50):
        await asyncio.sleep(0.01)
        if reload_calls:
            break
    assert len(reload_calls) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ps.closed and ps.unsubscribed == [VFSYNC_CONFIG_CHANNEL]


# ---------------------------------------------------------------------------
# reload_config: periodic loop and listener may overlap
# ---------------------------------------------------------------------------

class SlowConfigStore:
    """load_enabled that yields to the loop, so two reloads can interleave."""

    def __init__(self, tenants, delay_s=0.02):
        self.tenants = tenants
        self.delay_s = delay_s
        self.loads = 0

    async def load_enabled(self):
        self.loads += 1
        await asyncio.sleep(self.delay_s)
        return list(self.tenants)


class RecordingCoordinator:
    def __init__(self):
        self.recovered: list[str] = []
        self.resumed: list[str] = []

    async def recover(self, tenant):
        await asyncio.sleep(0)
        self.recovered.append(tenant.slug)

    def resume(self, tenant):
        self.resumed.append(tenant.slug)


def _tenant(slug: str) -> TenantConfig:
    return TenantConfig(airfield_id=uuid4(), slug=slug, enabled=True,
                        vf_username="u", vf_password_md5="p", vf_appkey="k")


async def test_concurrent_reloads_recover_each_new_tenant_once():
    """Listener and periodic loop firing together must not produce two
    recovery runs (= duplicate audit rows) for the same tenant."""
    tenants = [_tenant("ohlstadt"), _tenant("koenigsdorf")]
    config = SlowConfigStore(tenants)
    coord = RecordingCoordinator()
    state = vfmain.RuntimeState()

    await asyncio.gather(
        vfmain.reload_config(state, config, coord),
        vfmain.reload_config(state, config, coord),
    )

    assert config.loads == 2                                   # both reloads ran ...
    assert sorted(coord.recovered) == ["koenigsdorf", "ohlstadt"]   # ... recovery once each
    assert [t.slug for t in state.tenants] == ["ohlstadt", "koenigsdorf"]

    # a later reload with unchanged config recovers nothing
    await vfmain.reload_config(state, config, coord)
    assert len(coord.recovered) == 2


async def test_reload_lock_is_per_runtime_state():
    assert isinstance(vfmain.RuntimeState().reload_lock, asyncio.Lock)
    assert vfmain.RuntimeState().reload_lock is not vfmain.RuntimeState().reload_lock


# ---------------------------------------------------------------------------
# notify_config_changed: bounded connect
# ---------------------------------------------------------------------------

async def test_tools_do_not_hang_when_redis_connect_stalls(db, cred_key, monkeypatch):
    """Unreachable Redis (TCP black hole): the CLI must return after
    CONFIG_SIGNAL_TIMEOUT_S with the DB write intact."""
    hang = asyncio.Event()
    closed: list[bool] = []

    async def hanging_init():
        await hang.wait()          # never set

    async def fake_close():
        closed.append(True)

    monkeypatch.setattr(tools, "init_redis", hanging_init)
    monkeypatch.setattr(tools, "close_redis", fake_close)
    monkeypatch.setattr(tools, "CONFIG_SIGNAL_TIMEOUT_S", 0.05)

    assert await asyncio.wait_for(tools.notify_config_changed("ohlstadt"), timeout=2) is False
    assert closed == []                # nothing to close: connect never completed

    await asyncio.wait_for(tools.cmd_enable(SimpleNamespace(slug="ohlstadt", live=False)),
                           timeout=2)
    assert len(db.executed) == 1
