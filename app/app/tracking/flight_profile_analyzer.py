"""Flight Profile Analyzer - classifies flight-end scenarios on signal loss.

4 Scenarios:
1. DIVERTED: Controlled descent to nearby airport -> LOW severity
2. OUTLANDED: Controlled descent, no airport nearby -> MEDIUM severity
3. EMERGENCY: Abnormal profile (high sink, spin, dive) -> CRITICAL severity
4. SIGNAL_LOST: Normal flight, likely OGN coverage gap -> LOW severity

Uses the FlightProfileBuffer data (last ~10 min of beacons).
"""

from dataclasses import dataclass

import structlog

from app.tracking.flight_profile_buffer import ProfilePoint

log = structlog.get_logger()

# Detection thresholds
CRITICAL_SINK_RATE = -5.0         # m/s - emergency sink
RAPID_ALT_LOSS_M = 200            # meters lost in < 30s
COURSE_DEVIATION_DEG = 90         # degrees change in < 10s
NORMAL_VS_MIN = -3.0              # Normal approach min VS
NORMAL_VS_MAX = -0.3              # Normal approach max VS
CONTROLLED_SPEED_MAX = 120.0      # km/h max for controlled approach
LOW_ALT_BUFFER_M = 150            # meters above estimated ground
HIGH_ALT_THRESHOLD_M = 1500       # "high altitude" for signal loss
COURSE_STABLE_THRESHOLD = 30.0    # max variation for "stable course"
DIVE_SPEED_THRESHOLD = 150.0      # km/h
DIVE_SINK_THRESHOLD = -3.0        # m/s combined with high speed
VS_ACCELERATION_THRESHOLD = -0.5  # m/s per second (worsening sink)


@dataclass
class FlightEndClassification:
    """Result of flight profile analysis."""
    scenario: str        # DIVERTED / OUTLANDED / EMERGENCY / SIGNAL_LOST / UNKNOWN
    severity: str        # LOW / MEDIUM / HIGH / CRITICAL
    message: str
    indicators: list[str] | None = None
    nearest_airport: str = ""
    nearest_airport_dist_m: float = 0.0


def analyze_profile(
    beacons: list[ProfilePoint],
    ground_elevation_m: float = 300.0,
    nearest_airport_name: str = "",
    nearest_airport_dist_m: float = float("inf"),
    nearest_airport_elevation_m: float = 0.0,
) -> FlightEndClassification:
    """Analyze a flight's profile buffer to classify the end scenario.

    Args:
        beacons: Profile buffer points (last ~10 min).
        ground_elevation_m: Estimated ground level at last position.
        nearest_airport_name: Name of nearest airport (if any within range).
        nearest_airport_dist_m: Distance to nearest airport in meters.
        nearest_airport_elevation_m: Elevation of nearest airport.

    Returns:
        FlightEndClassification with scenario, severity and message.
    """
    if len(beacons) < 3:
        return FlightEndClassification(
            scenario="UNKNOWN",
            severity="HIGH",
            message="Unzureichende Daten fuer Profilanalyse",
        )

    last = beacons[-1]
    profile = _compute_profile_metrics(beacons)

    # Step 1: Check for abnormal profile (EMERGENCY)
    abnormal_flags = _detect_abnormal(profile, beacons)
    if abnormal_flags:
        return FlightEndClassification(
            scenario="EMERGENCY",
            severity="CRITICAL",
            message=(
                f"!!! NOTFALL-VERDACHT !!! Abnormales Flugprofil. "
                f"Letzte VS: {last.vs:+.1f} m/s, "
                f"Hoehe: {last.alt:.0f}m. "
                f"Indikatoren: {', '.join(abnormal_flags)}"
            ),
            indicators=abnormal_flags,
        )

    # Step 2: Controlled descent to nearby airport (DIVERTED)
    is_controlled = _is_controlled_descent(profile)
    if nearest_airport_dist_m < 2000 and (is_controlled or last.speed < 80):
        return FlightEndClassification(
            scenario="DIVERTED",
            severity="LOW",
            message=(
                f"Wahrscheinlich gelandet bei {nearest_airport_name}, "
                f"{nearest_airport_dist_m/1000:.1f}km entfernt"
            ),
            nearest_airport=nearest_airport_name,
            nearest_airport_dist_m=nearest_airport_dist_m,
        )

    # Step 3: Controlled descent, no airport (OUTLANDED)
    near_ground = last.alt < (ground_elevation_m + LOW_ALT_BUFFER_M)
    if is_controlled or (near_ground and last.speed < 80):
        return FlightEndClassification(
            scenario="OUTLANDED",
            severity="MEDIUM",
            message=(
                f"Wahrscheinlich aussengelandet. "
                f"Position: {last.lat:.4f}°N {last.lon:.4f}°E, "
                f"Hoehe: {last.alt:.0f}m"
            ),
        )

    # Step 4: Normal flight, high altitude (SIGNAL_LOST - coverage gap)
    if (last.alt > HIGH_ALT_THRESHOLD_M
            and not abnormal_flags
            and profile["course_stable"]):
        return FlightEndClassification(
            scenario="SIGNAL_LOST",
            severity="LOW",
            message=(
                f"Signal verloren (wahrscheinlich OGN-Abdeckung). "
                f"Letzter Kontakt: {last.alt:.0f}m, normaler Flug"
            ),
        )

    # Fallback: Unknown
    return FlightEndClassification(
        scenario="UNKNOWN",
        severity="HIGH",
        message=(
            f"Signalverlust - Szenario unklar. "
            f"Letzte Position: {last.lat:.4f}°N {last.lon:.4f}°E, "
            f"{last.alt:.0f}m, {last.speed:.0f}km/h"
        ),
    )


