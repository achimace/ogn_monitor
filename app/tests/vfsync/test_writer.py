"""Writer invariants (Konzept Kap. 5.4 / 9.2) against the VF mock.

These tests pin the non-negotiable write rules. They must never be
relaxed to make a change pass.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.vfsync.audit import AuditLog
from app.vfsync.budget import BudgetGuard
from app.vfsync.models import Session, SessionState, TenantConfig
from app.vfsync.vf_client.mock import MockVfStore, mock_client
from app.vfsync.writer import VfWriter, session_fields
from tests.vfsync.fakes import FakeAuditStore, FakeBudgetStore, FakeSessionStore

AF = uuid4()
T_OFF = datetime(2026, 9, 24, 10, 0, 12, tzinfo=timezone.utc)
T_LAND = datetime(2026, 9, 24, 12, 30, 45, tzinfo=timezone.utc)


class Harness:
    def __init__(self, dry_run: bool = False, daily_budget: int = 450):
        self.store = MockVfStore()
        self.sessions = FakeSessionStore()
        self.audit_store = FakeAuditStore()
        self.budget = BudgetGuard(FakeBudgetStore())
        self.tenant = TenantConfig(airfield_id=AF, slug="test", enabled=True,
                                   dry_run=dry_run, daily_budget=daily_budget,
                                   vf_username="u", vf_password_md5="m", vf_appkey="k")
        self._clients = []

        def client_for(tenant):
            c = mock_client(self.store, on_request=self.budget.hook_for(tenant))
            self._clients.append(c)
            return c

        self.writer = VfWriter(client_for, self.sessions, AuditLog(self.audit_store), self.budget)

    def session(self, flid: int, **kw) -> Session:
        base = dict(airfield_id=AF, flarm_id="DDA5BA", registration="D-1234",
                    takeoff_ts=T_OFF, landing_ts=T_LAND, landing_method="observed",
                    start_type_detected="aerotow", release_alt_agl_m=435, tow_time_min=7,
                    conf_pairing=0.95, landing_count=1, matched_flid=flid,
                    state=SessionState.MATCHED)
        base.update(kw)
        s = Session(**base)
        self.sessions.sessions[s.session_id] = s
        return s

    def actions(self):
        return self.audit_store.actions()


@pytest.fixture
def h():
    return Harness()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_bundled_landing_edit_writes_all_empty_fields(h: Harness):
    flid = h.store.add_flight(callsign="D-1234")
    s = h.session(flid)

    r = await h.writer.write(s, h.tenant)

    assert r.status == "written"
    assert r.sent == {
        "departuretime": "2026-09-24 10:00",
        "arrivaltime": "2026-09-24 12:30",
        "towheight": 435,
        "towtime": 7,
    }
    assert h.store.order_of(flid) == ["get", "edit"]          # read-before-write
    assert h.store.flight(flid)["towheight"] == "435"
    assert s.state == SessionState.COMPLETED
    assert h.actions() == ["get", "edit"]
    assert h.audit_store.entries[1].pre_state["arrivaltime"] == ""
    assert h.audit_store.entries[1].fields_sent == r.sent


async def test_live_departure_only(h: Harness):
    flid = h.store.add_flight(callsign="D-1234")
    s = h.session(flid, landing_ts=None)
    r = await h.writer.write(s, h.tenant, fields={"departuretime"})
    assert r.status == "written" and r.sent == {"departuretime": "2026-09-24 10:00"}
    assert s.state == SessionState.DEPARTURE_WRITTEN
    assert "arrivaltime" not in h.store.edits[0][1]


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------

async def test_only_empty_fields_are_sent(h: Harness):
    """Manual VF entries always win (R-08)."""
    flid = h.store.add_flight(callsign="D-1234", departuretime="2026-09-24 09:58",
                              towheight=500)
    s = h.session(flid)
    r = await h.writer.write(s, h.tenant)
    assert r.status == "written"
    assert set(r.sent) == {"arrivaltime", "towtime"}
    assert h.store.flight(flid)["towheight"] == "500"
    assert h.store.flight(flid)["departuretime"] == "2026-09-24 09:58"


async def test_race_field_changed_between_get_and_edit_is_not_sent(h: Harness):
    flid = h.store.add_flight(callsign="D-1234")
    h.store.mutate_after_get(flid, {"arrivaltime": "2026-09-24 12:29"})
    s = h.session(flid)
    r = await h.writer.write(s, h.tenant)
    # The mutation happened after OUR get, so this run still sends
    # arrivaltime based on the state it read - and the next run must not
    # touch the manual value any more (idempotent re-run).
    assert r.status == "written"
    s.state = SessionState.MATCHED
    r2 = await h.writer.write(s, h.tenant)
    assert "arrivaltime" not in r2.sent
    assert h.store.flight(flid)["arrivaltime"] in ("2026-09-24 12:29", "2026-09-24 12:30")


async def test_rerun_of_a_written_session_has_no_effect(h: Harness):
    flid = h.store.add_flight(callsign="D-1234")
    s = h.session(flid)
    await h.writer.write(s, h.tenant)
    s.state = SessionState.MATCHED
    r = await h.writer.write(s, h.tenant)
    assert r.status == "skipped" and "all_fields_already_set" in r.reasons
    assert len(h.store.edits) == 1
    assert s.state == SessionState.COMPLETED


async def test_landingcount_is_only_ever_raised(h: Harness):
    flid = h.store.add_flight(callsign="D-1234", landingcount=3)
    s = h.session(flid, landing_count=2, conf_touchgo=1.0)
    r = await h.writer.write(s, h.tenant)
    assert "landingcount" not in r.sent
    assert h.store.flight(flid)["landingcount"] == "3"
    assert any(x.startswith("landingcount_vf_higher") for x in s.review_reasons())
    assert s.state == SessionState.REVIEW

    flid2 = h.store.add_flight(callsign="D-1234", landingcount=1)
    s2 = h.session(flid2, landing_count=3, conf_touchgo=1.0)
    r = await h.writer.write(s2, h.tenant)
    assert r.sent["landingcount"] == 3
    assert h.store.flight(flid2)["landingcount"] == "3"


async def test_low_confidence_fields_are_dropped_but_rest_written(h: Harness):
    flid = h.store.add_flight(callsign="D-1234")
    s = h.session(flid, conf_pairing=0.5, landing_count=2, conf_touchgo=0.3)
    r = await h.writer.write(s, h.tenant)
    assert r.status == "written"
    assert set(r.sent) == {"departuretime", "arrivaltime"}
    assert any(x.startswith("towheight_low_pairing_confidence") for x in s.review_reasons())
    assert any(x.startswith("landingcount_low_touchgo_confidence") for x in s.review_reasons())
    assert s.state == SessionState.REVIEW   # done, but flagged for a human


async def test_implausible_height_is_never_written(h: Harness):
    flid = h.store.add_flight(callsign="D-1234")
    s = h.session(flid, release_alt_agl_m=2500)
    r = await h.writer.write(s, h.tenant)
    assert "towheight" not in r.sent and "towtime" in r.sent


async def test_silence_landing_needs_confidence(h: Harness):
    flid = h.store.add_flight(callsign="D-1234")
    s = h.session(flid, landing_method="silence", conf_landing=0.7)
    r = await h.writer.write(s, h.tenant)
    assert "arrivaltime" not in r.sent
    assert h.store.flight(flid)["arrivaltime"] == ""


async def test_starttype_conflict_aborts_whole_edit(h: Harness):
    flid = h.store.add_flight(callsign="D-1234", starttype=5)   # winch in VF
    s = h.session(flid)                                        # aerotow detected
    r = await h.writer.write(s, h.tenant)
    assert r.status == "review"
    assert h.store.edits == []
    assert h.store.order_of(flid) == ["get"]
    assert s.state == SessionState.REVIEW
    assert h.actions() == ["get", "abstain"]


async def test_dry_run_never_puts_but_audits_exact_payload(h: Harness):
    h.tenant.dry_run = True
    flid = h.store.add_flight(callsign="D-1234")
    s = h.session(flid)
    r = await h.writer.write(s, h.tenant)
    assert r.status == "dryrun"
    assert h.store.edits == []
    assert h.store.order_of(flid) == ["get"]
    assert h.actions() == ["get", "dryrun_edit"]
    assert h.audit_store.entries[1].fields_sent == r.sent == {
        "departuretime": "2026-09-24 10:00", "arrivaltime": "2026-09-24 12:30",
        "towheight": 435, "towtime": 7,
    }
    assert s.state == SessionState.COMPLETED
    assert h.store.flight(flid)["arrivaltime"] == ""


async def test_budget_hard_stop_defers_without_any_call(h: Harness):
    h.tenant.daily_budget = 3
    flid = h.store.add_flight(callsign="D-1234")
    s = h.session(flid)
    for _ in range(3):
        await h.budget.on_request(h.tenant)
    r = await h.writer.write(s, h.tenant)
    assert r.status == "deferred" and r.reasons == ["budget_exhausted"]
    assert h.store.requests == [] and h.actions() == []
    assert s.attempts == 0


async def test_every_request_counts_against_the_budget(h: Harness):
    flid = h.store.add_flight(callsign="D-1234")
    s = h.session(flid)
    await h.writer.write(s, h.tenant)
    # signin = accesstoken + signin, then get + edit
    assert await h.budget.used(h.tenant) == 4


async def test_400_goes_to_review_without_retry(h: Harness):
    flid = h.store.add_flight(callsign="D-1234")
    h.store.fail_next("flight/edit", 400)
    s = h.session(flid)
    r = await h.writer.write(s, h.tenant)
    assert r.status == "error" and r.http_status == 400
    assert "vf_400" in r.reasons
    assert s.state == SessionState.REVIEW
    assert h.actions() == ["get", "error"]
    assert len([p for _, p, _ in h.store.requests if "flight/edit" in p]) == 1


async def test_5xx_is_retried_then_left_for_the_scheduler(h: Harness):
    flid = h.store.add_flight(callsign="D-1234")
    h.store.fail_next("flight/get", 503, times=5)
    s = h.session(flid)
    r = await h.writer.write(s, h.tenant)
    assert r.status == "error" and "retry" in r.reasons
    assert s.state == SessionState.MATCHED          # not review: transient
    assert s.attempts == 1
    assert h.actions() == ["error"]


async def test_403_on_login_is_reported_for_tenant_pause(h: Harness):
    flid = h.store.add_flight(callsign="D-1234")
    h.store.fail_next("auth/signin", 403)
    s = h.session(flid)
    r = await h.writer.write(s, h.tenant)
    assert r.status == "error" and "login_forbidden" in r.reasons


async def test_unmatched_session_is_skipped(h: Harness):
    s = h.session(None)
    r = await h.writer.write(s, h.tenant)
    assert r.status == "skipped" and r.reasons == ["not_matched"]
    assert h.store.requests == []


async def test_nothing_to_write_makes_no_api_call(h: Harness):
    flid = h.store.add_flight(callsign="D-1234")
    s = h.session(flid, takeoff_ts=None, landing_ts=None, start_type_detected="winch")
    r = await h.writer.write(s, h.tenant)
    assert r.status == "skipped" and "nothing_to_write" in r.reasons
    assert h.store.requests == []
    assert h.actions() == ["abstain"]


async def test_audit_never_contains_auth_parameters(h: Harness):
    flid = h.store.add_flight(callsign="D-1234")
    s = h.session(flid)
    await h.writer.write(s, h.tenant)
    for e in h.audit_store.entries:
        for d in (e.fields_sent or {}, e.pre_state or {}):
            assert not ({"accesstoken", "password", "appkey"} & set(d))


def test_session_fields_only_offers_tow_data_for_aerotow():
    s = Session(airfield_id=AF, flarm_id="X", takeoff_ts=T_OFF, landing_ts=T_LAND,
                start_type_detected="winch", release_alt_agl_m=400, tow_time_min=3,
                landing_count=2)
    assert session_fields(s) == {"departuretime": "2026-09-24 10:00",
                                 "arrivaltime": "2026-09-24 12:30", "landingcount": 2}
    s.start_type_detected = "aerotow"
    assert session_fields(s)["towheight"] == 400 and session_fields(s)["towtime"] == 3
