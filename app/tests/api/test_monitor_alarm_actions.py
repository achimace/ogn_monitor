"""Tower alarm workflow: /api/monitor/{slug}/flights/{flarm_id}/actions.

Contract tests against a fake Redis (decode_responses=True semantics) and an
in-memory fake DB. SQL behaviour (ordering, date filter, tenant isolation
in the history queries) is covered by test_monitor_alarm_actions_pg.py.
"""

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from starlette.websockets import WebSocketState

import app.db.connection as db_connection
from app.api import monitor as monitor_api
from app.api.connection_manager import MonitorConnectionManager
from app.dependencies import get_current_user
from app.tracking.flight_state import FlightStatus

SLUG = "test"
FID = "DDA5BA"
TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
AIRFIELD_ID = UUID("33333333-3333-3333-3333-333333333333")
EMAIL = "max@example.invalid"
TENANT_NAME = "SFG Testverein"
TAKEOFF = "2026-09-25T09:15:00Z"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakePipeline:
    def __init__(self, redis: "FakeRedis"):
        self._redis = redis
        self._ops: list = []

    def hgetall(self, key: str):
        self._ops.append(("hgetall", key))
        return self

    async def execute(self):
        return [dict(self._redis.hashes.get(key, {})) for _, key in self._ops]


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.published: list[tuple[str, dict]] = []
        self.hset_calls: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        # TTL answered for existing hashes (worker sets one on every flight
        # hash); -1 simulates a hash the API re-created after the worker
        # deleted it.
        self.ttls: dict[str, int] = {}

    async def hgetall(self, key: str) -> dict:
        return dict(self.hashes.get(key, {}))

    async def ttl(self, key: str) -> int:
        if key not in self.hashes:
            return -2
        return self.ttls.get(key, 3600)

    async def delete(self, *keys: str) -> int:
        n = 0
        for key in keys:
            self.deleted.append(key)
            if self.hashes.pop(key, None) is not None:
                n += 1
        return n

    async def hset(self, key: str, mapping: dict | None = None, **kw) -> int:
        assert mapping is not None, "API must HSET with a mapping"
        self.hset_calls.append((key, dict(mapping)))
        self.hashes.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})
        return len(mapping)

    async def smembers(self, key: str) -> set:
        return set(self.sets.get(key, set()))

    async def sismember(self, key: str, member: str) -> bool:
        return member in self.sets.get(key, set())

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, json.loads(payload)))
        return 1

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)


class FakeDb:
    """Answers the three statements the endpoints use."""

    def __init__(self):
        self.airfields = {SLUG: {"id": AIRFIELD_ID, "tenant_id": TENANT,
                                 "tenant_name": TENANT_NAME}}
        self.rows: list[dict] = []
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        if "FROM airfields" in sql:
            return self.airfields.get(args[0])
        if "INSERT INTO flight_alarm_actions" in sql:
            airfield_id, flarm_id, takeoff_ts, kind, state, comment, set_by = args
            row = {
                "id": uuid4(), "airfield_id": airfield_id, "flarm_id": flarm_id,
                "flight_takeoff_ts": takeoff_ts, "alarm_kind": kind, "state": state,
                "comment": comment, "set_by": set_by,
                "created_at": datetime.now(timezone.utc).replace(microsecond=0),
            }
            self.rows.append(row)
            return row
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetch(self, sql: str, *args):
        assert "FROM flight_alarm_actions" in sql
        self.fetch_calls.append((sql, args))
        return list(reversed(self.rows))


def _hot_flight(status: FlightStatus, **extra) -> dict[str, str]:
    data = {
        "flarm_id": FID, "registration": "D-KMSF", "status": str(int(status)),
        "takeoff_time": TAKEOFF, "latitude": "47.6", "longitude": "11.2",
    }
    data.update({k: str(v) for k, v in extra.items()})
    return data


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def fake_db():
    return FakeDb()


def _make_app(user: dict | None) -> FastAPI:
    app = FastAPI()
    app.include_router(monitor_api.router)
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return app


@pytest.fixture
def user() -> dict:
    return {"tenant_id": TENANT, "email": EMAIL, "email_verified": True}


@pytest.fixture
async def client(fake_redis, fake_db, user, monkeypatch):
    monkeypatch.setattr(monitor_api, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(db_connection, "get_db", lambda: fake_db)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_make_app(user)), base_url="http://t"
    ) as c:
        yield c


