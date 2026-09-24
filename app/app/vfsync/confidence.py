"""Confidence gates per VF field (Konzept Kap. 5.4 step 2).

Pure logic: decides which of the requested fields may be written for a
session. A field that fails its gate is dropped and a review reason is
returned; the remaining fields are still written ("Abstention is a
feature" - never a wrong value, rather none).
"""

from app.vfsync.models import Session

# Gates (Kap. 5.4)
MIN_PAIRING_CONFIDENCE = 0.9      # towheight / towtime
MIN_TOUCHGO_CONFIDENCE = 0.9      # landingcount > 1
MIN_SILENCE_LANDING_CONFIDENCE = 0.8  # arrivaltime of a silence landing
TOWHEIGHT_MIN_AGL_M = 100
TOWHEIGHT_MAX_AGL_M = 2000

WRITABLE_FIELDS = ("departuretime", "arrivaltime", "towheight", "towtime", "landingcount")

# Fields that make up the bundled landing edit (Kap. 3.2 step 5)
LANDING_BUNDLE_FIELDS = frozenset({"arrivaltime", "towheight", "towtime", "landingcount"})


def gate_fields(session: Session, fields: set[str]) -> tuple[set[str], list[str]]:
    """Apply the confidence gates.

    Args:
        session: The sync session with detection results and confidences.
        fields: Requested VF field names (subset of WRITABLE_FIELDS).

    Returns:
        (allowed fields, review reasons for the dropped ones). Fields the
        session has no value for are dropped silently (no reason).
    """
    allowed: set[str] = set()
    reasons: list[str] = []

    for field in fields:
        if field not in WRITABLE_FIELDS:
            reasons.append(f"{field}_not_writable")
            continue

        if field == "departuretime":
            if session.takeoff_ts is not None:
                allowed.add(field)

        elif field == "arrivaltime":
            if session.landing_ts is None:
                continue
            if session.landing_method == "silence":
                conf = session.conf_landing or 0.0
                if conf < MIN_SILENCE_LANDING_CONFIDENCE:
                    reasons.append(f"arrivaltime_silence_low_confidence:{conf:.2f}")
                    continue
            allowed.add(field)

        elif field in ("towheight", "towtime"):
            if not session.is_aerotow:
                # Only aerotows carry heights/times (R-03); ambiguous pairs never
                continue
            value = session.release_alt_agl_m if field == "towheight" else session.tow_time_min
            if value is None:
                continue
            conf = session.conf_pairing or 0.0
            if conf < MIN_PAIRING_CONFIDENCE:
                reasons.append(f"{field}_low_pairing_confidence:{conf:.2f}")
                continue
            if field == "towheight" and not (
                TOWHEIGHT_MIN_AGL_M <= session.release_alt_agl_m <= TOWHEIGHT_MAX_AGL_M
            ):
                reasons.append(f"towheight_implausible:{session.release_alt_agl_m}m")
                continue
            if field == "towtime" and session.tow_time_min <= 0:
                continue
            allowed.add(field)

        elif field == "landingcount":
            if session.landing_count <= 1:
                continue  # VF default is 1, nothing to raise
            conf = session.conf_touchgo or 0.0
            if conf < MIN_TOUCHGO_CONFIDENCE:
                reasons.append(f"landingcount_low_touchgo_confidence:{conf:.2f}")
                continue
            allowed.add(field)

    return allowed, reasons
