"""import_airports tool: CSV filtering, elevation conversion, upsert / check (fake DB, no network)."""

import pytest

from app.tools import import_airports as tool
from app.tools.import_airports import (
    CHECK_SQL,
    UPSERT_SQL,
    ImportSummary,
    elevation_ft_to_m,
    filter_rows,
    format_check,
    icao_code_of,
    parse_csv,
    parse_row,
    upsert_airports,
)

HEADER = ("id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,"
          "iso_country,iso_region,municipality,scheduled_service,icao_code,iata_code,"
          "gps_code,local_code,home_link,wikipedia_link,keywords")

OHL = (47.6386, 11.2394)


def csv_text(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


ROWS = [
    # small airport 20 km from Ohlstadt (Unterwoessen-like), with ICAO
    '1,EDPU,small_airport,Unterwoessen Airfield,47.7297,12.4382,1837,EU,DE,DE-BY,Unterwoessen,no,EDPU,,EDPU,,,,',
    # medium airport, elevation empty
    '2,EDMA,medium_airport,Augsburg,48.425,10.9317,,EU,DE,DE-BY,Augsburg,no,EDMA,,EDMA,,,,',
    # large airport far away (Frankfurt, ~330 km)
    '3,EDDF,large_airport,Frankfurt,50.0333,8.5706,364,EU,DE,DE-HE,Frankfurt,yes,EDDF,FRA,EDDF,,,,',
    # heliport / closed / seaplane / balloonport: skipped
    '4,DE-0009,heliport,Klinikum Heli,47.64,11.23,2100,EU,DE,DE-BY,Garmisch,no,,,,,,,',
    '5,DE-0010,closed,Alte Wiese,47.65,11.24,2100,EU,DE,DE-BY,Ohlstadt,no,,,,,,,',
    '6,DE-0011,seaplane_base,See,47.66,11.25,2100,EU,DE,DE-BY,See,no,,,,,,,',
    '7,DE-0012,balloonport,Ballon,47.67,11.26,2100,EU,DE,DE-BY,Ballon,no,,,,,,,',
    # invalid coordinates -> rows_invalid
    '8,XX-0001,small_airport,Kaputt,,11.0,100,EU,DE,DE-BY,,no,,,,,,,',
    # synthetic ident without icao code, 60 km away
    '9,DE-0100,small_airport,Wiese Sued,47.10,11.23,3000,EU,AT,AT-7,Tirol,no,,,,,,,',
]


def test_elevation_ft_to_m():
    assert elevation_ft_to_m("1837") == 559.9
    assert elevation_ft_to_m("0") == 0.0
    assert elevation_ft_to_m("") is None
    assert elevation_ft_to_m(None) is None
    assert elevation_ft_to_m("abc") is None


def test_icao_code_prefers_column_then_gps_code_then_ident():
    assert icao_code_of({"icao_code": "EDMU", "gps_code": "", "ident": "EDMU"}) == "EDMU"
    assert icao_code_of({"icao_code": "", "gps_code": "edmu", "ident": "DE-0001"}) == "EDMU"
    assert icao_code_of({"icao_code": "", "gps_code": "", "ident": "EDPU"}) == "EDPU"
    assert icao_code_of({"icao_code": "", "gps_code": "", "ident": "DE-0001"}) is None
    assert icao_code_of({"ident": "K1G3"}) is None  # digits are not ICAO


def test_parse_row_converts_and_truncates():
    rows = list(parse_csv(csv_text(ROWS[0])))
    rec = parse_row(rows[0])
    assert rec.ident == "EDPU" and rec.icao_code == "EDPU"
    assert rec.name == "Unterwoessen Airfield" and rec.type == "small_airport"
    assert rec.latitude == 47.7297 and rec.longitude == 12.4382
    assert rec.elevation_m == 559.9
    assert rec.iso_country == "DE" and rec.municipality == "Unterwoessen"
    assert parse_row({"ident": "X", "name": "Y", "latitude_deg": "91", "longitude_deg": "0"}) is None
    assert parse_row({"ident": "", "name": "Y", "latitude_deg": "1", "longitude_deg": "0"}) is None


def test_filter_rows_by_type_and_radius():
    summary = ImportSummary(radius_km=300)
    kept = filter_rows(parse_csv(csv_text(*ROWS)), [OHL], 300, summary)
    idents = [r.ident for r in kept]
    assert idents == ["EDPU", "EDMA", "DE-0100"]
    assert summary.rows_total == 9
    assert summary.rows_type_kept == 5      # 3 kept + EDDF (radius) + invalid
    assert summary.rows_invalid == 1
    assert summary.rows_kept == 3
    assert summary.by_country == {"DE": 2, "AT": 1}


def test_filter_rows_small_radius_and_all_world():
    kept = filter_rows(parse_csv(csv_text(*ROWS)), [OHL], 50, ImportSummary())
    assert [r.ident for r in kept] == []
    # Unterwoessen and Augsburg are both ~90 km away, Wiese Sued 60 km
    kept = filter_rows(parse_csv(csv_text(*ROWS)), [OHL], 80, ImportSummary())
    assert [r.ident for r in kept] == ["DE-0100"]
    kept = filter_rows(parse_csv(csv_text(*ROWS)), [OHL], 95, ImportSummary())
    assert [r.ident for r in kept] == ["EDPU", "EDMA", "DE-0100"]
    # --all-world: radius None keeps Frankfurt too
    kept = filter_rows(parse_csv(csv_text(*ROWS)), [], None, ImportSummary())
    assert [r.ident for r in kept] == ["EDPU", "EDMA", "EDDF", "DE-0100"]


def test_filter_rows_any_of_several_centres():
    far = (50.0, 8.6)  # near Frankfurt
    kept = filter_rows(parse_csv(csv_text(*ROWS)), [OHL, far], 50, ImportSummary())
    assert [r.ident for r in kept] == ["EDDF"]


class FakeConn:
    def __init__(self, log: list):
        self.log = log

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self_inner):
                conn.log.append(("begin",))
                return None

            async def __aexit__(self_inner, *exc):
                conn.log.append(("commit",))
                return False

        return _Tx()

    async def executemany(self, sql: str, params: list):
        self.log.append(("executemany", sql, list(params)))


