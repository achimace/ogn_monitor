"""Geographic calculations for flight tracking.

Provides QDR (bearing), distance, compass direction and AGL calculations.
All functions are pure math - no I/O, no async, no DB queries.
Designed for high-frequency calls (every beacon, ~3s per flight).
"""

from math import atan2, cos, degrees, radians, sin, sqrt

# Earth radius in meters (WGS84 mean)
EARTH_RADIUS_M = 6_371_000

# 16-point compass rose (German aviation convention)
_COMPASS_POINTS = [
    "N", "NNO", "NO", "ONO", "O", "OSO", "SO", "SSO",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate great-circle distance between two points in meters.

    Args:
        lat1, lon1: First point (WGS84 decimal degrees).
        lat2, lon2: Second point (WGS84 decimal degrees).

    Returns:
        Distance in meters.
    """
    phi1, phi2 = radians(lat1), radians(lat2)
    delta_phi = radians(lat2 - lat1)
    delta_lambda = radians(lon2 - lon1)

    a = sin(delta_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(delta_lambda / 2) ** 2
    return EARTH_RADIUS_M * 2 * atan2(sqrt(a), sqrt(1 - a))


def azimuth(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate initial bearing (azimuth) from point 1 to point 2.

    This is the QDR from the airfield to the aircraft.

    Args:
        lat1, lon1: Origin point (airfield).
        lat2, lon2: Target point (aircraft).

    Returns:
        Bearing in degrees (0-360, clockwise from north).
    """
    phi1, phi2 = radians(lat1), radians(lat2)
    delta_lambda = radians(lon2 - lon1)

    y = sin(delta_lambda) * cos(phi2)
    x = cos(phi1) * sin(phi2) - sin(phi1) * cos(phi2) * cos(delta_lambda)

    return degrees(atan2(y, x)) % 360


def degrees_to_compass(deg: float) -> str:
    """Convert bearing in degrees to 16-point compass direction.

    Args:
        deg: Bearing in degrees (0-360).

    Returns:
        Compass direction string (e.g. "NNO", "SW", "O").
    """
    index = round(deg / 22.5) % 16
    return _COMPASS_POINTS[index]


def altitude_agl(altitude_m: float, elevation_m: float) -> float:
    """Calculate altitude above ground level.

    Args:
        altitude_m: Aircraft altitude MSL in meters.
        elevation_m: Ground elevation (airfield) in meters.

    Returns:
        Altitude AGL in meters (can be negative near ground).
    """
    return altitude_m - elevation_m
