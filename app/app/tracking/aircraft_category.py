"""Aircraft category - one enum for the OGN beacon type and the DDB / tenant coding.

Two sources describe what kind of aircraft a FLARM-ID belongs to:

* the **OGN beacon** itself: bits 5..2 of the id byte (``Beacon.device_type``,
  see ``aprs/beacon_parser.py``), the FLARM/OGN aircraft type table:
  1 glider, 2 tow plane, 3 helicopter, 4 parachute, 5 drop plane,
  6 hang glider, 7 paraglider, 8 powered, 9 jet, 10 UFO, 11 balloon,
  12 airship, 13 UAV, 14 reserved, 15 static obstacle;
* the **tenant fleet** (``tenant_aircraft.aircraft_type``, free strings such
  as ``glider`` / ``tow_plane`` / ``motor_glider`` / ``tmg`` / ``helicopter``)
  and the global ``aircraft_registry``. Note that the OGN DDB carries **no**
  aircraft category: its ``DEVICE_TYPE`` column (F / I / O) is the *address*
  type (FLARM / ICAO / OGN tracker) and must never be read as a category.
  The ``aircraft_registry.aircraft_type`` column (empty for DDB rows,
  maintainable by hand) is honoured when set.

``category_for_beacon`` combines both: a category known from the tenant /
registry wins, the beacon type is the fallback. This is what the
FlightTracker uses for the type filter (``settings.ignored_aircraft_categories``).
"""

from enum import Enum

__all__ = [
    "AircraftCategory",
    "category_from_ogn_type",
    "category_from_ddb",
    "category_for_beacon",
    "parse_category_list",
]


class AircraftCategory(str, Enum):
    """Normalised aircraft category (value = config / API spelling)."""

    GLIDER = "glider"
    TOW_PLANE = "tow_plane"
    MOTOR_GLIDER = "motor_glider"
    POWERED = "powered"
    ULTRALIGHT = "ultralight"
    HELICOPTER = "helicopter"
    PARAGLIDER_HANGGLIDER = "paraglider_hangglider"
    PARACHUTE_DROP = "parachute_drop"
    BALLOON_AIRSHIP = "balloon_airship"
    UAV = "uav"
    STATIC = "static"
    JET = "jet"
    UNKNOWN = "unknown"


# OGN / FLARM aircraft type (beacon id byte, bits 5..2) -> category
_OGN_TYPE_CATEGORY: dict[int, AircraftCategory] = {
    1: AircraftCategory.GLIDER,
    2: AircraftCategory.TOW_PLANE,
    3: AircraftCategory.HELICOPTER,
    4: AircraftCategory.PARACHUTE_DROP,      # parachute
    5: AircraftCategory.POWERED,             # drop plane: a powered aircraft that
                                             # does take off/land at glider fields
    6: AircraftCategory.PARAGLIDER_HANGGLIDER,   # hang glider
    7: AircraftCategory.PARAGLIDER_HANGGLIDER,   # paraglider
    8: AircraftCategory.POWERED,
    9: AircraftCategory.JET,
    # 10 = UFO, 14 = reserved: unknown
    11: AircraftCategory.BALLOON_AIRSHIP,    # balloon
    12: AircraftCategory.BALLOON_AIRSHIP,    # airship
    13: AircraftCategory.UAV,
    15: AircraftCategory.STATIC,
}

