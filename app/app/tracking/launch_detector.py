"""Launch Type Detection - Winch, Aerotow, Self-Launch, Powered.

Detects the launch method of a flight from the beacons after takeoff and,
for aerotows, tracks the glider/tow-plane pair until release.

Detection methods:
- POWERED:   a priori from the aircraft role (towplane / powered)
- WINCH:     VS above the airfield's winch threshold in the first 90 s,
             abrupt VS drop = release
- AEROTOW:   candidate scoring (distance, altitude, course, speed, role)
             over several beacons; one tow plane is assigned to at most one
             glider at a time; separation of the pair = release.
             Fallback when the glider track is gappy: the tow plane's
             maximum altitude in the climb (release_method='towplane_max').
- SELF:      role motorglider_sl / known model, no winch and no pair.

All timing is based on beacon timestamps (replayable). Results are written
to the FlightState and emitted as ``launch_type_detected`` events. Release
altitudes are provided both MSL (display) and AGL (billing).
"""

from dataclasses import dataclass, field

import structlog

from app.aprs.beacon_parser import Beacon
from app.tracking.flight_state import FlightState, FlightStatus
from app.tracking.flight_state_machine import AirfieldConfig, _iso_from_ts, _ts_from_iso
from app.tracking.geo_calc import haversine

log = structlog.get_logger()

# ---- Winch ----
WINCH_MAX_DURATION_S = 90         # Max winch launch duration
WINCH_VS_DROP_THRESHOLD = 5.0     # m/s drop for release detection

# ---- Aerotow pairing ----
AEROTOW_MAX_DISTANCE_M = 150      # Max distance between pair (rope 40-60 m)
AEROTOW_ALT_DIFF_M = 80           # Max altitude difference in pair
AEROTOW_MAX_TRACK_DIFF_DEG = 30   # Course difference that still scores well
AEROTOW_SEPARATION_DIST_M = 200   # Distance for separation detection
AEROTOW_MIN_DURATION_S = 120      # A real tow lasts at least 2 minutes
AEROTOW_MAX_DURATION_S = 1200     # Keep tracking an attached pair up to 20 min
AEROTOW_SPEED_MIN = 90            # km/h typical min tow speed
AEROTOW_SPEED_MAX = 150           # km/h typical max tow speed
AEROTOW_SPEED_HARD_MIN = 70       # km/h outside this band: not a tow plane
AEROTOW_SPEED_HARD_MAX = 170
CANDIDATE_MIN_BEACON_SCORE = 0.5  # single-beacon score needed to count
CANDIDATE_STALE_S = 30            # candidate position older than this: ignore
PAIR_MIN_BEACONS = 3              # correlated beacons before a pair is declared
PAIR_MIN_SCORE = 0.6              # average score needed to declare a pair
PAIRING_AMBIGUITY_MARGIN = 0.15   # two candidates closer than this -> ambiguous
PAIR_FULL_CONFIDENCE_BEACONS = 10 # beacons until the pairing confidence saturates
TOWPLANE_ROLE_BONUS = 0.15        # known tow plane scores higher

# ---- Tow-plane side fallback (glider track gappy) ----
TOWPLANE_DESCENT_VS_MS = -1.0     # tow plane sinking = released
TOWPLANE_DESCENT_BEACONS = 3
TOWPLANE_GLIDER_SILENCE_S = 45    # glider quiet this long while tow descends
TOWPLANE_MAX_CONFIDENCE_FACTOR = 0.85

# ---- Tie-breaker when BOTH partners lack role information ----
# After the release the tug pushes over clearly; a glider merely glides.
TIEBREAK_TUG_SINK_VS_MS = -2.5    # this much sink = the tug
TIEBREAK_VS_GAP_MS = 1.5          # ... and the partner sinks at least this much less
TIEBREAK_FRESH_S = 10             # partner position must be this recent
TIEBREAK_WINDOW_S = 30            # undecided this long after separation -> no heights

# ---- Windows ----
DETECTION_WINDOW_S = 180          # unpaired flights: final decision after 3 min
SELF_LAUNCH_EARLY_S = 60          # known self-launcher, nothing else seen

# Aircraft roles (tenant fleet / airfield config)
ROLE_TOWPLANE = "towplane"
ROLE_GLIDER = "glider"
ROLE_MOTORGLIDER = "motorglider_sl"
ROLE_POWERED = "powered"

LAUNCH_AEROTOW_AMBIGUOUS = "aerotow_ambiguous"