def _compute_profile_metrics(beacons: list[ProfilePoint]) -> dict:
    """Compute derived metrics from the beacon history."""
    last = beacons[-1]

    # Altitude loss in last 30 seconds
    alt_loss_30s = 0.0
    cutoff_30 = last.timestamp - 30
    for b in reversed(beacons):
        if b.timestamp <= cutoff_30:
            alt_loss_30s = b.alt - last.alt
            break

    # Average VS last 60 seconds
    cutoff_60 = last.timestamp - 60
    vs_points = [b.vs for b in beacons if b.timestamp >= cutoff_60]
    avg_vs_60s = sum(vs_points) / len(vs_points) if vs_points else 0.0

    # Max course change in 10 seconds
    max_course_change = 0.0
    cutoff_10 = last.timestamp - 10
    recent = [b for b in beacons if b.timestamp >= cutoff_10]
    for i in range(1, len(recent)):
        delta = abs(recent[i].track - recent[i - 1].track)
        if delta > 180:
            delta = 360 - delta
        max_course_change = max(max_course_change, delta)

    # Course stability (variance in last 60s)
    tracks_60 = [b.track for b in beacons if b.timestamp >= cutoff_60]
    course_stable = True
    if len(tracks_60) >= 2:
        track_diffs = []
        for i in range(1, len(tracks_60)):
            d = abs(tracks_60[i] - tracks_60[i - 1])
            if d > 180:
                d = 360 - d
            track_diffs.append(d)
        course_stable = max(track_diffs) < COURSE_STABLE_THRESHOLD

    # Speed trend
    speed_decreasing = False
    if len(beacons) >= 5:
        early_speed = sum(b.speed for b in beacons[:3]) / 3
        late_speed = sum(b.speed for b in beacons[-3:]) / 3
        speed_decreasing = late_speed < early_speed * 0.7

    # VS trend (acceleration of sink)
    vs_trend = 0.0
    if len(vs_points) >= 4:
        half = len(vs_points) // 2
        first_half = sum(vs_points[:half]) / half
        second_half = sum(vs_points[half:]) / (len(vs_points) - half)
        duration = last.timestamp - beacons[-len(vs_points)].timestamp
        if duration > 0:
            vs_trend = (second_half - first_half) / duration

    return {
        "last_vs": last.vs,
        "last_speed": last.speed,
        "last_alt": last.alt,
        "alt_loss_30s": alt_loss_30s,
        "avg_vs_60s": avg_vs_60s,
        "max_course_change_10s": max_course_change,
        "course_stable": course_stable,
        "speed_decreasing": speed_decreasing,
        "vs_trend": vs_trend,
    }


def _detect_abnormal(profile: dict, beacons: list[ProfilePoint]) -> list[str]:
    """Detect abnormal flight profile indicators.

    Returns list of indicator strings (empty = normal profile).
    """
    flags = []

    # Critical sink rate
    if profile["last_vs"] < CRITICAL_SINK_RATE:
        flags.append(f"Starkes Sinken ({profile['last_vs']:+.1f} m/s)")

    # Rapid altitude loss
    if profile["alt_loss_30s"] > RAPID_ALT_LOSS_M:
        flags.append(f"Schneller Hoehenverlust ({profile['alt_loss_30s']:.0f}m in 30s)")

    # Course instability (spin/tumble)
    if profile["max_course_change_10s"] > COURSE_DEVIATION_DEG:
        flags.append(f"Kurs instabil ({profile['max_course_change_10s']:.0f}° in 10s)")

    # High speed + sink (dive)
    if (profile["last_speed"] > DIVE_SPEED_THRESHOLD
            and profile["last_vs"] < DIVE_SINK_THRESHOLD):
        flags.append(
            f"Sturzflug ({profile['last_speed']:.0f}km/h, "
            f"{profile['last_vs']:+.1f}m/s)"
        )

    # Accelerating sink
    if profile["vs_trend"] < VS_ACCELERATION_THRESHOLD:
        flags.append(f"Sinken beschleunigt ({profile['vs_trend']:+.2f} m/s²)")

    return flags


def _is_controlled_descent(profile: dict) -> bool:
    """Check if the profile shows a controlled descent (approach/landing)."""
    vs = profile["avg_vs_60s"]
    return (
        NORMAL_VS_MIN <= vs <= NORMAL_VS_MAX
        and profile["speed_decreasing"]
        and profile["last_speed"] < CONTROLLED_SPEED_MAX
    )
