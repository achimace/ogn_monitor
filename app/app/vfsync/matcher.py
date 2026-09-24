"""Matching of a sync session to a Vereinsflieger flight (Konzept 4.2, R-06).

Pure function over the tenant's `flight/list/today` result. Candidate
filter in this order: callsign, time window, start-type plausibility,
at least one target field still empty. Exactly one candidate matches;
several equally plausible ones are ambiguous (no write, review).
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from app.vfsync.models import Session
from app.vfsync.vf_client.mapping import is_starttype_compatible, normalize_callsign
from app.vfsync.vf_client.models import VfFlight

DEFAULT_WINDOW_MIN = 30

MatchKind = Literal["matched", "awaiting_match", "ambiguous", "starttype_conflict"]


@dataclass
class MatchDecision:
    kind: MatchKind
    flid: int | None = None
    reason: str = ""
    # candidates dropped because their VF start type contradicts the
    # detection (R-04): the session gets a review flag even if another
    # candidate matched
    conflict_flids: list[int] = field(default_factory=list)


def _wants_field(session: Session, flight: VfFlight, field_name: str) -> bool:
    """Is this target field still writable on the VF flight?"""
    if field_name == "landingcount":
        return session.landing_count > (flight.landingcount or 1)
    return flight.is_empty(field_name)


def match_session(
    session: Session,
    flights: list[VfFlight],
    target_fields: set[str],
    window_min: int = DEFAULT_WINDOW_MIN,
) -> MatchDecision:
    """Decide which VF flight (if any) a session belongs to.

    Args:
        session: Sync session with registration / takeoff_ts / detection.
        flights: Tenant flights of the day (`flight/list/today`).
        target_fields: VF fields the caller intends to write now.
        window_min: Tolerance around the session's takeoff time.

    Returns:
        MatchDecision; `matched` carries the flid.
    """
    # Sticky: a session that was matched before stays with its flight as
    # long as it is still listed (idempotent re-runs).
    if session.matched_flid is not None:
        for f in flights:
            if f.flid == session.matched_flid:
                return MatchDecision("matched", f.flid, "previously_matched")

    reg = normalize_callsign(session.registration)
    if not reg:
        return MatchDecision("awaiting_match", reason="no_registration")

    window = timedelta(minutes=window_min)
    timed: list[VfFlight] = []      # departuretime set and inside the window
    untimed: list[VfFlight] = []    # departuretime empty
    conflicts: list[int] = []

    for f in flights:
        if normalize_callsign(f.callsign) != reg:
            continue

        dep = f.departure_dt
        if dep is not None:
            if session.takeoff_ts is None or abs(dep - session.takeoff_ts) > window:
                continue

        if not is_starttype_compatible(session.start_type_detected, f.starttype):
            conflicts.append(f.flid)
            continue

        if not any(_wants_field(session, f, name) for name in target_fields):
            continue

        (timed if dep is not None else untimed).append(f)

    pool = timed if timed else untimed
    pool.sort(key=lambda f: f.flid)

    if len(pool) == 1:
        return MatchDecision("matched", pool[0].flid,
                             "departure_time_match" if timed else "single_open_flight",
                             conflicts)
    if len(pool) > 1:
        return MatchDecision(
            "ambiguous", None,
            "ambiguous_match:" + ",".join(str(f.flid) for f in pool),
            conflicts,
        )
    if conflicts:
        return MatchDecision(
            "starttype_conflict", None,
            "starttype_conflict:" + ",".join(str(x) for x in conflicts),
            conflicts,
        )
    return MatchDecision("awaiting_match", reason="no_candidate")
