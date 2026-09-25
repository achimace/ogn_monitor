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
- Lookup **vor** dem CPU-gebundenen State-Machine-Aufruf (async, Subtile-
  Cache im Speicher, DB nur bei Miss, Negativ-Cache fuer Regionen ohne
  Kacheln). Nur fuer bereits verfolgte Fluege - nie fuer jeden der
  tausenden Beacons im APRS-Filterradius.
- Subtile-Cache: `elevation_tiles.rast` sind 64x64-px-Subtiles (~0,053 Grad,
  ~6 km x 4 km bei 47N). Ein Miss laedt mit **einer** Query alle Subtiles,
  die die 0,05-Grad-Rasterzelle der Position schneiden (max. 2x2), inklusive
  aller Pixelwerte (`ST_DumpValues`), und merkt die Zelle als "bekannt"
  (ohne Subtile = Negativ-Eintrag). Jede weitere Position im Subtile ist
  ein reiner Speicherzugriff (`values[row*w+col]`), d. h. eine DB-Query pro
  ~24 km^2 statt pro 100-m-Zelle. Speicher: ein Subtile = 4096 float32 =
  16 KB (`array('f')`); `terrain_cache_max_tiles` (4000 ~ 65 MB) begrenzt
  den Cache, bei Ueberlauf faellt die aeltere Haelfte weg. Warm-up pro
  Flugplatz = eine Query ueber `ST_Buffer(geography, radius)` (10 km ~
  20-40 Subtiles, wenige 100 ms). Einziger DB-Fixkostenblock: der erste
  Raster-Aufruf pro Backend-Verbindung (~100 ms, Extension-Init).
- Kein Wert (keine Kachel, DB-Fehler, `TERRAIN_AGL_ENABLED=false`) =>
  `terrain_m=None` => Platzhoehe wie frueher. Kill switch in `config.py`.
- Copernicus ist ein DSM: ueber Wald ~20-30 m zu hoch. Deshalb bleiben die
  Pisten-Entscheidungen bei der Platzhoehe.
- Tests: `app/tests/test_elevation.py`, `test_flight_state_machine.py`
  (`test_terrain_*`), `test_flight_tracker.py` (`test_terrain_*`).

## Fremde Flugplaetze / Besucher

Quelle: Tabelle `airports` (OurAirports, Migration 011, Import
`python -m app.tools.import_airports`, DEPLOYMENT.md 9b). Der Worker haelt
alle Flugplaetze im Umkreis `airports_index_radius_km` (300 km) um jeden
aktiven Platz im Speicher (`tracking/airports.py`, `AirportIndex`, 0,1-Grad-
Raster, `nearest(lat, lon, max_m)`), laedt sie beim Start und bei jedem
Config-Reload. **Leerer Index = Verhalten wie frueher** (jede Landung
ausserhalb = Aussenlandung, keine Besucher). Verdrahtung wie das
Gelaendemodell: `worker.py` -> `FlightTracker(airports=...)` ->
`FlightStateMachine.airports`.

Schwellwerte (`config.py`, per `AirfieldConfig` ueberschreibbar):

| Wert | Default | Bedeutung |
|---|---|---|
| `foreign_airfield_radius_m` | 2000 | langsam + tief innerhalb dieser Distanz zu einem bekannten Platz = "an diesem Platz" |
| `visitor_zone_km` | 15 | fliegende Fremde innerhalb dieser Distanz zum Heimatplatz werden als Besucher verfolgt |
| `visitor_max_agl_m` | 1500 | Besucher nur unterhalb dieser Hoehe ueber Platz (Streckenflieger hoch drueber sind keine) |
| `foreign_ground_max_entries` | 5000 | Obergrenze der Boden-/Abflug-/Kandidaten-Dicts je Platz (aelteste fliegen raus) |

Regeln (alle in `FlightStateMachine`, Hot Path bleibt billig: Fremd-Beacons
kosten eine Distanz + einen Dict-Lookup, die Platzsuche laeuft nur fuer
*langsame* Beacons):

