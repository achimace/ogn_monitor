"""APRS-IS filter builder for multiple airfields.

Consolidates multiple airfield positions into at most 9 APRS range filters.
Uses minimum enclosing circle for nearby airfields to reduce filter count.

APRS-IS filter format: r/lat/lon/radius_km
Max 9 filters per connection.
"""

from dataclasses import dataclass
from math import atan2, cos, degrees, radians, sin, sqrt

import structlog

from app.tracking.geo_calc import haversine

log = structlog.get_logger()

MAX_FILTERS = 9


@dataclass
class AirfieldPosition:
    """Minimal airfield data needed for filter building."""
    slug: str
    latitude: float
    longitude: float
    radius_km: int


def build_filters(airfields: list[AirfieldPosition]) -> list[str]:
    """Build APRS-IS range filters for a list of airfields.

    Clusters nearby airfields into shared filters when possible,
    ensuring we stay within the 9-filter APRS-IS limit.

    Args:
        airfields: List of airfield positions with their tracking radii.

    Returns:
        List of APRS filter strings (e.g. ["r/47.658/11.234/500"]).
    """
    if not airfields:
        return []

    if len(airfields) == 1:
        af = airfields[0]
        return [f"r/{af.latitude:.3f}/{af.longitude:.3f}/{af.radius_km}"]

    # Start with one filter per airfield
    clusters: list[_Cluster] = [
        _Cluster(
            lat=af.latitude,
            lon=af.longitude,
            radius_km=af.radius_km,
            airfields=[af],
        )
        for af in airfields
    ]

    # Merge closest clusters until we have <= MAX_FILTERS
    while len(clusters) > MAX_FILTERS:
        clusters = _merge_closest(clusters)

    # Also merge overlapping clusters even if under limit
    clusters = _merge_overlapping(clusters)

    filters = []
    for c in clusters:
        filters.append(f"r/{c.lat:.3f}/{c.lon:.3f}/{c.radius_km}")

    log.info(
        "aprs_filters_built",
        airfield_count=len(airfields),
        filter_count=len(filters),
        filters=filters,
    )
    return filters


@dataclass
class _Cluster:
    lat: float
    lon: float
    radius_km: int
    airfields: list[AirfieldPosition]


def _merge_closest(clusters: list[_Cluster]) -> list[_Cluster]:
    """Find and merge the two closest clusters."""
    min_dist = float("inf")
    merge_i, merge_j = 0, 1

    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            dist = haversine(
                clusters[i].lat, clusters[i].lon,
                clusters[j].lat, clusters[j].lon,
            )
            if dist < min_dist:
                min_dist = dist
                merge_i, merge_j = i, j

    merged = _merge_two(clusters[merge_i], clusters[merge_j])

    result = []
    for k, c in enumerate(clusters):
        if k != merge_i and k != merge_j:
            result.append(c)
    result.append(merged)
    return result


def _merge_overlapping(clusters: list[_Cluster]) -> list[_Cluster]:
    """Merge clusters whose coverage areas overlap significantly."""
    changed = True
    while changed:
        changed = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                dist_km = haversine(
                    clusters[i].lat, clusters[i].lon,
                    clusters[j].lat, clusters[j].lon,
                ) / 1000.0

                # If one circle fully contains the other, merge
                max_r = max(clusters[i].radius_km, clusters[j].radius_km)
                if dist_km + min(clusters[i].radius_km, clusters[j].radius_km) <= max_r * 1.1:
                    merged = _merge_two(clusters[i], clusters[j])
                    clusters = [c for k, c in enumerate(clusters) if k != i and k != j]
                    clusters.append(merged)
                    changed = True
                    break
            if changed:
                break

    return clusters


def _merge_two(a: _Cluster, b: _Cluster) -> _Cluster:
    """Merge two clusters into one covering both."""
    all_airfields = a.airfields + b.airfields

    # Center = weighted average (by count of airfields)
    total = len(all_airfields)
    center_lat = sum(af.latitude for af in all_airfields) / total
    center_lon = sum(af.longitude for af in all_airfields) / total

    # Radius must cover all airfields with their individual radii
    max_needed = 0
    for af in all_airfields:
        dist_km = haversine(center_lat, center_lon, af.latitude, af.longitude) / 1000.0
        needed = dist_km + af.radius_km
        max_needed = max(max_needed, needed)

    return _Cluster(
        lat=center_lat,
        lon=center_lon,
        radius_km=int(max_needed) + 10,  # Small buffer
        airfields=all_airfields,
    )