@pytest.fixture
async def anon_client(fake_redis, fake_db, monkeypatch):
    monkeypatch.setattr(monitor_api, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(db_connection, "get_db", lambda: fake_db)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_make_app(None)), base_url="http://t"
    ) as c:
        yield c


@pytest.fixture
async def foreign_client(fake_redis, fake_db, monkeypatch):
    monkeypatch.setattr(monitor_api, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(db_connection, "get_db", lambda: fake_db)
    other = {"tenant_id": OTHER_TENANT, "email": "x@example.invalid", "email_verified": True}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_make_app(other)), base_url="http://t"
    ) as c:
        yield c


ACTIONS = f"/api/monitor/{SLUG}/flights/{FID}/actions"


# ---------------------------------------------------------------------------
# Auth / tenant / slug
# ---------------------------------------------------------------------------

async def test_post_without_token_is_401(anon_client, fake_db):
    r = await anon_client.post(ACTIONS, json={"state": "acknowledged"})
    assert r.status_code == 401
    assert fake_db.rows == []


async def test_get_without_token_is_401(anon_client):
    assert (await anon_client.get(ACTIONS)).status_code == 401
    assert (await anon_client.get(f"/api/monitor/{SLUG}/actions")).status_code == 401


async def test_foreign_tenant_is_403(foreign_client, fake_db, fake_redis):
    fake_redis.hashes[f"flight:{SLUG}:{FID}"] = _hot_flight(FlightStatus.ALARM)
    r = await foreign_client.post(ACTIONS, json={"state": "acknowledged"})
    assert r.status_code == 403
    assert fake_db.rows == [] and fake_redis.hset_calls == [] and fake_redis.published == []
    assert (await foreign_client.get(ACTIONS)).status_code == 403
    assert (await foreign_client.get(f"/api/monitor/{SLUG}/actions")).status_code == 403


async def test_unknown_slug_is_404(client, fake_db):
    r = await client.post("/api/monitor/nowhere/flights/ABCDEF/actions",
                          json={"state": "acknowledged"})
    assert r.status_code == 404
    assert (await client.get("/api/monitor/nowhere/flights/ABCDEF/actions")).status_code == 404
    assert (await client.get("/api/monitor/nowhere/actions")).status_code == 404
    assert fake_db.rows == []


# ---------------------------------------------------------------------------
# POST with hot state
# ---------------------------------------------------------------------------

async def test_post_with_hot_state_derives_kind_and_takeoff_and_mirrors(client, fake_redis, fake_db):
    key = f"flight:{SLUG}:{FID}"
    fake_redis.hashes[key] = _hot_flight(FlightStatus.ALARM, altitude_m=1200)

    r = await client.post(ACTIONS, json={"state": "acknowledged", "comment": "  Pilot per Handy erreicht  "})
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"action", "alarmState"}

    action = body["action"]
    assert action["state"] == "acknowledged"
    assert action["alarmKind"] == "alarm"
    assert action["comment"] == "Pilot per Handy erreicht"
    assert action["setBy"] == EMAIL
    assert action["flightTakeoffTs"] == TAKEOFF
    assert action["createdAt"].endswith("Z")
    UUID(action["id"])

    # alarmState mirrors the public hash: label instead of the e-mail
    assert body["alarmState"] == {
        "alarmState": "acknowledged",
        "alarmComment": "Pilot per Handy erreicht",
        "alarmSetBy": TENANT_NAME,
        "alarmSetAt": action["createdAt"],
    }

    # DB row: kind + takeoff ts from the hot state, body alarm_kind ignored
    row = fake_db.rows[0]
    assert row["alarm_kind"] == "alarm"
    assert row["flight_takeoff_ts"] == datetime(2026, 9, 25, 9, 15, tzinfo=timezone.utc)
    assert row["airfield_id"] == AIRFIELD_ID and row["flarm_id"] == FID

    # HSET with a mapping of exactly the four alarm_* fields, nothing else touched
    assert fake_redis.hset_calls == [(key, {
        "alarm_state": "acknowledged",
        "alarm_comment": "Pilot per Handy erreicht",
        "alarm_set_by": TENANT_NAME,
        "alarm_set_at": action["createdAt"],
    })]
    # The hash is public: the Flugleiter's e-mail must not be in it
    assert EMAIL not in json.dumps(fake_redis.hashes[key])
    assert fake_redis.deleted == []
    assert fake_redis.hashes[key]["status"] == str(int(FlightStatus.ALARM))
    assert fake_redis.hashes[key]["altitude_m"] == "1200"

    # PUBLISH on event:{slug}
    assert len(fake_redis.published) == 1
    channel, event = fake_redis.published[0]
    assert channel == f"event:{SLUG}"
    assert event == {
        "type": "alarm_action",
        "flarm_id": FID,
        "data": {
            "alarm_state": "acknowledged",
            "alarm_comment": "Pilot per Handy erreicht",
            "alarm_set_by": TENANT_NAME,
            "alarm_set_at": action["createdAt"],
        },
        "message": f"Alarm quittiert ({TENANT_NAME})",
    }
    assert EMAIL not in json.dumps(event)