# tenant_aircraft.aircraft_type / aircraft_registry.aircraft_type strings
# (lower-case, spaces and dashes normalised to underscores) -> category
_DDB_STRING_CATEGORY: dict[str, AircraftCategory] = {
    "glider": AircraftCategory.GLIDER,
    "sailplane": AircraftCategory.GLIDER,
    "segelflugzeug": AircraftCategory.GLIDER,
    "tow_plane": AircraftCategory.TOW_PLANE,
    "towplane": AircraftCategory.TOW_PLANE,
    "schlepper": AircraftCategory.TOW_PLANE,
    "motor_glider": AircraftCategory.MOTOR_GLIDER,
    "motorglider": AircraftCategory.MOTOR_GLIDER,
    "motorglider_sl": AircraftCategory.MOTOR_GLIDER,
    "tmg": AircraftCategory.MOTOR_GLIDER,
    "motorsegler": AircraftCategory.MOTOR_GLIDER,
    "powered": AircraftCategory.POWERED,
    "motor": AircraftCategory.POWERED,
    "airplane": AircraftCategory.POWERED,
    "aircraft": AircraftCategory.POWERED,
    "motorflugzeug": AircraftCategory.POWERED,
    "ultralight": AircraftCategory.ULTRALIGHT,
    "ul": AircraftCategory.ULTRALIGHT,
    "microlight": AircraftCategory.ULTRALIGHT,
    "helicopter": AircraftCategory.HELICOPTER,
    "heli": AircraftCategory.HELICOPTER,
    "hubschrauber": AircraftCategory.HELICOPTER,
    "paraglider": AircraftCategory.PARAGLIDER_HANGGLIDER,
    "hang_glider": AircraftCategory.PARAGLIDER_HANGGLIDER,
    "hangglider": AircraftCategory.PARAGLIDER_HANGGLIDER,
    "paraglider_hangglider": AircraftCategory.PARAGLIDER_HANGGLIDER,
    "parachute": AircraftCategory.PARACHUTE_DROP,
    "drop_plane": AircraftCategory.PARACHUTE_DROP,
    "parachute_drop": AircraftCategory.PARACHUTE_DROP,
    "balloon": AircraftCategory.BALLOON_AIRSHIP,
    "airship": AircraftCategory.BALLOON_AIRSHIP,
    "balloon_airship": AircraftCategory.BALLOON_AIRSHIP,
    "uav": AircraftCategory.UAV,
    "drone": AircraftCategory.UAV,
    "static": AircraftCategory.STATIC,
    "static_obstacle": AircraftCategory.STATIC,
    "jet": AircraftCategory.JET,
}


def category_from_ogn_type(device_type: int | None) -> AircraftCategory:
    """Category of an OGN beacon aircraft type (0 / unknown codes -> UNKNOWN)."""
    if device_type is None:
        return AircraftCategory.UNKNOWN
    try:
        return _OGN_TYPE_CATEGORY.get(int(device_type), AircraftCategory.UNKNOWN)
    except (TypeError, ValueError):
        return AircraftCategory.UNKNOWN


def category_from_ddb(aircraft_type: str | None) -> AircraftCategory:
    """Category of a tenant / registry ``aircraft_type`` string.

    Accepts the tenant spellings (``glider``, ``tow_plane``, ``motor_glider``,
    ``tmg``, ``helicopter``, ...), the enum values themselves and a numeric
    OGN type as string. Anything else - including the DDB address type
    letters F / I / O, which are not a category - yields UNKNOWN.
    """
    if not aircraft_type:
        return AircraftCategory.UNKNOWN
    key = str(aircraft_type).strip().lower().replace("-", "_").replace(" ", "_")
    if not key:
        return AircraftCategory.UNKNOWN
    if key.isdigit():
        return category_from_ogn_type(int(key))
    return _DDB_STRING_CATEGORY.get(key, AircraftCategory.UNKNOWN)


def category_for_beacon(known: AircraftCategory | None,
                        beacon_device_type: int) -> AircraftCategory:
    """Effective category of a beacon: the known (tenant / registry) category
    wins when it is not UNKNOWN, else the type transmitted in the beacon."""
    if known is not None and known is not AircraftCategory.UNKNOWN:
        return known
    return category_from_ogn_type(beacon_device_type)


def parse_category_list(value: str) -> frozenset[AircraftCategory]:
    """Parse a comma separated list of category names (config value).

    Case-insensitive, blanks ignored; an empty string is an empty set.
    Raises ValueError for an unknown name and for ``unknown`` itself
    (ignoring every aircraft without a known category would silence the
    monitor for all devices not in the DDB).
    """
    result: set[AircraftCategory] = set()
    for raw in (value or "").split(","):
        name = raw.strip().lower()
        if not name:
            continue
        try:
            cat = AircraftCategory(name)
        except ValueError:
            try:
                cat = AircraftCategory[name.upper()]
            except KeyError:
                valid = ", ".join(c.value for c in AircraftCategory if c is not AircraftCategory.UNKNOWN)
                raise ValueError(
                    f"unknown aircraft category '{raw.strip()}' (valid: {valid})"
                ) from None
        if cat is AircraftCategory.UNKNOWN:
            raise ValueError("'unknown' cannot be ignored: it would drop every aircraft "
                             "without a known category")
        result.add(cat)
    return frozenset(result)
