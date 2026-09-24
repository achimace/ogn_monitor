"""Pydantic model of a Vereinsflieger flight record.

ASSUMPTIONS about the VF response (verify in AP-12): VF serialises every
field as a string (``"towheight": "450"``, ``"landingcount": "1"``, empty
values as ``""``). Unknown fields are kept (``extra="allow"``) so the raw
record can be stored as ``pre_state`` in the audit log without loss.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.vfsync.vf_client.mapping import VF_NULL_TIME, from_vf_time

__all__ = ["VfFlight"]

_OPTIONAL_INT_FIELDS = ("towheight", "towtime", "towflid")


def _coerce_optional_int(value: Any) -> int | None:
    """``""``/``None`` → None, numeric strings → int."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
        return int(float(value)) if "." in value else int(value)
    return int(value)


class VfFlight(BaseModel):
    """One flight as returned by ``flight/get`` or ``flight/list/*``.

    Times are VF strings (``YYYY-mm-dd HH:MM``); use ``departure_dt`` /
    ``arrival_dt`` for tz-aware UTC datetimes.
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=False)

    flid: int
    callsign: str = ""
    pilotname: str = ""
    attendantname: str = ""
    departuretime: str = ""
    arrivaltime: str = ""
    starttype: int | str | None = None
    towheight: int | None = None
    towtime: int | None = None
    landingcount: int = 1
    towcallsign: str = ""
    towflid: int | None = None
    flighttime: str = ""
    comment: str = ""

    @field_validator("flid", mode="before")
    @classmethod
    def _flid_from_str(cls, v: Any) -> Any:
        if isinstance(v, str):
            return int(v.strip())
        return v

    @field_validator(*_OPTIONAL_INT_FIELDS, mode="before")
    @classmethod
    def _optional_int(cls, v: Any) -> int | None:
        return _coerce_optional_int(v)

    @field_validator("landingcount", mode="before")
    @classmethod
    def _landingcount(cls, v: Any) -> int:
        coerced = _coerce_optional_int(v)
        return 1 if coerced is None else coerced

    @field_validator("callsign", "pilotname", "attendantname", "departuretime",
                     "arrivaltime", "towcallsign", "flighttime", "comment",
                     mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        if v is None:
            return ""
        return str(v)

    @property
    def departure_dt(self) -> datetime | None:
        """``departuretime`` as tz-aware UTC datetime (``None`` if unset)."""
        return from_vf_time(self.departuretime)

    @property
    def arrival_dt(self) -> datetime | None:
        """``arrivaltime`` as tz-aware UTC datetime (``None`` if unset)."""
        return from_vf_time(self.arrivaltime)

    def is_empty(self, field: str) -> bool:
        """Return True if ``field`` is unset in VF.

        Empty means ``None``, ``""``, ``0`` / ``"0"`` or the VF null date
        ``0000-00-00 00:00`` (with or without seconds). Unknown field names
        count as empty.
        """
        value = getattr(self, field, None)
        if value is None:
            return True
        if isinstance(value, str):
            s = value.strip()
            return s == "" or s == "0" or s.startswith(VF_NULL_TIME[:10])
        if isinstance(value, (int, float)):
            return value == 0
        return False

    def raw(self) -> dict[str, Any]:
        """All fields including extras (for audit ``pre_state``)."""
        return self.model_dump()