@pytest.mark.parametrize("tenant_name", [None, "", "   "])
async def test_alarm_set_by_falls_back_to_flugleiter_without_tenant_name(
    client, fake_redis, fake_db, tenant_name,
):
    fake_db.airfields[SLUG]["tenant_name"] = tenant_name
    key = f"flight:{SLUG}:{FID}"
    fake_redis.hashes[key] = _hot_flight(FlightStatus.ALARM)

    r = await client.post(ACTIONS, json={"state": "acknowledged"})
    assert r.status_code == 201
    assert r.json()["alarmState"]["alarmSetBy"] == "Flugleiter"
    assert r.json()["action"]["setBy"] == EMAIL  # auth-only history keeps the user
    assert fake_redis.hashes[key]["alarm_set_by"] == "Flugleiter"
    event = fake_redis.published[0][1]
    assert event["data"]["alarm_set_by"] == "Flugleiter"
    assert event["message"] == "Alarm quittiert (Flugleiter)"
    assert EMAIL not in json.dumps(event)


async def test_hset_is_undone_when_worker_removed_the_flight_meanwhile(
    client, fake_redis, fake_db,
):
    """HGETALL saw the flight, the worker archived it before our HSET: the
    HSET re-created the hash without TTL -> the API must delete it again
    and must not announce an alarm_action for a flight that is gone."""
    key = f"flight:{SLUG}:{FID}"
    fake_redis.hashes[key] = _hot_flight(FlightStatus.ALARM)
    fake_redis.ttls[key] = -1

    r = await client.post(ACTIONS, json={"state": "resolved", "comment": "spät"})
    assert r.status_code == 201, r.text
    # history row is still written
    assert len(fake_db.rows) == 1 and fake_db.rows[0]["state"] == "resolved"
    assert r.json()["alarmState"]["alarmState"] == "resolved"
    # HSET happened, but the orphan hash was removed and nothing published
    assert len(fake_redis.hset_calls) == 1
    assert fake_redis.deleted == [key]
    assert key not in fake_redis.hashes
    assert fake_redis.published == []


@pytest.mark.parametrize("status_code,kind", [
    (FlightStatus.ALARM, "alarm"),
    (FlightStatus.EMERGENCY, "emergency"),
    (FlightStatus.OUTLANDING, "outlanding"),
    (FlightStatus.OUTLANDING_PENDING, "outlanding"),
    (FlightStatus.SIGNAL_LOST, "signal_lost"),
    (FlightStatus.FLYING, "other"),
    (FlightStatus.DIVERTED, "other"),
])
async def test_alarm_kind_follows_hot_state_status(client, fake_redis, status_code, kind):
    fake_redis.hashes[f"flight:{SLUG}:{FID}"] = _hot_flight(status_code)
    # a body alarm_kind must not override the hot-state derived kind
    r = await client.post(ACTIONS, json={"state": "resolved", "alarm_kind": "emergency"})
    assert r.status_code == 201
    assert r.json()["action"]["alarmKind"] == kind


