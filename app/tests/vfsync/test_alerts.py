"""Alert rules (R-12) and de-duplicating sink."""

from datetime import datetime, timedelta, timezone

import httpx

from app.vfsync.alerts import (
    Alert,
    AlertSink,
    MonitoringSnapshot,
    TenantSnapshot,
    evaluate,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _snap(**kw) -> MonitoringSnapshot:
    base = dict(now=NOW, last_event_ts=NOW - timedelta(minutes=5),
                worker_started_at=NOW - timedelta(hours=2),
                tenants=[TenantSnapshot(slug="ohlstadt", budget_used=10, daily_budget=450)])
    base.update(kw)
    return MonitoringSnapshot(**base)


def test_quiet_system_has_no_alerts():
    assert evaluate(_snap()) == []


def test_no_flight_events_is_not_a_fault():
    """Rainy day: no events for hours, APRS worker connected -> quiet."""
    assert evaluate(_snap(last_event_ts=None, aprs_connected=True)) == []
    assert evaluate(_snap(last_event_ts=NOW - timedelta(hours=9), aprs_connected=True)) == []
    assert evaluate(_snap(aprs_connected=None)) == []          # ogn:health unknown


def test_feed_dead_when_aprs_worker_disconnected_for_30_minutes():
    assert evaluate(_snap(aprs_connected=False, aprs_down_for=timedelta(minutes=10))) == []
    alerts = evaluate(_snap(aprs_connected=False, aprs_down_for=timedelta(minutes=31)))
    assert [a.key for a in alerts] == ["feed_dead"] and alerts[0].level == "critical"


def test_feed_dead_not_raised_without_tenants():
    assert evaluate(_snap(aprs_connected=False, aprs_down_for=timedelta(hours=5), tenants=[])) == []


def test_config_error_alert():
    alerts = evaluate(_snap(config_errors=["ohlstadt"]))
    assert [a.key for a in alerts] == ["config_error:ohlstadt"]


def test_budget_rules():
    t = TenantSnapshot(slug="x", budget_used=360, daily_budget=450)  # 80 %
    assert [a.key for a in evaluate(_snap(tenants=[t]))] == ["budget:x"]
    t.budget_used = 450
    alerts = evaluate(_snap(tenants=[t]))
    assert [a.key for a in alerts] == ["budget_stop:x"] and alerts[0].level == "critical"


def test_pending_session_write_errors_and_login():
    t = TenantSnapshot(slug="x", budget_used=0, daily_budget=450,
                       oldest_pending_age=timedelta(hours=25),
                       write_error_streak=3, login_forbidden=True)
    keys = [a.key for a in evaluate(_snap(tenants=[t]))]
    assert keys == ["pending:x", "write_errors:x", "login:x"]


async def test_sink_deduplicates_and_resends_after_repeat_window():
    clock = {"now": NOW}
    posted: list[tuple[str, bytes, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        posted.append((str(request.url), request.content, dict(request.headers)))
        return httpx.Response(200)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = AlertSink(ntfy_url="https://ntfy.example/topic", email_to="", http=http,
                     clock=lambda: clock["now"], repeat_after=timedelta(hours=6))
    a = Alert(key="budget:x", level="warning", title="t", message="m")

    assert await sink.send(a) is True
    assert await sink.send(a) is False           # de-duplicated
    clock["now"] = NOW + timedelta(hours=7)
    assert await sink.send(a) is True            # repeated after the window
    assert len(posted) == 2
    assert posted[0][2]["title"] == "t" and posted[0][2]["priority"] == "4"

    # condition disappears -> next occurrence alerts immediately
    assert await sink.send_all([]) == 0
    assert await sink.send(a) is True
    await http.aclose()


async def test_sink_failures_never_raise():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = AlertSink(ntfy_url="https://ntfy.example/topic", email_to="", http=http)
    assert await sink.send(Alert(key="k", level="critical", title="t", message="m")) is True
    assert sink.channels == ["ntfy"]
    await http.aclose()


def test_log_channel_when_nothing_configured():
    sink = AlertSink(ntfy_url="", email_to="")
    assert sink.channels == ["log"]