1. **Landung an fremdem Platz (P2)**: nicht at_home, `is_slow`, Gelaende-AGL
   < `outlanding_max_agl_m` **und** bekannter Platz im Radius, dessen Hoehe
   im gleichen Band wie zu Hause liegt (`abs(alt - Platzhoehe) <=
   near_ground_band_m`, ohne bekannte Platzhoehe nur der Gelaende-AGL-Check)
   -> gleiche Hysterese wie zu Hause, dann `_land(..., airport=...)`:
   `landing_type = "foreign"`, `landing_airfield = "Name (ICAO)"`, Event
   `landing` ("Landung in ..."). Kein Platz -> `OUTLANDING_PENDING` wie
   bisher. Restart / Touch & Go am fremden Platz werden relativ zum Platz
   beurteilt (`_landing_ref_*`), der Folgeflug startet dort
   (`takeoff_airfield`). Eine noch nicht finale Fremdlandung wird
   zurueckgenommen (`landing_retracted`, Log `foreign_landing_retracted`),
   wenn das Flugzeug **abseits** des Platzes (nicht `at_ref`) wieder klar
   in der Luft ist (Gelaende-AGL > `outlanding_recover_agl_m`, schnell) -
   ein tiefer langsamer Vorbeiflug ist keine Landung. Heimatlandungen
   setzen `landing_type = "home"`, `landing_airfield = <Platzname>`;
   Aussenlandungen `"outlanding"` / `""`. `LANDABLE_STATUSES` enthaelt
   auch `OUTLANDING_PENDING`: ein Verdachtsfall, der ins Heimat-Polygon
   rollt, landet zu Hause.
2. **Rueckkehr nach Aussenlandung (P1)**: aus `OUTLANDING`/`DIVERTED` gibt es
   zwei Wege. (a) Ist das Flugzeug auf `OUTLANDING_RECOVER_MIN_BEACONS` (2)
   aufeinanderfolgenden Beacons wieder klar in der Luft (Rohgeschwindigkeit
   >= Startgeschwindigkeit, Gelaende-AGL > `outlanding_recover_agl_m`), wird
   der Aussenlande-Flug wie beim Restart eines gelandeten Flugs mit
   `flight_restarted` archiviert (Landezeit = Beginn des Verdachts,
   `landing_type = "outlanding"`) und ein **neuer** Flug gestartet
   (`takeoff` mit `takeoff_airfield` = naechster Platz oder `"Feld"`,
   Startzeit = erster Luft-Beacon). (b) Taucht es langsam am Boden zu Hause
   auf (Ruecktransport, Funkloch auf dem Rueckweg), wird der Aussenlande-Flug
   mit **`outlanding_returned`** archiviert (Tracker: flight_log + Entfernen
   aus dem Hot State, WS `flight_removed`) und das Flugzeug **vergessen** -
   es ist ab diesem Beacon ein normaler Bodenkontakt am Heimatplatz, ein
   spaeterer echter Start ist ein normaler Heimatflug. Es wird **nie** ein
   Flug ohne Startzeit erfunden. Ein sitzendes Flugzeug bleibt `OUTLANDING`.