@pytest.mark.parametrize("state,text", [
    ("acknowledged", "Alarm quittiert"),
    ("retrieval_underway", "Rückholung läuft"),
    ("resolved", "Alarm erledigt"),
    ("false_alarm", "Fehlalarm"),
])
async def test_event_message_is_german_per_state(client, fake_redis, state, text):
    fake_redis.hashes[f"flight:{SLUG}:{FID}"] = _hot_flight(FlightStatus.EMERGENCY)
    r = await client.post(ACTIONS, json={"state": state})
    assert r.status_code == 201
    assert fake_redis.published[0][1]["message"] == f"{text} ({TENANT_NAME})"
    assert fake_redis.published[0][1]["data"]["alarm_state"] == state


async def test_missing_comment_is_empty_string_in_hot_state(client, fake_redis):
    key = f"flight:{SLUG}:{FID}"
    fake_redis.hashes[key] = _hot_flight(FlightStatus.SIGNAL_LOST, takeoff_time="")
    r = await client.post(ACTIONS, json={"state": "false_alarm"})
    assert r.status_code == 201
    assert r.json()["alarmState"]["alarmComment"] == ""
    assert r.json()["action"]["comment"] is None
    assert r.json()["action"]["flightTakeoffTs"] is None
    assert fake_redis.hashes[key]["alarm_comment"] == ""


async def test_flarm_id_is_uppercased(client, fake_redis, fake_db):
    fake_redis.hashes[f"flight:{SLUG}:{FID}"] = _hot_flight(FlightStatus.ALARM)
    r = await client.post(f"/api/monitor/{SLUG}/flights/{FID.lower()}/actions",
                          json={"state": "acknowledged"})
    assert r.status_code == 201
    assert fake_db.rows[0]["flarm_id"] == FID
    assert fake_redis.published[0][1]["flarm_id"] == FID


# ---------------------------------------------------------------------------
# POST without hot state
# ---------------------------------------------------------------------------

async def test_post_without_hot_state_still_stores(client, fake_redis, fake_db):
    r = await client.post(ACTIONS, json={"state": "resolved", "alarm_kind": "outlanding",
                                         "comment": "Rückholer unterwegs"})
    assert r.status_code == 201
    body = r.json()
    assert body["action"]["alarmKind"] == "outlanding"
    assert body["action"]["flightTakeoffTs"] is None
    assert body["alarmState"]["alarmState"] == "resolved"
    assert len(fake_db.rows) == 1
    # nothing mirrored, nothing published
    assert fake_redis.hset_calls == [] and fake_redis.published == []
    assert f"flight:{SLUG}:{FID}" not in fake_redis.hashes


async def test_post_without_hot_state_defaults_kind_to_other(client, fake_db):
    r = await client.post(ACTIONS, json={"state": "acknowledged"})
    assert r.status_code == 201
    assert r.json()["action"]["alarmKind"] == "other"
    assert fake_db.rows[0]["alarm_kind"] == "other"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"state": "done"},
    {"state": ""},
    {},
    {"state": "acknowledged", "alarm_kind": "fire"},
    {"state": "acknowledged", "comment": "x" * 501},
    {"state": "acknowledged", "comment": 42},
])
async def test_invalid_body_is_422(client, fake_db, fake_redis, body):
    fake_redis.hashes[f"flight:{SLUG}:{FID}"] = _hot_flight(FlightStatus.ALARM)
    r = await client.post(ACTIONS, json=body)
    assert r.status_code == 422
    assert fake_db.rows == [] and fake_redis.hset_calls == [] and fake_redis.published == []


async def test_comment_of_exactly_500_chars_is_accepted(client):
    r = await client.post(ACTIONS, json={"state": "acknowledged", "comment": "x" * 500})
    assert r.status_code == 201
    assert len(r.json()["action"]["comment"]) == 500


