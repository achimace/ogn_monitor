---
name: implement-flight-logic
description: Implementiere Flight Tracking Logik (State Machine, Profile Analysis, Launch Detection)
triggers:
  - "flight logic"
  - "state machine"
  - "flugzustand"
  - "tracking"
  - "startart"
  - "flugprofil"
---

# Flight Logic Implementierung

## Kontext laden (PFLICHT!)
1. Lies `feature.md` komplett - dort stehen ALLE Details zu:
   - Flight State Machine (Section 4)
   - Flugprofil-Analyse (Section 4a)
   - Startart-Erkennung (Section 4b)
2. Lies `app/app/tracking/` fuer bestehenden Code
3. Lies `app/app/config.py` fuer Schwellwerte

## Architektur-Regeln (KRITISCH!)

### Flight Logic gehoert in den WORKER, nicht in die API!
- Alle Dateien in `app/tracking/` werden NUR vom Worker ausgefuehrt
- Der Worker hat In-Memory State (Python Dicts) - kein DB-Query pro Beacon!
- Ergebnisse gehen nach Redis (HSET + PUBLISH), nicht nach PostgreSQL

### Datenfluss
```
APRS Beacon -> beacon_parser -> flight_tracker -> state_machine -> Redis
                                     |                |
                                     v                v
                              launch_detector   profile_analyzer
```

## Flight States (numerisch, siehe init.sql)
```
0 = GROUND        (am Boden, kein aktiver Flug)
1 = TAKEOFF       (Start erkannt, erste Beacons)
2 = FLYING        (normaler Flug)
3 = LANDING       (Landung am Heimatplatz)
4 = OUTLANDING    (Aussenlandung bestaetigt)
5 = ALARM         (kein Signal, Zeitlimit ueberschritten)
6 = TOWING        (im F-Schlepp)
7 = OUTLANDING_PENDING  (Verdacht auf Aussenlandung)
8 = EMERGENCY     (abnormales Flugprofil erkannt)
9 = DIVERTED      (auf anderem Flugplatz gelandet)
10 = SIGNAL_LOST  (Signal verloren, Ursache unklar)
```

## Geo-Berechnungen
- IMMER `geo_calc.py` verwenden
- QDR: Azimut vom Heimatplatz zum Flugzeug (0-360 Grad)
- Distanz: Haversine-Formel
- Himmelsrichtung: 16-Punkt-Kompass (N, NNO, NO, ONO, ...)
- ACHTUNG: Geo-Berechnungen sind CPU-bound!

## Redis Hot State Schema
```python
await redis.hset(f"flight:{airfield_slug}:{flarm_id}", mapping={
    "flarm_id": flarm_id,
    "registration": registration,
    "status": str(status_code),
    "latitude": str(lat),
    "longitude": str(lon),
    "altitude_m": str(alt),
    "qdr_deg": str(qdr),
    "bearing_text": bearing,
    "distance_m": str(dist),
    # ... alle weiteren Felder
})
await redis.publish(f"beacon:{airfield_slug}", json.dumps({
    "flarm_id": flarm_id,
    "type": "update"  # oder "added", "removed", "alarm"
}))
```

## AGL-Quelle: Gelaendemodell vs. Platzhoehe

`FlightTracker` loest fuer Beacons bereits verfolgter Fluege die
Gelaendehoehe unter dem Flugzeug ueber `tracking/elevation.py`
(`ElevationService`, Copernicus-GLO-90-Kacheln in `elevation_tiles`,
Import: `python -m app.tools.import_elevation`) auf und uebergibt sie als
`terrain_m` an `FlightStateMachine.process_beacon`. Die State Machine fuehrt
zwei AGL-Werte (Details im Modul-Docstring):

| Referenz | Verwendung |
|---|---|
| **Platzhoehe** (`agl_af`, `is_high`, `near_ground`) | alles, was an der Heimat-Piste haengt: Bodenkontakt vor dem Start, `takeoff_max_agl_m`, Landeband, Silence-Landing-Kandidat, Touch-&-Go-Konfidenz (`_ground_min_agl`), `release_alt_agl_m` (Abrechnung) |
| **Gelaende** (`agl`) | `FlightState.altitude_agl` (Anzeige, Track-Stream, flight_log), Aussenlande-Erkennung (`outlanding_max_agl_m` / `outlanding_recover_agl_m`), "eindeutig airborne" beim Zuruecknehmen einer Phantom-Funkstille-Landung, `analyze_profile(ground_elevation_m=...)` via `FlightTracker.classify_flight_end` |

Regeln:
- Lookup **vor** dem CPU-gebundenen State-Machine-Aufruf (async, Cache
  ~100-m-Zellen, DB nur bei Miss, Negativ-Cache fuer Regionen ohne Kacheln).
  Nur fuer bereits verfolgte Fluege - nie fuer jeden der tausenden Beacons
  im APRS-Filterradius.
- Kein Wert (keine Kachel, DB-Fehler, `TERRAIN_AGL_ENABLED=false`) =>
  `terrain_m=None` => Platzhoehe wie frueher. Kill switch in `config.py`.