3. **Besucher (P3)**: fuer *nicht* verfolgte Flugzeuge ausserhalb des
   Heimatbereichs gilt am naechsten bekannten Platz dieselbe Boden-/Start-
   logik wie zu Hause (Referenz = Platzhoehe, `_foreign_ground`); ein
   bestaetigter Start landet in `_foreign_departures` (12 h). Noch kein
   FlightState. Ist ein unverfolgtes Flugzeug in der Luft innerhalb
   `visitor_zone_km` (Startgeschwindigkeit, `ground_max_agl_m` < AGL <=
   `visitor_max_agl_m`, `takeoff_min_fast_beacons` Beacons in Folge), entsteht
   ein FlightState `FLYING` mit `is_visitor = True`, `takeoff_airfield` /
   `takeoff_time` aus dem Abflug (sonst `"unbekannt"` / leer),
   `visitor_since`, Event **`visitor_arrived`** ("Besucher aus ...";
   API/WS liefern es als `flight_added`). `visitor_zone_km = 0` schaltet
   die Besucher-Erkennung ab. Keine Startart-Erkennung, und ein Besucher
   wird nie als Schleppflugzeug eines Heimatfluges gepaart. Der Besucher
   landet zu Hause wie jeder andere (`landing_type = "home"`, Flugbuch mit
   `takeoff_airfield`, `is_visitor`). **Besucher erreichen nie die
   Aussenlande-/Alarm-Pfade** (`OUTLANDING_PENDING`, `OUTLANDING`,
   `SIGNAL_LOST`, `ALARM`): verlaesst er die Zone wieder (Faktor 1,2),
   verstummt er vor der Landung, geht er abseits eines Platzes langsam und
   tief runter oder landet er an einem *anderen* Platz in der Zone, wird er
   ohne Alarm entfernt (Event **`visitor_left`** mit Grund `left_zone` /
   `signal_lost` / `outlanding` / `landed_elsewhere` -> `flight_removed`;
   Tracker loescht Hash, Track-Stream und `flight_status`-Zeile, kein
   Flugbuch-Eintrag).
4. Ein Flugzeug gehoert zu genau einem Platz: `FlightTracker.process_line`
   bietet verfolgte Flugzeuge nur ihrem Platz an, damit ein Flug von Platz
   B nicht Besucher von Platz A wird.
5. Persistenz: `FlightState.takeoff_airfield / landing_airfield /
   landing_type / is_visitor / visitor_since` im Redis-Hash und in
   `flight_status` / `flight_log` (Synchronizer, parametrisiert).
   `flight_log.takeoff_time` ist NOT NULL: **nur** Besucher ohne bekannten
   Start werden ab `visitor_since` (Notnagel: Landezeit) gebucht. Jeder
   andere Flug hat eine Startzeit; ein Nicht-Besucher ohne Startzeit wird
   nicht geschrieben (Log `flight_archive_skipped_no_takeoff_time`), damit
   nie eine Zeile mit Start == Landung entsteht.
6. VF-Sync (`vfsync/coordinator.py`) ignoriert Events ohne `takeoff_time`
   (kein Uhr-Fallback, kein Anhaengen an eine andere offene Session) und
   alle Events mit `is_visitor = "1"` (Log `vfsync_visitor_ignored`).

Tests: `app/tests/test_foreign_airfields.py` (State Machine + Tracker),
`test_airports.py`, `test_import_airports.py`, `test_state_synchronizer.py`,
`api/test_flight_log_api.py`.

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

## Typfilter, Ignorierliste, Beacon-Plausibilitaet

Motivation (Produktivdaten Ohlstadt): Rettungshubschrauber der Klinik
nebenan (z. B. D-HYAU, ADS-B-Adresstyp "I") schweben langsam ueber den
Platz und wurden als Heimatfluege geloggt; per ADS-B-Relay empfangene
Verkehrsflugzeuge (ICAO 3E71D4, `max_distance_m` 500 km) bekamen einen
"Start in Ohlstadt". Ursachen: keine Typfilterung, und Relay-Beacons mit
Hoehe 0 / Speed 0 erfuellten die Bodenregel, weil `agl_af` stark negativ war.

Alle drei Regeln werden - wie die DDB-Flags - an **einer** Stelle
durchgesetzt: `FlightTracker.process_line` / `_process_beacon_for_airfield`
(Worker), **vor** State Machine, Hot State, Track-Stream, Event und
`flight_log`. Reihenfolge: Plausibilitaet -> `tracked = N` -> Typfilter ->
Ignorierliste des Platzes.

### Flugzeugkategorie (`app/tracking/aircraft_category.py`)

