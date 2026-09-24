"""Vereinsflieger REST adapter: mapping, models, client and in-process mock.

Interface contract: docs/dev-guides/vfsync-internals.md (section vf_client).
The mock is intentionally not re-exported here – import it from
``app.vfsync.vf_client.mock`` in tests only.
"""

from app.vfsync.vf_client.client import (
    VfAuthError,
    VfBadRequest,
    VfClient,
    VfError,
    VfForbidden,
    VfNetworkError,
    VfServerError,
)
from app.vfsync.vf_client.mapping import (
    AEROTOW_AMBIGUOUS,
    READ_MAP,
    TOWTIME_ROUNDING,
    VF_TIME_FMT,
    WRITE_MAP,
    StartType,
    from_vf_time,
    is_starttype_compatible,
    normalize_callsign,
    to_vf_time,
    tow_time_minutes,
)
from app.vfsync.vf_client.models import VfFlight

__all__ = [
    "AEROTOW_AMBIGUOUS",
    "READ_MAP",
    "TOWTIME_ROUNDING",
    "VF_TIME_FMT",
    "WRITE_MAP",
    "StartType",
    "VfAuthError",
    "VfBadRequest",
    "VfClient",
    "VfError",
    "VfFlight",
    "VfForbidden",
    "VfNetworkError",
    "VfServerError",
    "from_vf_time",
    "is_starttype_compatible",
    "normalize_callsign",
    "to_vf_time",
    "tow_time_minutes",
]
