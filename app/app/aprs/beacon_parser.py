"""OGN APRS Beacon Parser.

Parses raw APRS-IS lines from OGN into structured Beacon dataclasses.
Handles OGN-specific extensions (FLARM ID, climb rate, turn rate, etc.).

OGN APRS format example:
  FLRDDA5BA>APRS,qAS,LFLE:/174849h4540.45N/00554.94E'157/091/A=003930
  !W68! id06DDA5BA -078fpm +0.0rot 19.0dB 0e -6.2kHz gps3x5 s6.01 h44 rDDA5BA

Reference: http://wiki.glidernet.org/wiki:subscribe-to-ogn-data
"""

import re
import time
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger()

# Precompiled regex for OGN beacon parsing
# Matches the position part: /HHMMSSh DDmm.mmN/DDDmm.mmE' CCC/SSS/A=AAAAAA
_POSITION_RE = re.compile(
    r"/(\d{6})h"                           # timestamp HHMMSS
    r"(\d{4}\.\d{2})([NS])"               # latitude DDmm.mm N/S
    r"(.)"                                  # symbol table
    r"(\d{5}\.\d{2})([EW])"               # longitude DDDmm.mm E/W
    r"(.)"                                  # symbol code
    r"(\d{3})/(\d{3})"                     # course/speed (knots)
    r"/A=(\d{6})"                          # altitude (feet)
)

# OGN extension fields in the comment section
_FLARM_ID_RE = re.compile(r"id(\w{2})(\w{6})")        # id06DDA5BA
_CLIMB_RE = re.compile(r"([+-]\d+)fpm")               # -078fpm
_TURN_RE = re.compile(r"([+-][\d.]+)rot")              # +0.0rot
_SIGNAL_RE = re.compile(r"([\d.]+)dB")                 # 19.0dB
_ERROR_RE = re.compile(r"(\d+)e")                      # 0e
_FREQ_RE = re.compile(r"([+-][\d.]+)kHz")              # -6.2kHz
_GPS_RE = re.compile(r"gps(\d+)x(\d+)")                # gps3x5
_HEAR_RE = re.compile(r"hear(\w+)")                    # hearXXXX
_REG_RE = re.compile(r"reg(\w+)")                      # regD-KMSF
_PRECISION_RE = re.compile(r"!W(\d)(\d)!")             # !W68! precision enhancement

# Conversion constants
_FEET_TO_METERS = 0.3048
_KNOTS_TO_KMH = 1.852
_FPM_TO_MS = 0.00508  # feet per minute -> meters per second


@dataclass(slots=True)
class Beacon:
    """Parsed OGN beacon with all relevant flight data."""

    timestamp: float             # Unix timestamp of beacon
    flarm_id: str                # 6-char hex FLARM/OGN device ID
    lat: float                   # WGS84 latitude (decimal degrees)
    lon: float                   # WGS84 longitude (decimal degrees)
    altitude: float              # Altitude MSL in meters
    speed: float                 # Ground speed in km/h
    vs: float                    # Vertical speed in m/s (+ up, - down)
    track: float                 # Course/heading 0-360 degrees
    receiver: str = ""           # Receiving station name
    signal_db: float = 0.0       # Signal strength in dB
    device_type: int = 0         # 1=Glider, 2=TowPlane, 3=Helicopter, etc.
    gps_h: int = 0               # GPS horizontal accuracy
    gps_v: int = 0               # GPS vertical accuracy
    turn_rate: float = 0.0       # Turn rate in half-turns per 2 minutes
    error_count: int = 0         # Transmission error count
    freq_offset: float = 0.0     # Frequency offset in kHz
    registration: str = ""       # Registration from APRS (if available)
    raw: str = ""                # Original raw APRS line