`AircraftCategory` (GLIDER, TOW_PLANE, MOTOR_GLIDER, POWERED, ULTRALIGHT,
HELICOPTER, PARAGLIDER_HANGGLIDER, PARACHUTE_DROP, BALLOON_AIRSHIP, UAV,
STATIC, JET, UNKNOWN) normalisiert zwei Codierungen:

| Quelle | Funktion | Hinweis |
|---|---|---|
| OGN-Beacon, Bits 5..2 des ID-Bytes (`Beacon.device_type`) | `category_from_ogn_type(int)` | 1 Segler, 2 Schlepper, 3 Hubschrauber, 4 Fallschirm, 5 Absetzer, 6 Haengegleiter, 7 Gleitschirm, 8 Motor, 9 Jet, 10 UFO, 11 Ballon, 12 Luftschiff, 13 UAV, 14 reserviert, 15 statisches Hindernis |
| `tenant_aircraft.aircraft_type` / `aircraft_registry.aircraft_type` (String) | `category_from_ddb(str)` | `glider`, `tow_plane`, `motor_glider`, `tmg`, `helicopter`, `powered`, `ultralight`, ... (auch Enum-Werte, deutsche Bezeichnungen, numerischer OGN-Code) |

**Achtung:** Die OGN-DDB hat *keine* Kategorie. Ihr `DEVICE_TYPE` (F / I / O)
ist der **Adresstyp** (FLARM / ICAO / OGN-Tracker) und darf nie als Typ
gelesen werden (`_parse_device_type` in `aircraft_resolver.py` ist eine
Altlast: "I" wuerde "Schlepper" ergeben). `AircraftInfo.category` kommt aus
dem Mandanten-`aircraft_type`, sonst aus `aircraft_registry.aircraft_type`
(manuell pflegbar, von der DDB nie befuellt), sonst UNKNOWN.

Aufloesung pro Beacon (`category_for_beacon`): Kategorie aus Mandant /
Registry, wenn bekannt und nicht UNKNOWN - sonst der Beacon-Typ. Ein als
Segler eingetragenes Flugzeug mit falsch konfiguriertem FLARM-Typ wird also
verfolgt, ein Hubschrauber mit Beacon-Typ 0, den der Mandant als
`helicopter` fuehrt, verworfen.

### Typfilter (`settings.ignored_aircraft_categories`)

Kommaliste von Enum-Werten, Default
`helicopter,balloon_airship,uav,static,parachute_drop`; leer = kein Filter;
unbekannte Namen und `unknown` werden beim Start abgelehnt
(`parse_category_list`). Beacons einer ignorierten Kategorie werden fuer
**alle** Plaetze verworfen wie `tracked = N`: kein State, kein Hot State,
kein Track, kein Event, kein `flight_log`; ein laufender Flug wird beim
naechsten Beacon bzw. in `check_timeouts()` evakuiert (Kategorie aus dem
gespeicherten `flarm_aircraft_type`), beim Recovery aus Redis gepurgt.
Zaehler `FlightTracker._drops["ignored_category"]`, Log
`beacon_dropped_ignored_category` (debug, hoechstens einmal pro FLARM-ID
und Stunde). Schlepper, Motorflugzeuge, Motorsegler und Jets bleiben
verfolgt: sie landen auch mal am Platz und duerfen beim Ueberflug nicht
verschwinden - Bodenkontakt bekommen sie dank der Plausibilitaetsregel
ohnehin nicht.

### Ignorierliste pro Platz (`airfield_ignored_aircraft`, Migration 012)

| Route | Ergebnis |
|---|---|
| `GET /api/airfields/{id}/ignored-aircraft` | `[{id, flarmId, note, createdAt}]` |
| `POST ...` Body `{flarm_id (4-16 hex, wird upper-cased), note?}` | 201 Item, 409 Duplikat, 422 ungueltig |
| `DELETE .../{flarm_id}` | 204, 404 unbekannt |