class FakeDb:
    def __init__(self):
        self.log: list = []
        self.executed: list[tuple] = []
        self.check_row: dict | None = None

    def acquire(self):
        db = self

        class _Acq:
            async def __aenter__(self_inner):
                return FakeConn(db.log)

            async def __aexit__(self_inner, *exc):
                return False

        return _Acq()

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))

    async def fetch(self, sql: str, *args):
        return [{"latitude": OHL[0], "longitude": OHL[1]}]

    async def fetchrow(self, sql: str, *args):
        assert sql == CHECK_SQL
        assert args == (47.7297, 12.4382)
        return self.check_row


async def test_upsert_is_parametrised_and_transactional():
    db = FakeDb()
    summary = ImportSummary()
    records = filter_rows(parse_csv(csv_text(*ROWS)), [OHL], 300, summary)
    n = await upsert_airports(db, records)
    assert n == 3
    assert db.log[0] == ("begin",) and db.log[-1] == ("commit",)
    kind, sql, params = db.log[1]
    assert kind == "executemany" and sql == UPSERT_SQL
    assert "ON CONFLICT (ident) DO UPDATE" in sql
    assert "ST_MakePoint($6, $5)" in sql        # lon, lat order
    assert "DELETE" not in sql.upper()
    assert params[0] == ("EDPU", "EDPU", "Unterwoessen Airfield", "small_airport",
                         47.7297, 12.4382, 559.9, "DE", "Unterwoessen")
    assert params[1][6] is None                   # Augsburg: no elevation
    # No literal values inside the SQL text
    assert "Unterwoessen" not in sql
    assert await upsert_airports(db, []) == 0


async def test_run_import_uses_given_csv_and_analyzes(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(tool, "get_db", lambda: db)
    summary = await tool.run_import(300, csv_text=csv_text(*ROWS))
    assert summary.upserted == 3 and summary.airfields == 1
    assert db.executed == [("ANALYZE airports", ())]


async def test_main_exit_code_is_1_when_nothing_was_imported(monkeypatch):
    async def noop():
        return None

    summaries = []

    async def fake_run_import(radius_km, csv_text=None):
        return summaries.pop(0)

    monkeypatch.setattr(tool, "init_db", noop)
    monkeypatch.setattr(tool, "close_db", noop)
    monkeypatch.setattr(tool, "run_import", fake_run_import)
    monkeypatch.setattr(ImportSummary, "print", lambda self: None)
    # empty CSV (zero rows) -> failure, not success
    summaries.append(ImportSummary(radius_km=300))
    assert await tool.main([]) == 1
    # rows but nothing in range -> failure
    s = ImportSummary(radius_km=300)
    s.rows_total = 9
    summaries.append(s)
    assert await tool.main(["--radius-km", "10"]) == 1
    # something imported -> success
    s = ImportSummary(radius_km=300)
    s.rows_total, s.upserted = 9, 3
    summaries.append(s)
    assert await tool.main([]) == 0


async def test_run_check_and_format(monkeypatch):
    db = FakeDb()
    db.check_row = {"ident": "EDPU", "icao_code": "EDPU", "name": "Unterwoessen Airfield",
                    "type": "small_airport", "elevation_m": 559.9,
                    "municipality": "Unterwoessen", "dist_m": 12.3}
    monkeypatch.setattr(tool, "get_db", lambda: db)
    row = await tool.run_check(47.7297, 12.4382)
    out = format_check(47.7297, 12.4382, row)
    assert out == ("47.72970 12.43820: Unterwoessen Airfield (EDPU) "
                   "[EDPU, small_airport, 560 m] 12 m")
    assert format_check(1.0, 2.0, None).endswith("no airports imported")


def test_parser_flags():
    p = tool.build_parser()
    args = p.parse_args(["--radius-km", "120"])
    assert args.radius_km == 120 and not args.all_world and args.check is None
    args = p.parse_args(["--all-world"])
    assert args.all_world
    args = p.parse_args(["--check", "47.7297", "12.4382"])
    assert args.check == [47.7297, 12.4382]