def parse_beacon(line: str) -> Beacon | None:
    """Parse a raw APRS-IS line into a Beacon object.

    Args:
        line: Raw APRS-IS line from OGN.

    Returns:
        Parsed Beacon or None if line is not a valid OGN position report.
    """
    # Skip server comments and keepalives
    if not line or line.startswith("#"):
        return None

    try:
        # Split into header and body at the first ':'
        header, _, body = line.partition(":")
        if not body:
            return None

        # Extract sender and receiver from header
        # Format: FLRDDA5BA>APRS,qAS,LFLE
        sender, _, path = header.partition(">")
        if not path:
            return None

        # Extract receiver station name (last element after qAS/qAR)
        receiver = ""
        path_parts = path.split(",")
        for i, part in enumerate(path_parts):
            if part.startswith("qA") and i + 1 < len(path_parts):
                receiver = path_parts[i + 1]
                break

        # Parse position report
        pos_match = _POSITION_RE.search(body)
        if not pos_match:
            return None

        # Time (HHMMSS) - use today's date
        time_str = pos_match.group(1)
        hours = int(time_str[0:2])
        minutes = int(time_str[2:4])
        seconds = int(time_str[4:6])

        # Build timestamp from today + beacon time (UTC)
        now = time.time()
        today_start = now - (now % 86400)  # Midnight UTC
        beacon_time = today_start + hours * 3600 + minutes * 60 + seconds
        # Handle day boundary (beacon from yesterday)
        if beacon_time > now + 3600:
            beacon_time -= 86400

        # Latitude: DDmm.mm -> decimal degrees
        lat_raw = pos_match.group(2)
        lat_deg = int(lat_raw[:2]) + float(lat_raw[2:]) / 60.0
        if pos_match.group(3) == "S":
            lat_deg = -lat_deg

        # Longitude: DDDmm.mm -> decimal degrees
        lon_raw = pos_match.group(5)
        lon_deg = int(lon_raw[:3]) + float(lon_raw[3:]) / 60.0
        if pos_match.group(6) == "W":
            lon_deg = -lon_deg

        # Apply precision enhancement (!Wxy!)
        prec_match = _PRECISION_RE.search(body)
        if prec_match:
            lat_enhance = int(prec_match.group(1))
            lon_enhance = int(prec_match.group(2))
            lat_deg += lat_enhance / 60000.0
            lon_deg += lon_enhance / 60000.0

        # Course, speed, altitude
        track = float(pos_match.group(8))
        speed_knots = float(pos_match.group(9))
        speed_kmh = speed_knots * _KNOTS_TO_KMH
        altitude_ft = float(pos_match.group(10))
        altitude_m = altitude_ft * _FEET_TO_METERS

        # Parse OGN comment section for extended data
        flarm_id = ""
        device_type = 0
        vs = 0.0
        signal_db = 0.0
        turn_rate = 0.0
        error_count = 0
        freq_offset = 0.0
        gps_h = 0
        gps_v = 0
        registration = ""

        # FLARM ID (required for tracking)
        id_match = _FLARM_ID_RE.search(body)
        if id_match:
            device_type = int(id_match.group(1), 16) & 0x0F
            flarm_id = id_match.group(2).upper()
        else:
            # No FLARM ID - can't track this beacon
            return None

        # Vertical speed (fpm -> m/s)
        climb_match = _CLIMB_RE.search(body)
        if climb_match:
            vs = int(climb_match.group(1)) * _FPM_TO_MS

        # Signal strength
        sig_match = _SIGNAL_RE.search(body)
        if sig_match:
            signal_db = float(sig_match.group(1))

        # Turn rate
        turn_match = _TURN_RE.search(body)
        if turn_match:
            turn_rate = float(turn_match.group(1))

        # Error count
        err_match = _ERROR_RE.search(body)
        if err_match:
            error_count = int(err_match.group(1))

        # Frequency offset
        freq_match = _FREQ_RE.search(body)
        if freq_match:
            freq_offset = float(freq_match.group(1))

        # GPS quality
        gps_match = _GPS_RE.search(body)
        if gps_match:
            gps_h = int(gps_match.group(1))
            gps_v = int(gps_match.group(2))

        # Registration (if broadcast)
        reg_match = _REG_RE.search(body)
        if reg_match:
            registration = reg_match.group(1)

        return Beacon(
            timestamp=beacon_time,
            flarm_id=flarm_id,
            lat=lat_deg,
            lon=lon_deg,
            altitude=altitude_m,
            speed=speed_kmh,
            vs=vs,
            track=track,
            receiver=receiver,
            signal_db=signal_db,
            device_type=device_type,
            gps_h=gps_h,
            gps_v=gps_v,
            turn_rate=turn_rate,
            error_count=error_count,
            freq_offset=freq_offset,
            registration=registration,
            raw=line,
        )

    except Exception:
        log.debug("beacon_parse_error", line=line[:120])
        return None