async def test_bad_date_query_is_422(client):
    r = await client.get(ACTIONS, params={"date": "25.09.2026"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET history (contract; ordering / filter semantics in the PG tests)
# ---------------------------------------------------------------------------

async def test_get_history_shape_and_default_day(client, fake_db):
    await client.post(ACTIONS, json={"state": "acknowledged"})
    await client.post(ACTIONS, json={"state": "resolved", "comment": "ok"})

    r = await client.get(ACTIONS)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"items", "count"}
    assert body["count"] == 2
    assert [i["state"] for i in body["items"]] == ["resolved", "acknowledged"]
    assert set(body["items"][0]) == {
        "id", "state", "comment", "alarmKind", "setBy", "createdAt", "flightTakeoffTs",
    }

    # default window = today UTC, passed as aware datetimes (no strings)
    sql, args = fake_db.fetch_calls[-1]
    assert "ORDER BY created_at DESC" in sql
    airfield_id, flarm_id, start, end = args
    assert airfield_id == AIRFIELD_ID and flarm_id == FID
    today = datetime.now(timezone.utc).date()
    assert start == datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    assert (end - start).days == 1


async def test_get_history_explicit_date(client, fake_db):
    r = await client.get(ACTIONS, params={"date": "2026-05-01"})
    assert r.status_code == 200 and r.json() == {"items": [], "count": 0}
    _, args = fake_db.fetch_calls[-1]
    assert args[2] == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert args[3] == datetime(2026, 5, 2, tzinfo=timezone.utc)


async def test_get_airfield_history_queries_by_airfield_only(client, fake_db):
    await client.post(ACTIONS, json={"state": "acknowledged"})
    r = await client.get(f"/api/monitor/{SLUG}/actions", params={"date": "2026-05-01"})
    assert r.status_code == 200
    assert r.json()["count"] == 1
    sql, args = fake_db.fetch_calls[-1]
    assert "flarm_id" not in sql.split("WHERE", 1)[1]
    assert args == (AIRFIELD_ID,
                    datetime(2026, 5, 1, tzinfo=timezone.utc),
                    datetime(2026, 5, 2, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# Public monitor exposes the mirrored fields
# ---------------------------------------------------------------------------

async def test_public_status_exposes_alarm_state(client, fake_redis):
    fake_redis.sets[f"flights:{SLUG}"] = {FID}
    fake_redis.hashes[f"flight:{SLUG}:{FID}"] = _hot_flight(FlightStatus.ALARM)
    await client.post(ACTIONS, json={"state": "acknowledged", "comment": "ok"})

    r = await client.get(f"/api/monitor/{SLUG}")
    assert r.status_code == 200
    flight = r.json()["flights"][0]
    assert flight["flarmId"] == FID
    assert flight["alarmState"] == "acknowledged"
    assert flight["alarmComment"] == "ok"
    # Public endpoint: display label, never the Flugleiter's e-mail
    assert flight["alarmSetBy"] == TENANT_NAME
    assert EMAIL not in r.text
    assert flight["alarmSetAt"].endswith("Z")
    assert r.json()["stats"]["alarm"] == 1


async def test_public_status_has_no_alarm_fields_without_action(client, fake_redis):
    fake_redis.sets[f"flights:{SLUG}"] = {FID}
    fake_redis.hashes[f"flight:{SLUG}:{FID}"] = _hot_flight(FlightStatus.ALARM)
    flight = (await client.get(f"/api/monitor/{SLUG}")).json()["flights"][0]
    assert "alarmState" not in flight


# ---------------------------------------------------------------------------
# WebSocket forwarder: alarm_action -> flight_update with camelCased data
# ---------------------------------------------------------------------------

class FakeWebSocket:
    client_state = WebSocketState.CONNECTED

    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


async def test_forwarder_turns_alarm_action_into_flight_update():
    mgr = MonitorConnectionManager()
    ws = FakeWebSocket()
    mgr._connections[SLUG] = [ws]
    mgr._client_state[id(ws)] = {FID: {"status": "5"}}

    await mgr.broadcast_event(SLUG, {
        "type": "alarm_action",
        "flarm_id": FID,
        "data": {
            "alarm_state": "acknowledged",
            "alarm_comment": "",
            "alarm_set_by": TENANT_NAME,
            "alarm_set_at": "2026-09-25T10:00:00Z",
        },
        "message": f"Alarm quittiert ({TENANT_NAME})",
    })

    assert len(ws.sent) == 1
    msg = ws.sent[0]
    assert msg["type"] == "flight_update"
    assert msg["flarmId"] == FID
    assert msg["eventType"] == "alarm_action"
    assert msg["message"] == f"Alarm quittiert ({TENANT_NAME})"
    assert msg["d"] == {
        "alarmState": "acknowledged",
        "alarmComment": "",
        "alarmSetBy": TENANT_NAME,
        "alarmSetAt": "2026-09-25T10:00:00Z",
    }
    # the flight stays: no removal from the per-client state
    assert FID in mgr._client_state[id(ws)]