# FLARM aircraft category (beacon id byte) -> role fallback. Type 1 covers
# gliders AND motor gliders, so it only rules out towing, never self-launch.
FLARM_TYPE_ROLES = {
    1: ROLE_GLIDER,
    2: ROLE_TOWPLANE,
    8: ROLE_POWERED,
    9: ROLE_POWERED,
}

# Known self-launcher models (fallback when no role is configured)
SELF_LAUNCH_MODELS = {
    "arcus m", "arcus t", "ventus 2cm", "ventus 2ct", "ventus 3m", "ventus 3t",
    "dg-808", "dg-808c", "dg-1001m", "dg-1001t", "ash 26e", "eta",
    "nimbus 4dm", "nimbus 4dt", "antares 20e", "antares 23e",
    "duo discus xlt", "hph 304ms", "js3 rj",
    "sf 25", "sf25", "dimona", "super dimona", "falke",
    "g 109", "g109", "grob 109",
}


@dataclass
class _CandidateScore:
    """Accumulated pairing score of one tow-plane candidate."""
    total: float = 0.0
    count: int = 0
    last_ts: float = 0.0

    @property
    def avg(self) -> float:
        return self.total / self.count if self.count else 0.0


@dataclass
class _LaunchDetectionState:
    """Tracking state for launch type detection of a single flight."""
    flarm_id: str
    airfield_slug: str
    takeoff_ts: float             # beacon time (unix)
    beacons: list[Beacon] = field(default_factory=list)
    is_resolved: bool = False
    launch_type: str = "unknown"

    # Winch tracking
    had_winch_vs: bool = False
    max_vs: float = 0.0

    # Aerotow candidate scoring
    candidates: dict[str, _CandidateScore] = field(default_factory=dict)
    rejected: set[str] = field(default_factory=set)
    ambiguous: bool = False
    force_ambiguous: bool = False   # tug/glider undecidable -> never a height
    separated_ts: float = 0.0       # first separation beacon (tie-break window)

    # Aerotow pair tracking
    tow_plane_id: str = ""
    tow_plane_reg: str = ""
    pair_start_ts: float = 0.0
    last_paired_ts: float = 0.0
    last_paired_alt: float = 0.0
    paired_beacons: int = 0
    last_glider_ts: float = 0.0

    # Tow-plane side observations (fallback release estimate)
    tow_max_alt: float = 0.0
    tow_max_alt_ts: float = 0.0
    tow_descent_beacons: int = 0


