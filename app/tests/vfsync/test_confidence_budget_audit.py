"""Pure-logic units: confidence gates, budget stages/guard, audit log."""

from datetime import date, datetime, timezone
from uuid import uuid4

import pytest

from app.vfsync.audit import AuditLog, strip_auth
from app.vfsync.budget import BudgetGuard, Stage, stage_for
from app.vfsync.confidence import gate_fields
from app.vfsync.models import AuditEntry, Session, TenantConfig

AF = uuid4()
T = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)


def _aerotow_session(**kw) -> Session:
    base = dict(
        airfield_id=AF, flarm_id="DDA5BA", registration="D-1234", takeoff_ts=T,
        landing_ts=T.replace(hour=12), landing_method="observed",
        start_type_detected="aerotow", release_alt_agl_m=435, tow_time_min=7,
        conf_pairing=0.95, landing_count=1,
    )
    base.update(kw)
    return Session(**base)


ALL = {"departuretime", "arrivaltime", "towheight", "towtime", "landingcount"}


# ---------------------------------------------------------------------------
# confidence
# ---------------------------------------------------------------------------

def test_gate_passes_clean_aerotow():
    allowed, reasons = gate_fields(_aerotow_session(), ALL)
    assert allowed == {"departuretime", "arrivaltime", "towheight", "towtime"}
    assert reasons == []


def test_gate_drops_height_and_time_on_low_pairing_confidence():
    allowed, reasons = gate_fields(_aerotow_session(conf_pairing=0.7), ALL)
    assert allowed == {"departuretime", "arrivaltime"}
    assert any(r.startswith("towheight_low_pairing_confidence") for r in reasons)
    assert any(r.startswith("towtime_low_pairing_confidence") for r in reasons)


@pytest.mark.parametrize("agl", [99, 2001, 0])
def test_gate_drops_implausible_height(agl):
    allowed, reasons = gate_fields(_aerotow_session(release_alt_agl_m=agl), {"towheight", "towtime"})
    assert "towheight" not in allowed
    assert "towtime" in allowed
    assert any(r.startswith("towheight_implausible") for r in reasons)


@pytest.mark.parametrize("start_type", ["winch", "self", "powered", "unknown", "aerotow_ambiguous"])
def test_gate_never_writes_heights_for_non_aerotow(start_type):
    allowed, reasons = gate_fields(
        _aerotow_session(start_type_detected=start_type, conf_pairing=1.0), ALL
    )
    assert "towheight" not in allowed and "towtime" not in allowed
    assert reasons == []  # silent: nothing to write, not a confidence issue


def test_gate_landingcount_requires_touchgo_confidence():
    s = _aerotow_session(landing_count=2, conf_touchgo=0.6)
    allowed, reasons = gate_fields(s, {"landingcount"})
    assert allowed == set()
    assert reasons and reasons[0].startswith("landingcount_low_touchgo_confidence")

    s.conf_touchgo = 0.9
    allowed, reasons = gate_fields(s, {"landingcount"})
    assert allowed == {"landingcount"} and reasons == []

    s.landing_count = 1
    assert gate_fields(s, {"landingcount"}) == (set(), [])


def test_gate_silence_landing_needs_landing_confidence():
    s = _aerotow_session(landing_method="silence", conf_landing=0.7)
    allowed, reasons = gate_fields(s, {"arrivaltime"})
    assert allowed == set() and reasons[0].startswith("arrivaltime_silence_low_confidence")
    s.conf_landing = 0.8
    assert gate_fields(s, {"arrivaltime"}) == ({"arrivaltime"}, [])


def test_gate_missing_values_are_dropped_silently():
    s = Session(airfield_id=AF, flarm_id="X")  # nothing known yet
    assert gate_fields(s, ALL) == (set(), [])


def test_gate_rejects_unknown_field():
    allowed, reasons = gate_fields(_aerotow_session(), {"starttype"})
    assert allowed == set() and reasons == ["starttype_not_writable"]


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "used, movements, expected",
    [
        (0, 0, Stage.NORMAL),
        (269, 80, Stage.NORMAL),
        (270, 0, Stage.NO_LIVE_DEPARTURE),   # 60 %
        (100, 81, Stage.NO_LIVE_DEPARTURE),  # busy day
        (405, 0, Stage.AEROTOW_ONLY),        # 90 %
        (449, 0, Stage.AEROTOW_ONLY),
        (450, 0, Stage.HARD_STOP),
        (500, 0, Stage.HARD_STOP),
    ],
)
def test_stage_thresholds(used, movements, expected):
    assert stage_for(used, 450, movements) is expected


class FakeBudgetStore:
    def __init__(self):
        self.data: dict[tuple, int] = {}

    async def used(self, airfield_id, day):
        return self.data.get((airfield_id, day), 0)

    async def increment(self, airfield_id, day, n=1):
        self.data[(airfield_id, day)] = self.data.get((airfield_id, day), 0) + n
        return self.data[(airfield_id, day)]


def _tenant(**kw) -> TenantConfig:
    base = dict(airfield_id=AF, slug="test", enabled=True, daily_budget=450)
    base.update(kw)
    return TenantConfig(**base)


async def test_budget_guard_counts_every_request_and_stops_hard():
    store = FakeBudgetStore()
    guard = BudgetGuard(store, clock=lambda: T)
    tenant = _tenant(daily_budget=4)
    hook = guard.hook_for(tenant)

    assert await guard.reserve(tenant, 2) is True
    await hook(); await hook()
    assert await guard.used(tenant) == 2
    assert await guard.reserve(tenant, 2) is True
    await hook()
    assert await guard.reserve(tenant, 2) is False   # only 1 left
    await hook()
    assert await guard.stage(tenant) is Stage.HARD_STOP
    assert await guard.reserve(tenant, 1) is False
    assert await guard.remaining(tenant) == 0


async def test_budget_day_is_tenant_local_date():
    store = FakeBudgetStore()
    # 23:30 UTC on the 24th is already the 25th in Europe/Berlin (CEST)
    guard = BudgetGuard(store, clock=lambda: datetime(2026, 9, 24, 23, 30, tzinfo=timezone.utc))
    tenant = _tenant()
    assert guard.day_for(tenant) == date(2026, 9, 25)
    await guard.on_request(tenant)
    assert store.data == {(AF, date(2026, 9, 25)): 1}


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

class FakeAuditStore:
    def __init__(self):
        self.entries: list[AuditEntry] = []

    async def append(self, entry):
        entry.id = len(self.entries) + 1
        self.entries.append(entry)
        return entry

    async def list(self, airfield_id, session_id=None, limit=200):
        return [e for e in self.entries if e.airfield_id == airfield_id][-limit:]


async def test_audit_strips_auth_parameters_and_appends():
    store = FakeAuditStore()
    audit = AuditLog(store)
    sid = uuid4()
    e = await audit.record(
        AF, "edit", session_id=sid, flid=42,
        fields_sent={"arrivaltime": "2026-09-24 12:00", "accesstoken": "t", "appkey": "k"},
        pre_state={"arrivaltime": "", "password": "x"},
        http_status=200, detail="ok",
    )
    assert e.id == 1
    assert e.fields_sent == {"arrivaltime": "2026-09-24 12:00"}
    assert e.pre_state == {"arrivaltime": ""}
    assert (await audit.list(AF))[0].flid == 42


async def test_audit_rejects_unknown_action():
    audit = AuditLog(FakeAuditStore())
    with pytest.raises(ValueError):
        await audit.record(AF, "delete")


def test_strip_auth_none():
    assert strip_auth(None) is None