- Copernicus ist ein DSM: ueber Wald ~20-30 m zu hoch. Deshalb bleiben die
  Pisten-Entscheidungen bei der Platzhoehe.
- Tests: `app/tests/test_elevation.py`, `test_flight_state_machine.py`
  (`test_terrain_*`), `test_flight_tracker.py` (`test_terrain_*`).

## F-Schlepp Paar-Erkennung (3 Kriterien!)
1. Distanz < 150m (Seil-Laenge)
2. Hoehendifferenz < 80m
3. **Kurs-Abweichung < 20 Grad** (verhindert False Positives bei parallelen Starts!)

## OGN-DDB Privacy-Flags (tracked / identified) - PFLICHT

Die OGN Device Database steht unter ODbL mit der Auflage *"you must follow
DDB tracking privacy choices"*. `ddb_updater.py` importiert `TRACKED` und
`IDENTIFIED` (Y/N) nach `aircraft_registry`, `AircraftResolver` liefert sie
als `AircraftInfo.tracked / .identified`. Durchgesetzt wird das an genau
einer Stelle: `FlightTracker` (Worker), **vor** jedem Schreibzugriff.

| Flag | Bedeutung | Umsetzung |
|---|---|---|
| `tracked = N` | Halter will nicht verfolgt werden | Beacon wird komplett verworfen: kein State, kein Redis Hot State, kein Track-Stream, kein Event, kein `flight_log`. Gilt **auch**, wenn der Mandant die FLARM-ID in `tenant_aircraft` eingetragen hat (`build_cache` erbt `tracked` aus der DDB). Kippt das Flag per DDB-Reload waehrend eines Fluges, wird der Flug beim naechsten Beacon bzw. in `check_timeouts()` entfernt (`_evict_untracked_flight`: Hot State, Track-Stream und `flight_status`-Zeile geloescht, kein `flight_log`). Beim Recovery aus Redis werden solche Eintraege gepurgt statt wiederhergestellt. Verworfene Beacons werden gezaehlt und hoechstens einmal pro FLARM-ID und Stunde auf `debug` geloggt. |
| `identified = N` | Verfolgen ja, identifizieren nein | Label = nur FLARM-ID. `build_cache` blankt Kennzeichen und Wettbewerbskennzeichen aus der DDB, `update_from_aprs` fuellt sie nie nach (Eintrag existiert im Cache). Modell bleibt (nicht identifizierend, Startart-Erkennung). **Ausnahme:** FLARM-ID in `tenant_aircraft` = eigene Flotte = Einwilligung -> Mandanten-Kennzeichen wird verwendet (`source="tenant"`, `identified=True`). |

Eviction eines Schleppers mitten im Schlepp: der gepaarte Segler verliert
jede Referenz auf ihn (`LaunchDetector.forget_partner`, `tow_plane_flarm_id`
/ `tow_plane_reg` werden geblankt und der Hash neu geschrieben), seine
Startart-Erkennung laeuft ohne Partner weiter. Eine fehlgeschlagene
`flight_status`-Loeschung wird in `check_timeouts()` nachgeholt (begrenztes
Retry-Set, best effort).

Bewusst **nicht** bereinigt beim Kippen auf `tracked = N`:
- Der gemeinsame Stream `positions:{slug}` (alle LFZ eines Platzes) wird
  nicht gepurgt - er rollt von selbst ueber (MAXLEN) und hat aktuell keinen
  Konsumenten.
- Historische `flight_log`-Zeilen frueherer, regulaer archivierter Fluege
  bleiben bestehen; nur der laufende Flug wird ohne `flight_log` verworfen.

Regeln fuer neue Code-Pfade:
- Jeder neue Konsument von Beacons/Flugdaten im Worker haengt hinter
  `FlightTracker._process_beacon_for_airfield` - nie am Parser vorbei.
- Kennzeichen immer aus `AircraftInfo`/`FlightState.registration` nehmen,
  nie direkt aus `Beacon.registration` oder `aircraft_registry`.
- Tests: `app/tests/test_flight_tracker.py` (Drop/Eviction),
  `app/tests/test_aircraft_resolver.py` (Merge-Regeln).

Zweite ODbL-Auflage: keine Weitergabe von OGN-Daten, die aelter als 24 h
sind. Deshalb ist `settings.track_retention_s` per Validator auf 86400 s
begrenzt (`config.py`).

## Checkliste
- [ ] feature.md gelesen fuer die relevante Section
- [ ] DDB-Flags `tracked`/`identified` respektiert (siehe oben) - keine neue Stelle, die Beacons am FlightTracker vorbei verarbeitet
- [ ] Code lebt in app/tracking/ (nicht in app/api/)
- [ ] In-Memory State, keine DB-Queries im Hot Path
- [ ] Redis HSET + PUBLISH nach jedem State Update
- [ ] Alle Schwellwerte aus config.py oder airfield-Config
- [ ] Type Hints ueberall
- [ ] Docstrings mit Erklaerung der Fluglogik
