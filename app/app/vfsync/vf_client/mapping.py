"""Domain mapping between ogn_monitor and the Vereinsflieger REST API.

Binding interface: docs/dev-guides/vfsync-internals.md (section vf_client),
specification: docs/konzept-vf-sync.md Kap. 5.3.

ASSUMPTIONS (to be verified / corrected in AP-12, the VF API spike):

* Start type codes: the VF *read* side (``flight/get``, ``flight/list/*``)
  returns ``starttype`` as a numeric code (``1`` self launch/powered,
  ``3`` aerotow, ``5`` winch, ``7`` bungee, ``9`` vehicle tow) – possibly as a
  string, because VF serialises every field as string. The *write* side
  (``flight/edit``) takes a letter (``E`` self, ``W`` winch, ``F`` aerotow).
  ``is_starttype_compatible`` accepts both alphabets to be safe.
* VF times are UTC in the format ``YYYY-mm-dd HH:MM`` (minute granularity).
  ``from_vf_time`` also tolerates a trailing ``:SS`` and treats the VF
  "null date" ``0000-00-00 00:00`` as *not set*.
* ``towtime`` is an integer number of minutes; the rounding rule from seconds
  is the spike result (``TOWTIME_ROUNDING``), default half-up.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Final, Literal

__all__ = [
    "StartType",
    "WRITE_MAP",
    "READ_MAP",
    "VF_TIME_FMT",
    "VF_NULL_TIME",
    "TOWTIME_ROUNDING",
    "AEROTOW_AMBIGUOUS",
    "to_vf_time",
    "from_vf_time",
    "tow_time_minutes",
    "is_starttype_compatible",
    "normalize_callsign",
]


class StartType(str, Enum):
    """Launch type as detected by the tracking worker."""

    AEROTOW = "aerotow"
    WINCH = "winch"
    SELF = "self"
    POWERED = "powered"
    UNKNOWN = "unknown"


#: Tracking value for an aerotow whose tow pairing could not be established
#: unambiguously (``Session.start_type_detected``). Not a StartType member
#: because it is never written to VF.
AEROTOW_AMBIGUOUS: Final[str] = "aerotow_ambiguous"

#: StartType → VF ``starttype`` letter for ``flight/edit``.
#: POWERED is deliberately absent until the spike (Kap. 11, item 6) confirms
#: what VF itself writes for powered aircraft.
WRITE_MAP: Final[dict[StartType, str]] = {
    StartType.AEROTOW: "F",
    StartType.WINCH: "W",
    StartType.SELF: "E",
}

#: VF numeric ``starttype`` code → set of compatible detected start types.
#: 7 (bungee) and 9 (vehicle tow) map to the empty set: nothing we detect
#: is compatible with them.
READ_MAP: Final[dict[int, frozenset[StartType]]] = {
    1: frozenset({StartType.SELF, StartType.POWERED}),
    3: frozenset({StartType.AEROTOW}),
    5: frozenset({StartType.WINCH}),
    7: frozenset(),
    9: frozenset(),
}

#: Inverse of WRITE_MAP (letter → detected start types), used when VF echoes
#: the letter alphabet on the read side.
_LETTER_MAP: Final[dict[str, frozenset[StartType]]] = {
    "F": frozenset({StartType.AEROTOW}),
    "W": frozenset({StartType.WINCH}),
    "E": frozenset({StartType.SELF, StartType.POWERED}),
}

VF_TIME_FMT: Final[str] = "%Y-%m-%d %H:%M"
VF_TIME_FMT_SECONDS: Final[str] = "%Y-%m-%d %H:%M:%S"
VF_NULL_TIME: Final[str] = "0000-00-00 00:00"

#: Rounding rule seconds → minutes for ``towtime``. Spike result (AP-12).
TOWTIME_ROUNDING: Literal["half_up", "floor", "ceil"] = "half_up"


def to_vf_time(dt: datetime) -> str:
    """Format a datetime for VF (UTC, minute granularity, seconds truncated).

    Args:
        dt: Timezone-aware datetime (converted to UTC) or naive datetime
            (interpreted as UTC).

    Returns:
        String in ``VF_TIME_FMT``.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime(VF_TIME_FMT)


def from_vf_time(s: str | None) -> datetime | None:
    """Parse a VF time string into a tz-aware UTC datetime.

    Args:
        s: ``"YYYY-mm-dd HH:MM"`` (optionally with ``:SS``), ``""``, ``None``
            or the VF null date ``"0000-00-00 00:00"``.

    Returns:
        Timezone-aware UTC datetime, or ``None`` for empty / null values.

    Raises:
        ValueError: If the string is non-empty but not a valid VF time.
    """
    if s is None:
        return None
    s = s.strip()
    if not s or s.startswith("0000-00-00"):
        return None
    for fmt in (VF_TIME_FMT, VF_TIME_FMT_SECONDS):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"not a VF time: {s!r}")


def tow_time_minutes(seconds: int) -> int:
    """Convert a tow duration in seconds to VF ``towtime`` minutes.

    Uses ``TOWTIME_ROUNDING``; the default ``half_up`` rounds 5:30 → 6,
    5:29 → 5 (commercial rounding, never banker's rounding).

    Args:
        seconds: Tow duration in seconds (negative values are clamped to 0).

    Returns:
        Whole minutes.
    """
    seconds = max(0, int(seconds))
    if TOWTIME_ROUNDING == "floor":
        return seconds // 60
    if TOWTIME_ROUNDING == "ceil":
        return -(-seconds // 60)
    return (seconds + 30) // 60


def _is_empty_starttype(value: int | str | None) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in ("", "0")
    return value == 0


def is_starttype_compatible(detected: StartType | str | None,
                            vf_starttype: int | str | None) -> bool:
    """Check whether a detected start type may be written next to VF's value.

    Rules (Kap. 5.4 step 5, R-04):

    * VF value empty (``None``, ``""``, ``0``, ``"0"``) → always compatible.
    * Detected ``UNKNOWN`` / ``aerotow_ambiguous`` / ``None`` → only compatible
      with an empty VF value.
    * Numeric VF code (int or numeric string) → ``READ_MAP``.
    * Letter code (``E``/``W``/``F``, case-insensitive) → inverse ``WRITE_MAP``.
    * Unknown codes → incompatible.

    Args:
        detected: Detected start type (StartType or its string value).
        vf_starttype: Raw ``starttype`` value from a VF flight.

    Returns:
        ``True`` if writing is allowed with respect to the start type.
    """
    if _is_empty_starttype(vf_starttype):
        return True

    if detected is None:
        return False
    if isinstance(detected, StartType):
        det = detected
    else:
        try:
            det = StartType(str(detected).strip().lower())
        except ValueError:
            return False  # e.g. "aerotow_ambiguous"
    if det is StartType.UNKNOWN:
        return False

    raw = vf_starttype
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.lstrip("-").isdigit():
            allowed = READ_MAP.get(int(raw))
        else:
            allowed = _LETTER_MAP.get(raw.upper())
    else:
        allowed = READ_MAP.get(int(raw))
    if allowed is None:
        return False
    return det in allowed


def normalize_callsign(s: str | None) -> str:
    """Normalise a registration for comparison: ``"D-1234 "`` → ``"D1234"``.

    Removes whitespace and hyphens and upper-cases. ``None`` → ``""``.
    """
    if not s:
        return ""
    return "".join(ch for ch in s.upper() if ch not in " -\t\r\n")