class LaunchDetector:
    """Detects launch types from the beacons after takeoff."""

    def __init__(self):
        self._pending: dict[str, _LaunchDetectionState] = {}
        # One-to-one tow assignment: (airfield_slug, tow_id) -> glider_id
        self._tow_assignments: dict[tuple[str, str], str] = {}
        # Events generated during processing (consumed by flight tracker)
        self._pending_events: list[dict] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def drain_events(self) -> list[dict]:
        """Get and clear pending ``launch_type_detected`` events."""
        events = self._pending_events
        self._pending_events = []
        return events

    def on_takeoff(self, flight: FlightState, airfield_flights: list[FlightState],
                   config: AirfieldConfig) -> None:
        """Called when a TAKEOFF is detected. Starts launch detection."""
        state = _LaunchDetectionState(
            flarm_id=flight.flarm_id,
            airfield_slug=flight.airfield_slug,
            takeoff_ts=_ts_from_iso(flight.takeoff_time),
        )
        self._pending[flight.flarm_id] = state

        # This aircraft starts a new flight: any glider it was still
        # assigned to as tow plane belongs to an earlier tow. Close those
        # detections now, otherwise the stale assignment would block the
        # next glider and the new climb would pollute towplane_max.
        self._close_tow_assignments(flight.flarm_id, airfield_flights, config,
                                    reason="Schlepper startet erneut")

        # A priori: tow planes and powered aircraft do not get launched.
        role = self._role_of(flight, config)
        if role in (ROLE_TOWPLANE, ROLE_POWERED):
            self._resolve(flight, state, "powered", config,
                          confidence=1.0, reason=f"Rolle {role}")

    def on_beacon(self, flight: FlightState, beacon: Beacon,
                  airfield_flights: list[FlightState],
                  config: AirfieldConfig) -> None:
        """Called for every beacon of every flight at the airfield.

        Runs the glider-side analysis if this flight has a pending
        detection and the tow-plane-side analysis for every pending glider
        that is paired with this aircraft.
        """
        state = self._pending.get(flight.flarm_id)
        if state and not state.is_resolved:
            self._on_glider_beacon(flight, state, beacon, airfield_flights, config)

        self._on_tow_beacon(flight, beacon, airfield_flights, config)

    def is_pending(self, flarm_id: str) -> bool:
        """Check if launch detection is still pending for a flight."""
        state = self._pending.get(flarm_id)
        return state is not None and not state.is_resolved

    def cleanup(self, flarm_id: str) -> None:
        """Remove detection state (after landing/archive)."""
        state = self._pending.pop(flarm_id, None)
        if state and state.tow_plane_id:
            self._release_assignment(state)

    def forget_partner(self, airfield_slug: str, flarm_id: str) -> list[str]:
        """Drop an aircraft from every pending detection that refers to it.

        DDB ``tracked = N`` eviction: the aircraft must not surface as tow
        plane (id / registration) in a partner's result. Its pairing is
        cancelled, its tow-side observations are discarded and it is
        barred from being scored again; the partner's own detection
        simply continues without it.

        Returns:
            FLARM ids of the gliders that were paired with the aircraft.
        """
        affected: list[str] = []
        for state in self._pending.values():
            if state.airfield_slug != airfield_slug or state.flarm_id == flarm_id:
                continue
            if state.tow_plane_id == flarm_id:
                self._release_assignment(state)
                state.tow_plane_id = ""
                state.tow_plane_reg = ""
                state.paired_beacons = 0
                state.ambiguous = False
                state.separated_ts = 0.0
                state.tow_max_alt = 0.0
                state.tow_max_alt_ts = 0.0
                state.tow_descent_beacons = 0
                affected.append(state.flarm_id)
            state.candidates.pop(flarm_id, None)
            state.rejected.add(flarm_id)
        self._tow_assignments.pop((airfield_slug, flarm_id), None)
        return affected

    # ------------------------------------------------------------------
    # Glider side
    # ------------------------------------------------------------------

    def _on_glider_beacon(self, flight: FlightState, state: _LaunchDetectionState,
                          beacon: Beacon, airfield_flights: list[FlightState],
                          config: AirfieldConfig) -> None:
        state.beacons.append(beacon)
        state.last_glider_ts = beacon.timestamp
        ts = beacon.timestamp
        elapsed = ts - state.takeoff_ts

        # --- Winch: high VS in the first 90 s, abrupt drop = release ---
        if elapsed < WINCH_MAX_DURATION_S:
            winch_vs = config.winch_vs_threshold_ms
            if beacon.vs > winch_vs:
                state.had_winch_vs = True
                state.max_vs = max(state.max_vs, beacon.vs)

            if state.had_winch_vs and len(state.beacons) >= 2:
                prev = state.beacons[-2]
                vs_drop = prev.vs - beacon.vs
                if vs_drop > WINCH_VS_DROP_THRESHOLD and prev.vs > winch_vs * 0.7:
                    self._resolve(
                        flight, state, "winch", config,
                        release_alt=beacon.altitude,
                        release_ts=ts,
                        release_method="winch_vs_drop",
                        confidence=1.0,
                        reason=f"Windenausklinken bei {beacon.altitude:.0f}m MSL, "
                               f"VS-Drop {vs_drop:.1f}m/s",
                    )
                    return

        # --- Aerotow: score every eligible candidate on every beacon ---
        for other in airfield_flights:
            if other.flarm_id in state.rejected:
                continue
            score = self._score_candidate(beacon, flight, other, config)
            if score is None or score < CANDIDATE_MIN_BEACON_SCORE:
                continue
            cand = state.candidates.setdefault(other.flarm_id, _CandidateScore())
            cand.total += score
            cand.count += 1
            cand.last_ts = ts

        if not state.tow_plane_id:
            self._try_pair(flight, state, beacon, airfield_flights, config)

        if state.tow_plane_id:
            self._track_pair(flight, state, beacon, airfield_flights, config)
            if state.is_resolved:
                return

        # --- Windows ---
        if state.tow_plane_id:
            if elapsed > AEROTOW_MAX_DURATION_S:
                self._final_classification(flight, state, config)
        elif elapsed > DETECTION_WINDOW_S:
            self._final_classification(flight, state, config)
        elif (elapsed > SELF_LAUNCH_EARLY_S
                and not state.had_winch_vs
                and not state.candidates
                and self._is_self_launcher(flight, config)):
            self._resolve(flight, state, "self", config, confidence=1.0,
                          reason="Eigenstarter (Rolle/Modell), kein Schlepper, keine Winde")

    def _try_pair(self, flight: FlightState, state: _LaunchDetectionState,
                  beacon: Beacon, airfield_flights: list[FlightState],
                  config: AirfieldConfig) -> None:
        """Declare a pair once one candidate has enough correlated beacons."""
        ranked = sorted(
            (
                (cand.avg, fid)
                for fid, cand in state.candidates.items()
                if cand.count >= PAIR_MIN_BEACONS and cand.avg >= PAIR_MIN_SCORE
            ),
            reverse=True,
        )
        if not ranked:
            return
        best_avg, best_id = ranked[0]
        if len(ranked) > 1 and best_avg - ranked[1][0] < PAIRING_AMBIGUITY_MARGIN:
            # Two aircraft correlate equally well so far (parallel tows).
            # Track the best one for separation; whether the tow ends up
            # ambiguous is decided on the whole pairing at resolution.
            state.ambiguous = True
            log.warning(
                "aerotow_pairing_ambiguous",
                glider=flight.flarm_id,
                candidates=[fid for _, fid in ranked[:2]],
                scores=[round(s, 2) for s, _ in ranked[:2]],
            )

        tow = _find_flight(best_id, airfield_flights)
        if tow is None:
            return
        if not self._assign_tow(state, best_id, beacon.timestamp, airfield_flights, config):
            state.ambiguous = False
            return

        state.tow_plane_id = best_id
        state.tow_plane_reg = tow.registration
        state.pair_start_ts = beacon.timestamp
        state.last_paired_ts = beacon.timestamp
        state.last_paired_alt = beacon.altitude
        state.paired_beacons = 1
        state.tow_max_alt = tow.altitude_m
        state.tow_max_alt_ts = beacon.timestamp
        log.info(
            "aerotow_pair_detected",
            glider=flight.flarm_id,
            tow=best_id,
            score=round(best_avg, 2),
            ambiguous=state.ambiguous,
        )

    def _track_pair(self, flight: FlightState, state: _LaunchDetectionState,
                    beacon: Beacon, airfield_flights: list[FlightState],
                    config: AirfieldConfig) -> None:
        """Follow an assigned pair and detect the separation (release)."""
        tow = _find_flight(state.tow_plane_id, airfield_flights)
        if tow is None:
            return

        dist = haversine(beacon.lat, beacon.lon, tow.latitude, tow.longitude)
        alt_diff = abs(beacon.altitude - tow.altitude_m)

        if dist < AEROTOW_MAX_DISTANCE_M and alt_diff < AEROTOW_ALT_DIFF_M:
            # Still attached
            state.last_paired_ts = beacon.timestamp
            state.last_paired_alt = beacon.altitude
            state.paired_beacons += 1
            if tow.altitude_m > state.tow_max_alt:
                state.tow_max_alt = tow.altitude_m
                state.tow_max_alt_ts = beacon.timestamp
            return

        if dist > AEROTOW_SEPARATION_DIST_M or alt_diff > AEROTOW_ALT_DIFF_M * 2:
            tow_duration = state.last_paired_ts - state.takeoff_ts
            if tow_duration >= AEROTOW_MIN_DURATION_S:
                # Without role information both partners of a pair run a
                # detection and would resolve each other as "aerotow".
                # Tie-breaker: after the separation the tug is the one
                # that pushes over clearly while the other glides. If that
                # is not evident within a short window, neither partner
                # gets a release height (billing-safe abstention).
                if self.is_pending(tow.flarm_id):
                    verdict = self._tug_verdict(beacon, tow)
                    if verdict is None:
                        if not state.separated_ts:
                            state.separated_ts = beacon.timestamp
                        if beacon.timestamp - state.separated_ts < TIEBREAK_WINDOW_S:
                            return  # wait for more beacons
                        self._resolve_undecided_pair(flight, state, tow, config)
                        return
                    if verdict == "me_tug":
                        self._resolve(
                            flight, state, "powered", config, confidence=0.8,
                            reason=f"Sinkt nach Trennung von {tow.flarm_id}: Schlepper",
                        )
                        self._resolve_partner_as_glider(tow, flight, config)
                        return
                self._resolve(
                    flight, state, "aerotow", config,
                    release_alt=state.last_paired_alt,
                    release_ts=state.last_paired_ts,
                    release_method="pair_separation",
                    tow_plane_id=state.tow_plane_id,
                    tow_plane_reg=state.tow_plane_reg,
                    confidence=self._pair_confidence(state),
                    reason=f"Ausklinken bei {state.last_paired_alt:.0f}m MSL, "
                           f"Schleppzeit {tow_duration:.0f}s",
                )
                self._settle_partner(tow, flight.flarm_id, config)
            else:
                # Separated too early for a real tow: this was a coincidental
                # proximity (e.g. two aircraft departing close together).
                log.info(
                    "aerotow_pair_rejected",
                    glider=flight.flarm_id,
                    tow=state.tow_plane_id,
                    tow_duration_s=int(tow_duration),
                )
                state.rejected.add(state.tow_plane_id)
                state.candidates.pop(state.tow_plane_id, None)
                self._release_assignment(state)
                state.tow_plane_id = ""
                state.tow_plane_reg = ""
                state.paired_beacons = 0
                state.ambiguous = False

    # ------------------------------------------------------------------
    # Tow-plane side
    # ------------------------------------------------------------------

    def _on_tow_beacon(self, flight: FlightState, beacon: Beacon,
                       airfield_flights: list[FlightState],
                       config: AirfieldConfig) -> None:
        """Observe an aircraft that is assigned as tow plane to a pending glider.

        Keeps the tow plane's maximum climb altitude and, if the glider has
        gone quiet while the tow plane is clearly descending, resolves the
        tow with release_method='towplane_max'.
        """
        for state in list(self._pending.values()):
            if state.is_resolved or state.tow_plane_id != flight.flarm_id:
                continue

            ts = beacon.timestamp
            if ts - state.last_paired_ts > AEROTOW_MAX_DURATION_S:
                # Pairing is long over (glider silent, no descent seen):
                # do not feed this climb into the old detection.
                continue
            if beacon.vs > 0.3:
                state.tow_descent_beacons = 0
                if beacon.altitude > state.tow_max_alt:
                    state.tow_max_alt = beacon.altitude
                    state.tow_max_alt_ts = ts
            elif beacon.vs < TOWPLANE_DESCENT_VS_MS:
                state.tow_descent_beacons += 1

            glider_silent = (ts - state.last_glider_ts) > TOWPLANE_GLIDER_SILENCE_S
            tow_duration = state.tow_max_alt_ts - state.takeoff_ts
            if (state.tow_descent_beacons >= TOWPLANE_DESCENT_BEACONS
                    and glider_silent
                    and tow_duration >= AEROTOW_MIN_DURATION_S):
                glider = _find_flight(state.flarm_id, airfield_flights)
                if glider is None:
                    continue
                self._resolve(
                    glider, state, "aerotow", config,
                    release_alt=state.tow_max_alt,
                    release_ts=state.tow_max_alt_ts,
                    release_method="towplane_max",
                    tow_plane_id=state.tow_plane_id,
                    tow_plane_reg=state.tow_plane_reg,
                    confidence=self._pair_confidence(state) * TOWPLANE_MAX_CONFIDENCE_FACTOR,
                    reason=f"Schlepper sinkt, Segler stumm: Schlepper-Maximum "
                           f"{state.tow_max_alt:.0f}m MSL",
                )
                self._settle_partner(flight, glider.flarm_id, config)

    # ------------------------------------------------------------------
    # Scoring / assignment helpers
    # ------------------------------------------------------------------

    def _score_candidate(self, beacon: Beacon, flight: FlightState,
                         other: FlightState, config: AirfieldConfig) -> float | None:
        """Score how well ``other`` fits as tow plane for this beacon (0..1).

        Returns None if the candidate is ineligible.
        """
        if other.flarm_id == flight.flarm_id:
            return None
        if other.airfield_slug != flight.airfield_slug:
            return None
        if other.status not in (FlightStatus.TAKEOFF, FlightStatus.FLYING, FlightStatus.TOWING):
            return None
        if other.is_visitor:
            return None  # did not start here: cannot be this glider's tow plane
        role = self._role_of(other, config)
        if role in (ROLE_GLIDER, ROLE_MOTORGLIDER):
            return None  # gliders do not tow
        if other._last_beacon_ts and beacon.timestamp - other._last_beacon_ts > CANDIDATE_STALE_S:
            return None

        dist = haversine(beacon.lat, beacon.lon, other.latitude, other.longitude)
        if dist > AEROTOW_MAX_DISTANCE_M:
            return None
        alt_diff = abs(beacon.altitude - other.altitude_m)
        if alt_diff > AEROTOW_ALT_DIFF_M:
            return None
        speed = other.speed_kmh
        if not AEROTOW_SPEED_HARD_MIN <= speed <= AEROTOW_SPEED_HARD_MAX:
            return None
        track_diff = _track_diff(beacon.track, other.track_deg)
        if track_diff > 2 * AEROTOW_MAX_TRACK_DIFF_DEG:
            return None

        s_dist = 1.0 - dist / AEROTOW_MAX_DISTANCE_M
        s_alt = 1.0 - alt_diff / AEROTOW_ALT_DIFF_M
        s_track = max(0.0, 1.0 - track_diff / (2 * AEROTOW_MAX_TRACK_DIFF_DEG))
        s_speed = 1.0 if AEROTOW_SPEED_MIN <= speed <= AEROTOW_SPEED_MAX else 0.4

        score = 0.3 * s_dist + 0.25 * s_alt + 0.25 * s_track + 0.2 * s_speed
        if role == ROLE_TOWPLANE:
            score += TOWPLANE_ROLE_BONUS
        return min(1.0, score)

    def _assign_tow(self, state: _LaunchDetectionState, tow_id: str, now_ts: float,
                    airfield_flights: list[FlightState], config: AirfieldConfig) -> bool:
        """Enforce one tow plane <-> one glider at a time.

        Returns True if the assignment was made. On a conflict the glider
        with the higher score wins; the loser stays unclassified until its
        window ends (or a better candidate shows up). A holder whose
        pairing ended long ago (glider silent) is finalized and replaced.
        """
        key = (state.airfield_slug, tow_id)
        holder_id = self._tow_assignments.get(key)
        if holder_id and holder_id != state.flarm_id:
            holder = self._pending.get(holder_id)
            if holder and not holder.is_resolved and holder.tow_plane_id == tow_id:
                if now_ts - holder.last_paired_ts > AEROTOW_MAX_DURATION_S:
                    self._finalize_stale(holder, airfield_flights, config,
                                         reason="Zuordnung veraltet, Schlepper neu vergeben")
                else:
                    my_avg = state.candidates[tow_id].avg
                    their = holder.candidates.get(tow_id)
                    their_avg = their.avg if their else 0.0
                    if my_avg <= their_avg:
                        log.debug(
                            "aerotow_pair_conflict_lost",
                            glider=state.flarm_id,
                            tow=tow_id,
                            holder=holder_id,
                        )
                        return False
                    log.info(
                        "aerotow_pair_conflict_won",
                        glider=state.flarm_id,
                        tow=tow_id,
                        previous=holder_id,
                    )
                    holder.tow_plane_id = ""
                    holder.tow_plane_reg = ""
                    holder.paired_beacons = 0
        self._tow_assignments[key] = state.flarm_id
        return True

    def _release_assignment(self, state: _LaunchDetectionState) -> None:
        key = (state.airfield_slug, state.tow_plane_id)
        if self._tow_assignments.get(key) == state.flarm_id:
            del self._tow_assignments[key]

    def _close_tow_assignments(self, tow_id: str, airfield_flights: list[FlightState],
                               config: AirfieldConfig, reason: str) -> None:
        """Finalize every pending glider still assigned to this tow plane."""
        for (slug, tid), glider_id in list(self._tow_assignments.items()):
            if tid != tow_id:
                continue
            holder = self._pending.get(glider_id)
            if holder and not holder.is_resolved:
                self._finalize_stale(holder, airfield_flights, config, reason)
            else:
                del self._tow_assignments[(slug, tid)]

    def _finalize_stale(self, holder: _LaunchDetectionState,
                        airfield_flights: list[FlightState],
                        config: AirfieldConfig, reason: str) -> None:
        """Close a detection whose pairing is over but never resolved."""
        glider = _find_flight(holder.flarm_id, airfield_flights)
        log.info(
            "aerotow_pairing_stale",
            glider=holder.flarm_id,
            tow=holder.tow_plane_id,
            reason=reason,
        )
        if glider is None:
            self._release_assignment(holder)
            self._pending.pop(holder.flarm_id, None)
            return
        self._final_classification(glider, holder, config)

    @staticmethod
    def _tug_verdict(beacon: Beacon, partner: FlightState) -> str | None:
        """Decide who is the tug from the vertical speeds after separation.

        Returns "me_tug", "me_glider" or None (not evident / stale data).
        """
        if not partner._last_beacon_ts:
            return None
        if beacon.timestamp - partner._last_beacon_ts > TIEBREAK_FRESH_S:
            return None
        pvs = partner.vertical_speed_ms
        if beacon.vs <= TIEBREAK_TUG_SINK_VS_MS and pvs >= beacon.vs + TIEBREAK_VS_GAP_MS:
            return "me_tug"
        if pvs <= TIEBREAK_TUG_SINK_VS_MS and beacon.vs >= pvs + TIEBREAK_VS_GAP_MS:
            return "me_glider"
        return None

    def _resolve_partner_as_glider(self, glider: FlightState, tug: FlightState,
                                   config: AirfieldConfig) -> None:
        """This flight turned out to be the tug: close the partner's
        detection as the towed glider right away (it would otherwise pair
        the tug again and resolve with the tug's data)."""
        gstate = self._pending.get(glider.flarm_id)
        if gstate is None or gstate.is_resolved:
            return
        if gstate.tow_plane_id != tug.flarm_id or not gstate.last_paired_ts:
            self._final_classification(glider, gstate, config)
            return
        tow_duration = gstate.last_paired_ts - gstate.takeoff_ts
        self._resolve(
            glider, gstate, "aerotow", config,
            release_alt=gstate.last_paired_alt,
            release_ts=gstate.last_paired_ts,
            release_method="pair_separation",
            tow_plane_id=tug.flarm_id,
            tow_plane_reg=tug.registration,
            confidence=self._pair_confidence(gstate),
            reason=f"Ausklinken bei {gstate.last_paired_alt:.0f}m MSL, "
                   f"Schleppzeit {tow_duration:.0f}s (Schlepper sinkt)",
        )

    def _resolve_undecided_pair(self, flight: FlightState, state: _LaunchDetectionState,
                                partner: FlightState, config: AirfieldConfig) -> None:
        """Neither partner can be told apart: no release height for either."""
        log.warning(
            "aerotow_tug_undecidable",
            a=flight.flarm_id,
            b=partner.flarm_id,
        )
        state.force_ambiguous = True
        self._resolve(
            flight, state, "aerotow", config,
            tow_plane_id=state.tow_plane_id,
            tow_plane_reg=state.tow_plane_reg,
            confidence=self._pair_confidence(state),
            reason=f"Paar mit {partner.flarm_id} ohne Rolleninfo: Schlepper nicht bestimmbar",
        )
        pstate = self._pending.get(partner.flarm_id)
        if pstate is not None and not pstate.is_resolved:
            pstate.force_ambiguous = True
            self._resolve(
                partner, pstate, "aerotow", config,
                tow_plane_id=pstate.tow_plane_id,
                tow_plane_reg=pstate.tow_plane_reg,
                confidence=self._pair_confidence(pstate),
                reason=f"Paar mit {flight.flarm_id} ohne Rolleninfo: Schlepper nicht bestimmbar",
            )

    def _settle_partner(self, tow: FlightState, glider_id: str,
                        config: AirfieldConfig) -> None:
        """The partner of a resolved aerotow is the tow plane.

        If it still runs its own detection (no role information), resolve
        it as powered so it can never come out as a towed glider itself.
        """
        tstate = self._pending.get(tow.flarm_id)
        if tstate is None or tstate.is_resolved:
            return
        self._resolve(
            tow, tstate, "powered", config, confidence=0.8,
            reason=f"Schlepper von {glider_id} (Paar aufgeloest)",
        )

    @staticmethod
    def _is_ambiguous(state: _LaunchDetectionState) -> bool:
        """Is another candidate about as well correlated as the tow plane?

        Evaluated on the whole pairing (not only its first beacons) so two
        tows that merely departed close together are not ambiguous once
        their tracks diverged.
        """
        if state.force_ambiguous:
            return True
        tow = state.candidates.get(state.tow_plane_id)
        if tow is None:
            return state.ambiguous
        min_count = max(PAIR_MIN_BEACONS, tow.count // 2)
        for fid, cand in state.candidates.items():
            if fid == state.tow_plane_id or cand.count < min_count:
                continue
            if tow.avg - cand.avg < PAIRING_AMBIGUITY_MARGIN:
                return True
        return False

    def _pair_confidence(self, state: _LaunchDetectionState) -> float:
        cand = state.candidates.get(state.tow_plane_id)
        avg = cand.avg if cand else 0.0
        saturation = min(1.0, state.paired_beacons / PAIR_FULL_CONFIDENCE_BEACONS)
        conf = avg * saturation
        if self._is_ambiguous(state):
            conf *= 0.5
        return round(min(1.0, conf), 2)

    # ------------------------------------------------------------------
    # Classification / resolution
    # ------------------------------------------------------------------

    def _final_classification(self, flight: FlightState, state: _LaunchDetectionState,
                              config: AirfieldConfig) -> None:
        """Final launch type decision after the detection window expires."""
        if state.had_winch_vs and not state.tow_plane_id:
            # No clean VS drop seen: take the highest point of the launch
            climb = [b for b in state.beacons
                     if b.timestamp - state.takeoff_ts <= WINCH_MAX_DURATION_S]
            top = max(climb, key=lambda b: b.altitude) if climb else None
            self._resolve(
                flight, state, "winch", config,
                release_alt=top.altitude if top else 0.0,
                release_ts=top.timestamp if top else 0.0,
                release_method="winch_profile" if top else "",
                confidence=0.8,
                reason=f"Windenstart-Profil (max VS {state.max_vs:.1f}m/s)",
            )
        elif state.tow_plane_id:
            if state.tow_max_alt:
                self._resolve(
                    flight, state, "aerotow", config,
                    release_alt=state.tow_max_alt,
                    release_ts=state.tow_max_alt_ts,
                    release_method="towplane_max",
                    tow_plane_id=state.tow_plane_id,
                    tow_plane_reg=state.tow_plane_reg,
                    confidence=self._pair_confidence(state) * TOWPLANE_MAX_CONFIDENCE_FACTOR,
                    reason="F-Schlepp ohne beobachtete Trennung: Schlepper-Maximum",
                )
            else:
                self._resolve(
                    flight, state, "aerotow", config,
                    tow_plane_id=state.tow_plane_id,
                    tow_plane_reg=state.tow_plane_reg,
                    confidence=self._pair_confidence(state),
                    reason="F-Schlepp erkannt (Paar-Tracking), keine Ausklinkhoehe",
                )
        elif self._is_self_launcher(flight, config):
            self._resolve(flight, state, "self", config, confidence=1.0,
                          reason="Eigenstarter (Rolle/Modell)")
        else:
            self._resolve(flight, state, "unknown", config,
                          reason="Startart nicht erkannt")

    def _resolve(self, flight: FlightState, state: _LaunchDetectionState,
                 launch_type: str, config: AirfieldConfig,
                 release_alt: float = 0.0,
                 release_ts: float = 0.0,
                 release_method: str = "",
                 tow_plane_id: str = "",
                 tow_plane_reg: str = "",
                 confidence: float = 0.0,
                 reason: str = "") -> None:
        """Finalize launch type detection and emit the event."""
        state.is_resolved = True
        if state.tow_plane_id:
            self._release_assignment(state)

        if launch_type == "aerotow" and self._is_ambiguous(state):
            # Two candidates were indistinguishable: never report a height
            launch_type = LAUNCH_AEROTOW_AMBIGUOUS
            release_alt = 0.0
            release_ts = 0.0
            release_method = ""
        state.launch_type = launch_type

        flight.launch_type = launch_type
        flight.pairing_confidence = round(confidence, 2)
        if release_alt:
            flight.release_alt_m = release_alt
            flight.release_alt_agl_m = release_alt - config.elevation_m
        if release_ts:
            flight.release_time = _iso_from_ts(release_ts)
            if launch_type == "aerotow":
                flight.tow_duration_s = max(0, int(release_ts - state.takeoff_ts))
        if release_method:
            flight.release_method = release_method
        if tow_plane_id:
            flight.tow_plane_flarm_id = tow_plane_id
        if tow_plane_reg:
            flight.tow_plane_reg = tow_plane_reg

        log.info(
            "launch_type_detected",
            flarm_id=flight.flarm_id,
            launch_type=launch_type,
            release_alt_agl_m=round(flight.release_alt_agl_m),
            release_method=flight.release_method,
            tow_duration_s=flight.tow_duration_s,
            confidence=flight.pairing_confidence,
            reason=reason,
        )
        self._pending_events.append({
            "airfield_slug": flight.airfield_slug,
            "event_type": "launch_type_detected",
            "flarm_id": flight.flarm_id,
            "flight": flight,
            "message": reason,
        })

    # ------------------------------------------------------------------
    # Role helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _role_of(flight: FlightState, config: AirfieldConfig) -> str:
        """Aircraft role, most reliable source first.

        1. explicit role from the tenant fleet
        2. airfield tow-plane list
        3. known self-launcher model
        4. FLARM aircraft category from the beacon
        """
        if flight.aircraft_role:
            return flight.aircraft_role
        if config.tow_plane_flarm_ids and flight.flarm_id in config.tow_plane_flarm_ids:
            return ROLE_TOWPLANE
        if _is_known_self_launcher(flight):
            return ROLE_MOTORGLIDER
        return FLARM_TYPE_ROLES.get(flight.flarm_aircraft_type, "")

    def _is_self_launcher(self, flight: FlightState, config: AirfieldConfig) -> bool:
        return self._role_of(flight, config) == ROLE_MOTORGLIDER


def _is_known_self_launcher(flight: FlightState) -> bool:
    """Check if the aircraft model is a known self-launcher."""
    if not flight.aircraft_model:
        return False
    model_lower = flight.aircraft_model.lower().strip()
    return model_lower in SELF_LAUNCH_MODELS


def _track_diff(a: float, b: float) -> float:
    """Smallest angular difference between two courses (0..180)."""
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def _find_flight(flarm_id: str, flights: list[FlightState]) -> FlightState | None:
    """Find a flight by FLARM ID in the active flights list."""
    for f in flights:
        if f.flarm_id == flarm_id:
            return f
    return None
