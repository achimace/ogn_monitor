"""StateSynchronizer: the foreign-airfield / visitor columns reach PostgreSQL (fake DB)."""

from datetime import datetime, timezone

from app.tracking.flight_state import FlightState, FlightStatus
from app.tracking.state_synchronizer import StateSynchronizer


class FakeConn:
    def __init__(self, log: list):
        self.log = log

    async def execute(self, sql: str, *args):
        self.log.append((sql, args))


class FakeDb:
    def __init__(self):
        self.log: list[tuple[str, tuple]] = []

    def acquire(self):
        db = self

        class _Acq:
            async def __aenter__(self_inner):
                return FakeConn(db.log)

            async def __aexit__(self_inner, *exc):
                return False

        return _Acq()

    async def execute(self, sql: str, *args):
        self.log.append((sql, args))


def _flight(**kw) -> FlightState:
    base = dict(
        flarm_id="DDA5BA", airfield_slug="test", airfield_id=7,
        registration="D-KFGT", status=FlightStatus.LANDING,
        takeoff_time="2026-09-25T09:00:00Z", landing_time="2026-09-25T11:30:00Z",
        takeoff_airfield="Heimat", landing_airfield="Unterwoessen Airfield (EDPU)",
        landing_type="foreign", is_visitor=False,
    )
    base.update(kw)
    return FlightState(**base)


def _params_of(sql: str) -> int:
    """Highest $n placeholder in the SQL text."""
    import re
    return max(int(m) for m in re.findall(r"\$(\d+)", sql))


async def test_bulk_sync_writes_new_columns(monkeypatch):
    db = FakeDb()
    sync = StateSynchronizer()
    import app.tracking.state_synchronizer as mod
    monkeypatch.setattr(mod, "get_db", lambda: db)
    await sync._bulk_sync([_flight()])
    assert len(db.log) == 1
    sql, args = db.log[0]
    assert "takeoff_airfield, landing_airfield, landing_type" in sql
    assert "is_visitor" in sql
    assert "takeoff_airfield = EXCLUDED.takeoff_airfield" in sql
    assert _params_of(sql) == len(args) == 36
    assert args[32:36] == ("Heimat", "Unterwoessen Airfield (EDPU)", "foreign", False)
    # No literal values in the SQL
    assert "Heimat" not in sql


async def test_flight_log_uses_flight_landing_type_and_airfields():
    db = FakeDb()
    sync = StateSynchronizer()
    await sync._write_flight_log(db, _flight(), FlightStatus.LANDING)
    sql, args = db.log[0]
    assert "takeoff_airfield, landing_airfield, is_visitor" in sql
    assert _params_of(sql) == len(args) == 27
    assert args[10] == "foreign"                       # landing_type from the flight
    assert args[11] is not None and args[12] is not None  # coordinates for non-home
    assert args[24:27] == ("Heimat", "Unterwoessen Airfield (EDPU)", False)
    assert "COALESCE(EXCLUDED.takeoff_airfield" in sql


async def test_flight_log_falls_back_to_status_mapping_without_landing_type():
    db = FakeDb()
    sync = StateSynchronizer()
    await sync._write_flight_log(
        db, _flight(landing_type="", landing_airfield=""), FlightStatus.OUTLANDING
    )
    _, args = db.log[0]
    assert args[10] == "outlanding"
    await sync._write_flight_log(
        db, _flight(landing_type="", landing_airfield=""), FlightStatus.LANDING
    )
    assert db.log[1][1][10] == "home"
    assert db.log[1][1][11] is None                    # no coordinates for home


async def test_visitor_without_takeoff_time_is_logged_from_visitor_since():
    db = FakeDb()
    sync = StateSynchronizer()
    visitor = _flight(
        takeoff_time="", takeoff_airfield="unbekannt", is_visitor=True,
        visitor_since="2026-09-25T11:10:00Z", landing_type="home",
        landing_airfield="Heimat",
    )
    await sync._write_flight_log(db, visitor, FlightStatus.LANDING)
    _, args = db.log[0]
    assert args[5] == datetime(2026, 9, 25, 11, 10, tzinfo=timezone.utc)
    assert args[24:27] == ("unbekannt", "Heimat", True)
    # Neither takeoff nor visitor_since: the landing time (NOT NULL column)
    await sync._write_flight_log(
        db, _flight(takeoff_time="", visitor_since="", is_visitor=True), FlightStatus.LANDING
    )
    assert db.log[1][1][5] == datetime(2026, 9, 25, 11, 30, tzinfo=timezone.utc)


async def test_non_visitor_without_takeoff_time_is_not_logged():
    """The visitor_since / landing_time fallback is for visitors only: a
    non-visitor row with takeoff_time == landing_time must never be written."""
    db = FakeDb()
    sync = StateSynchronizer()
    for kw in (
        dict(takeoff_time="", visitor_since="2026-09-25T11:10:00Z"),
        dict(takeoff_time="", visitor_since=""),
        dict(takeoff_time="", landing_type="outlanding", landing_airfield=""),
    ):
        await sync._write_flight_log(db, _flight(is_visitor=False, **kw), FlightStatus.LANDING)
    assert db.log == []
    # Sanity: with a takeoff time the same flight is written normally
    await sync._write_flight_log(db, _flight(is_visitor=False), FlightStatus.LANDING)
    assert len(db.log) == 1
    for _, args in db.log:
        assert args[26] is True or args[5] != args[6]