Auth + `_verify_airfield_ownership` wie die Aircraft-Routen (401 / 403 /
404). Der Worker laedt die Liste mit den Platz-Configs
(`worker.load_airfield_configs` -> `AirfieldConfig.ignored_flarm_ids`,
frozenset, uppercase). Nach jedem erfolgreichen POST / DELETE publiziert
die API den Slug auf dem Redis-Kanal `tracker:config`
(`app/tracking/redis_keys.TRACKER_CONFIG_CHANNEL`); `worker.tracker_config_listener`
loest sofort denselben Reload aus wie die 5-Minuten-Schleife (gemeinsamer
`reload_configs()` + Lock). Best effort: ohne Redis bleibt der DB-Write
gueltig, der periodische Reload holt die Aenderung nach. Der Tracker
behandelt eine ID auf der Liste **nur fuer diesen Platz** wie
`tracked = N` (laufender Flug: Hot State, Track, `flight_status` weg;
`flight_log`-Historie bleibt); andere Plaetze verfolgen das Flugzeug
weiter. Nach dem Reload evakuiert `evict_ignored_flights()` sofort, auch
ohne weiteren Beacon.

### Beacon-Plausibilitaet

- `FlightTracker._is_implausible`: `altitude == 0 and speed == 0` (Relay
  ohne Daten traegt **exakt** 0/0) -> Beacon wird vor allem anderen
  verworfen (Zaehler `_drops["implausible"]`, Log
  `beacon_dropped_implausible`). Bewusst kein `<= 0`: ein geparktes
  Flugzeug an einem Platz auf Meereshoehe meldet durch GPS-Rauschen auch
  mal -10 m bei 0 km/h und ist ein echter Beacon.
- Bodenkontakt (State Machine, `_ground_plausible(altitude_m, ref_elev_m,
  config)`, zu Hause **und** an fremden Plaetzen): die Referenzhoehe wird
  explizit uebergeben - `config.elevation_m` zu Hause, die Hoehe des
  fremden Platzes dort (nie die Heimathoehe). Regel:
  `altitude_m > 0 or ref_elev_m <= 0` **und**
  `abs(altitude_m - ref_elev_m) < ground_max_agl_m`. Das Band ist
  symmetrisch: 30 m *unter* der Platzhoehe (GPS-Rauschen) gilt weiter als
  Boden, 200 m darunter nicht mehr - so wenig wie 200 m darueber. Auf
  Meereshoehe (Referenz <= 0 m) ist 0 m eine echte Hoehe.
- Die Drop-Zaehler (`_drops[reason]`, `_drops_logged_at[reason]`,
  `_untracked_drops`, `_untracked_logged_at`, Key = FLARM-ID) sind ueber
  `_bounded_put` auf `DROP_COUNTER_MAX_ENTRIES` (20 000) Eintraege
  begrenzt - jedes Geraet im APRS-Radius landet dort, sonst wachsen sie
  ueber die Prozesslaufzeit unbegrenzt. Aelteste Eintraege fliegen zuerst.

Tests: `app/tests/test_aircraft_category.py`, `test_aircraft_resolver.py`
(Kategorie-Merge), `test_flight_tracker.py` (Typfilter, Ignorierliste,
Plausibilitaet), `test_beacon_plausibility.py` (State Machine),
`tests/api/test_ignored_aircraft_api.py`, `test_worker_config.py`.

## Checkliste
- [ ] feature.md gelesen fuer die relevante Section
- [ ] DDB-Flags `tracked`/`identified` respektiert (siehe oben) - keine neue Stelle, die Beacons am FlightTracker vorbei verarbeitet
- [ ] Typfilter / Ignorierliste / Plausibilitaet nicht umgangen (Choke Point `FlightTracker`); Typ nur ueber `AircraftCategory`, nie ueber den DDB-Adresstyp
- [ ] Code lebt in app/tracking/ (nicht in app/api/)
- [ ] In-Memory State, keine DB-Queries im Hot Path
- [ ] Redis HSET + PUBLISH nach jedem State Update
- [ ] Alle Schwellwerte aus config.py oder airfield-Config
- [ ] Type Hints ueberall
- [ ] Docstrings mit Erklaerung der Fluglogik
