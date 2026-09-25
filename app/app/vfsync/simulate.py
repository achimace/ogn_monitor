"""Publish a synthetic flight as APRS-worker events (manual testing).

    python -m app.vfsync.simulate --slug ohlstadt --registration D-1234 \
        --type aerotow --tow-reg D-ETOW --release-agl 450 --tow-minutes 7 \
        --duration-min 45 --touch-go 1 --delay 3

Mirrors what the tracking worker does for one flight (takeoff,
launch_type_detected, touch_and_go, landing, landing_final): before every
``event:{slug}`` message the flight hash ``flight:{slug}:{flarm_id}`` and
the set ``flights:{slug}`` are written, so the API (monitor reload,
REST) sees the same state as an already open WebSocket client. Uses the
production RedisWriter, so payload and key format are the real ones.

The hot state exists only for the monitor: every hash carries the marker
``simulated=1``. The APRS worker's ``recover_from_redis`` skips marked
flights, so a worker restart never adopts a simulated flight into its
state machine; the entry simply expires via its TTL (landed flights get
the tracker's extended TTL, so they stay visible like real landings). A
simulated flight therefore never reaches ``flight_status`` or
``flight_log``. Never run this against a tenant whose VF-Sync points at
the real Vereinsflieger.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

import structlog

from app.redis_client import close_redis, init_redis
from app.tracking.flight_state import FlightState, FlightStatus
from app.tracking.redis_writer import SIMULATED_FIELD, SIMULATED_VALUE, RedisWriter

log = structlog.get_logger()

# Default of airfields.landed_visible_minutes (same fallback as the worker's
# load_airfield_configs); the simulator has no DB access.
DEFAULT_LANDED_VISIBLE_MIN = 1440
# FlightTracker adds this cushion to sticky_landed_max_age_s so the cleanup
# event always wins over the raw key expiry.
LANDED_TTL_CUSHION_S = 3600


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hot_state_ttl(flight: FlightState, landed_visible_min: int) -> int | None:
    """TTL the tracker passes to ``update_flight`` for this flight stage.

    ``None`` (default hot-state TTL) while airborne; landed flights get
    ``sticky_landed_max_age_s + cushion`` so the entry survives without
    beacons until the sticky-landed cleanup.
    """
    if flight.status == FlightStatus.LANDING:
        return landed_visible_min * 60 + LANDED_TTL_CUSHION_S
    return None


def build_events(args: argparse.Namespace, now: datetime | None = None) -> list[tuple[str, FlightState, str]]:
    """Event sequence (type, flight snapshot, message) for the given flight."""
    now = now or datetime.now(timezone.utc)
    takeoff = now - timedelta(minutes=args.duration_min)
    flight = FlightState(
        flarm_id=args.flarm.upper(), airfield_slug=args.slug,
        registration=args.registration, status=FlightStatus.TAKEOFF,
        takeoff_time=_iso(takeoff), aircraft_role=args.role or "",
    )
    events: list[tuple[str, FlightState, str]] = []

    def snap(etype: str, message: str = "") -> None:
        events.append((etype, FlightState(**vars(flight)), message))

    snap("takeoff")

    flight.status = FlightStatus.FLYING
    flight.launch_type = args.type
    if args.type == "aerotow":
        flight.tow_plane_reg = args.tow_reg
        flight.tow_plane_flarm_id = "TOW001"
        flight.release_alt_agl_m = float(args.release_agl)
        flight.release_alt_m = float(args.release_agl) + 660.0
        flight.release_time = _iso(takeoff + timedelta(minutes=args.tow_minutes))
        flight.release_method = "pair_separation"
        flight.tow_duration_s = int(args.tow_minutes * 60)
        flight.pairing_confidence = args.confidence
    elif args.type == "winch":
        flight.release_alt_agl_m = 400.0
        flight.release_alt_m = 1060.0
        flight.release_time = _iso(takeoff + timedelta(seconds=45))
        flight.release_method = "winch_vs_drop"
        flight.pairing_confidence = 1.0
    else:
        flight.pairing_confidence = 1.0
    snap("launch_type_detected", f"Startart {args.type}")

    for i in range(args.touch_go):
        flight.landing_count += 1
        flight.touch_go_confidence = 1.0
        snap("touch_and_go", f"Touch & Go ({flight.landing_count}. Landung)")

    landing = now - timedelta(seconds=120)
    flight.status = FlightStatus.LANDING
    flight.landing_time = _iso(landing)
    flight.landing_method = args.landing_method
    flight.landing_confidence = 1.0 if args.landing_method == "observed" else 0.85
    snap("landing", "Landung am Heimatplatz")
    flight.landing_final = True
    snap("landing_final", f"Landung bestaetigt ({flight.landing_count} Landung(en))")
    return events


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.vfsync.simulate")
    p.add_argument("--slug", required=True, help="airfield slug (event channel)")
    p.add_argument("--registration", required=True, help="e.g. D-1234 (must match the VF callsign)")
    p.add_argument("--flarm", default="SIM001")
    p.add_argument("--type", default="aerotow", choices=["aerotow", "winch", "self", "powered", "unknown"])
    p.add_argument("--role", default="", help="glider | towplane | motorglider_sl | powered")
    p.add_argument("--tow-reg", default="D-ETOW")
    p.add_argument("--release-agl", type=int, default=450)
    p.add_argument("--tow-minutes", type=float, default=7)
    p.add_argument("--confidence", type=float, default=0.96, help="pairing confidence")
    p.add_argument("--duration-min", type=int, default=45, help="flight duration (takeoff = now - duration)")
    p.add_argument("--touch-go", type=int, default=0)
    p.add_argument("--landing-method", default="observed", choices=["observed", "silence"])
    p.add_argument("--delay", type=float, default=2.0, help="seconds between events")
    p.add_argument("--only-takeoff", action="store_true", help="stop after the takeoff event")
    p.add_argument("--landed-visible-min", type=int, default=DEFAULT_LANDED_VISIBLE_MIN,
                   help="airfield landed_visible_minutes (hot-state TTL of landed flights)")
    return p


async def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    events = build_events(args)
    if args.only_takeoff:
        events = events[:1]
    redis = await init_redis()
    writer = RedisWriter(redis)
    try:
        for i, (etype, flight, message) in enumerate(events):
            data = flight.to_redis_dict()
            data[SIMULATED_FIELD] = SIMULATED_VALUE   # never adopted by the APRS worker
            # Hot state first (like FlightTracker), then the event: a client
            # reacting to the event already finds the matching hash.
            await writer.update_flight(args.slug, flight.flarm_id, data,
                                       ttl=hot_state_ttl(flight, args.landed_visible_min))
            await writer.publish_event(args.slug, etype, flight.flarm_id,
                                       data=data, message=message)
            print(f"{etype:22} {flight.registration} landing_count={flight.landing_count}")
            if i < len(events) - 1:
                await asyncio.sleep(args.delay)
    finally:
        await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
