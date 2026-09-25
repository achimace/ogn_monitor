"""Flight log API: the foreign-airfield / visitor fields are returned (list + CSV)."""

from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from app.api import flight_log as flight_log_api
from app.dependencies import get_current_user

TENANT = UUID("11111111-1111-1111-1111-111111111111")
AIRFIELD_ID = UUID("33333333-3333-3333-3333-333333333333")

ROW = {
    "id": UUID("44444444-4444-4444-4444-444444444444"),
    "flarm_id": "DDA5BA",
    "registration": "D-KFGT",
    "competition_sign": "GT",
    "aircraft_model": "Ventus",
    "takeoff_time": datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc),
    "landing_time": datetime(2026, 9, 25, 11, 30, tzinfo=timezone.utc),
    "flight_duration_s": 9000,
    "max_altitude_m": 2100,
    "max_distance_m": 45200,
    "launch_type": "self",
    "landing_type": "foreign",
    "tow_plane_registration": None,
    "release_altitude_m": None,
    "signal_loss_scenario": None,
    "takeoff_airfield": "Heimat",
    "landing_airfield": "Unterwoessen Airfield (EDPU)",
    "is_visitor": False,
}


class FakeDb:
    def __init__(self):
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.queries.append((sql, args))
        if "FROM airfields" in sql:
            return [{"id": AIRFIELD_ID}]
        assert "FROM flight_log" in sql
        # Only columns the SELECT asks for (as asyncpg would)
        cols = sql.split("SELECT", 1)[1].split("FROM", 1)[0]
        wanted = [c.strip().split(".")[-1] for c in cols.replace("\n", " ").split(",")]
        return [{k: ROW[k] for k in wanted}]

    async def fetchval(self, sql: str, *args):
        self.queries.append((sql, args))
        return 1


@pytest.fixture
def fake_db():
    return FakeDb()


@pytest.fixture
async def client(fake_db, monkeypatch):
    monkeypatch.setattr(flight_log_api, "get_db", lambda: fake_db)
    app = FastAPI()
    app.include_router(flight_log_api.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "tenant_id": TENANT, "email": "max@example.invalid"
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        yield c


async def test_list_returns_airfield_fields(client, fake_db):
    resp = await client.get("/api/flight-log/", params={"date": "2026-09-25"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["takeoffAirfield"] == "Heimat"
    assert item["landingAirfield"] == "Unterwoessen Airfield (EDPU)"
    assert item["landingType"] == "foreign"
    assert item["isVisitor"] is False
    # Existing snake_case keys are untouched
    assert item["end_status"] == "foreign"
    assert item["landing_type"] == "foreign"
    assert item["takeoff_airfield"] == "Heimat"
    # Date filter passed as a real date, tenant airfields parametrised
    sql, args = fake_db.queries[-1]
    assert "takeoff_airfield" in sql and "is_visitor" in sql
    assert args[0] == AIRFIELD_ID
    assert str(args[1]) == "2026-09-25"


async def test_csv_export_has_start_and_landing_place(client):
    resp = await client.get("/api/flight-log/export/csv", params={"date": "2026-09-25"})
    assert resp.status_code == 200
    lines = resp.text.strip().splitlines()
    header = lines[0].split(";")
    assert header[-2:] == ["Startort", "Landeort"]
    row = lines[1].split(";")
    assert row[-2:] == ["Heimat", "Unterwoessen Airfield (EDPU)"]
    assert row[1] == "D-KFGT" and row[10] == "foreign"
