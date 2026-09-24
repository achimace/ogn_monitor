"""Beacon parser: FLARM aircraft category from the id byte."""

import pytest

from app.aprs.beacon_parser import parse_beacon

LINE = (
    "FLRDDA5BA>APRS,qAS,LFLE:/174849h4540.45N/00554.94E'157/091/A=003930 "
    "!W68! id{idbyte}DDA5BA -078fpm +0.0rot 19.0dB 0e -6.2kHz gps3x5 s6.01 h44 rDDA5BA"
)


@pytest.mark.parametrize(
    "idbyte, expected_type",
    [
        ("06", 1),  # 0b0000_0110: type 1 glider, FLARM address
        ("0A", 2),  # 0b0000_1010: type 2 tow plane
        ("22", 8),  # 0b0010_0010: type 8 powered aircraft
        ("86", 1),  # stealth bit set, still a glider
    ],
)
def test_device_type_is_bits_2_to_5_of_id_byte(idbyte, expected_type):
    b = parse_beacon(LINE.format(idbyte=idbyte))
    assert b is not None
    assert b.flarm_id == "DDA5BA"
    assert b.device_type == expected_type
