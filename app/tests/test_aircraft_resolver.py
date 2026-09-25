"""AircraftResolver cache merge and the OGN DDB privacy flags.

ODbL condition for the OGN DDB: "you must follow DDB tracking privacy
choices". tracked=N devices are dropped by the FlightTracker (tested
there); identified=N devices must never get a registration / competition
sign from the DDB or the APRS stream - only from the tenant's own fleet.
"""

from app.data.aircraft_resolver import AircraftResolver, build_cache

FID = "DD1234"


def registry_row(**overrides) -> dict:
    row = dict(
        device_id=FID, registration="D-1234", aircraft_model="ASK 21",
        competition_sign="XY", device_type="F", source="ogn_ddb",
        tracked=True, identified=True,
    )
    row.update(overrides)
    return row


def tenant_row(**overrides) -> dict:
    row = dict(
        flarm_id=FID, registration="D-TENANT", aircraft_model="ASK 21",
        competition_sign="TT", aircraft_type="glider", role="glider",
    )
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# build_cache
# ---------------------------------------------------------------------------

def test_registry_entry_with_default_flags_is_fully_identified():
    info = build_cache([registry_row()], [])[FID]
    assert info.tracked is True and info.identified is True
    assert info.registration == "D-1234"
    assert info.competition_sign == "XY"
    assert info.aircraft_model == "ASK 21"
    assert info.source == "ogn_ddb"


def test_null_flags_count_as_true():
    info = build_cache([registry_row(tracked=None, identified=None)], [])[FID]
    assert info.tracked is True and info.identified is True


def test_untracked_flag_is_passed_through():
    info = build_cache([registry_row(tracked=False)], [])[FID]
    assert info.tracked is False


def test_unidentified_device_has_no_registration_or_competition_sign():
    info = build_cache([registry_row(identified=False)], [])[FID]
    assert info.identified is False
    assert info.registration == ""
    assert info.competition_sign == ""
    # the model is not identifying and is needed for launch detection
    assert info.aircraft_model == "ASK 21"


def test_lowercase_device_id_is_normalised():
    cache = build_cache([registry_row(device_id="dd1234")], [])
    assert set(cache) == {FID}


def test_tenant_fleet_entry_overrides_unidentified_ddb_entry():
    """Operator entered its own aircraft = explicit consent."""
    info = build_cache([registry_row(identified=False)], [tenant_row()])[FID]
    assert info.source == "tenant"
    assert info.identified is True
    assert info.registration == "D-TENANT"
    assert info.competition_sign == "TT"
    assert info.role == "glider"


def test_tenant_fleet_entry_does_not_override_ddb_opt_out():
    """tracked=N is the device owner's choice and wins over the tenant."""
    info = build_cache([registry_row(tracked=False)], [tenant_row()])[FID]
    assert info.source == "tenant"
    assert info.tracked is False


def test_tenant_entry_without_ddb_entry_is_tracked():
    info = build_cache([], [tenant_row()])[FID]
    assert info.tracked is True and info.identified is True
    assert info.registration == "D-TENANT"


# ---------------------------------------------------------------------------
# update_from_aprs
# ---------------------------------------------------------------------------

def test_aprs_registration_cannot_reidentify_unidentified_device():
    resolver = AircraftResolver()
    resolver._cache = build_cache([registry_row(identified=False)], [])

    resolver.update_from_aprs(FID, "D-1234")

    info = resolver.resolve(FID)
    assert info.registration == ""
    assert info.identified is False
    assert info.source == "ogn_ddb"


def test_aprs_registration_is_used_for_unknown_device():
    resolver = AircraftResolver()
    resolver.update_from_aprs("UNKNW1", "D-APRS")
    info = resolver.resolve("UNKNW1")
    assert info is not None
    assert info.registration == "D-APRS" and info.source == "aprs"
    assert info.tracked is True and info.identified is True
