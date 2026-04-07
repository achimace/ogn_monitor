# Flight Monitor - Verbesserungsplan

## Ueberblick

Kompletter Umbau des Flight Monitors von einer Single-Airfield PHP-Anwendung zu einer mandantenfaehigen, dockerisierten SaaS-Plattform mit moderner Architektur. Jeder Flugplatz kann sich registrieren, seinen Platz konfigurieren und den Monitor in Echtzeit nutzen.

---

## 1. Architektur-Ueberblick (Ziel)

### Design-Prinzipien

1. **Eine Sprache, zwei Rollen:** Python/FastAPI fuer alles - kein Node.js.
   Aber: APRS-Ingestion und API/WebSocket als **getrennte Prozesse** (skalierbar).
2. **Hot State in Redis:** Live-Flugdaten leben in Redis (< 1ms Zugriff).
   Redis ist die zentrale Datenbrücke zwischen APRS-Worker und API.
   PostgreSQL nur fuer persistente Daten (Mandanten, Config, Fluglog).
3. **Delta-Protokoll:** WebSocket sendet nur Aenderungen, nicht den gesamten Status.
4. **5 Container:** Nginx (+ Frontend), APRS-Worker, API (multi-worker), PostgreSQL, Redis.

```
                    +----------------------------+
                    |   Nginx                    |
                    |   + Frontend (static)      |    React Build als Static Files
                    |   + SSL Termination        |    eingebettet via Multi-Stage Docker Build
                    |   + WebSocket Proxy        |
                    |   + Load Balancing         |
                    +-----------+----------------+
                                |
                    +-----------v----------------+
                    |   API Server (FastAPI)     |    Uvicorn --workers 4
                    |                            |    (skalierbar auf N Worker/Kerne)
                    |   - REST API (Auth, CRUD)  |
                    |   - WebSocket Server       |    Liest Hot State aus Redis
                    |   - QDR/Distanz berechnen  |    Subscribed Redis PubSub fuer Push
                    |   - State Sync -> PG       |
                    +-----------+----------------+
                                |
                  +-------------+-------------+
                  |                           |
        +---------v---------+     +-----------v-----------+
        |   PostgreSQL 16   |     |   Redis 7             |
        |   + PostGIS       |     |   Hot State (Flights) |
        |                   |     |   Cache (Aircraft)    |
        |   Persistente     |     |   PubSub (Beacon-     |
        |   Daten:          |     |     Notifications)    |
        |   - Mandanten     |     |   Streams (Positionen)|
        |   - Flugplaetze   |     |                       |
        |   - Fluglog       |     +-----------^-----------+
        |   - Aircraft Reg. |                 |
        +-------------------+     +-----------+-----------+
                                  |   APRS Worker         |    Singleton-Prozess
                                  |   (Python asyncio)    |    (1 Worker, 1 OGN-Verbindung)
                                  |                       |
                                  |   - APRS-IS Client    |    Schreibt in Redis:
                                  |   - Beacon Parser     |    HSET + PUBLISH pro Beacon
                                  |   - Flight Tracker    |
                                  |   - State Machine     |    Flight Logic lebt HIER
                                  |   - Profile Analyzer  |    (nah an den Rohdaten)
                                  |   - Launch Detector   |
                                  +-----------+-----------+
                                              |
                                  +-----------v-----------+
                                  |   aprs.glidernet.org  |
                                  |   Port 14580 (TCP)    |
                                  +-----------------------+
```

### Warum 2 Prozesse statt 1?

Das urspruengliche Design hatte APRS + API in einem einzigen Prozess (`--workers 1`).
**Das Problem:** Geo-Berechnungen (haversine, azimuth), Ringpuffer-Analysen und
DB-Sync sind CPU-bound und blockieren den asyncio Event-Loop. Bei 50 Mandanten
mit 500 Fluegen und hunderten WebSocket-Clients wird ein einzelner Core ueberlastet.

**Die Loesung:** APRS-Ingestion und API/WebSocket als getrennte Container:

| Aspekt | 1-Prozess (Risiko) | 2-Prozesse (jetzt) |
|---|---|---|
| CPU-Nutzung | 1 Core (alles) | Worker: 1 Core + API: 4+ Cores |
| WebSocket-Kapazitaet | ~500 Clients (dann Stau) | ~5000+ Clients (multi-worker) |
| APRS-Parsing blockiert API? | Ja (gleicher Event-Loop) | Nein (getrennte Prozesse) |
| QDR-Berechnung blockiert APRS? | Ja | Nein |
| Skalierung | Vertikal (groesserer Server) | Horizontal (mehr API-Worker) |
| APRS Singleton-Problem | Limitiert alles auf 1 Worker | Nur Worker ist Singleton, API skaliert |
| Kommunikation | In-Process (direkt) | Redis PubSub (< 1ms Overhead) |
| Deployment-Komplexitaet | Einfach (1 Container) | Moderat (2 Container, gleiches Image) |

**Wichtig:** Beide nutzen dasselbe Docker-Image und dieselbe Codebase.
Nur der Startbefehl unterscheidet sich:
- APRS-Worker: `python -m app.worker`
- API-Server: `uvicorn app.main:app --workers 4`

### Komponenten

| Komponente | Technologie | Aufgabe |
|---|---|---|
| **APRS Worker** | Python 3.12 + asyncio | OGN-Verbindung, Beacon-Parsing, Flight Logic, Redis-Writes |
| **API Server** | Python 3.12 + FastAPI + Uvicorn | REST API, WebSocket-Server, Auth, Redis-Reads, PG-Sync |
| **Frontend** | React 19 + Vite + TailwindCSS | SPA, wird als Static Build in Nginx eingebettet |
| **Datenbank** | PostgreSQL 16 + PostGIS | Mandanten, Flugplaetze, Fluglog, Aircraft-Registry |
| **Hot State / Cache** | Redis 7 | Live-Flugstatus, Aircraft-Cache, PubSub, Position-Streams |
| **Reverse Proxy** | Nginx | SSL, Static Files, WebSocket-Proxy, Rate Limiting |
| **Container** | Docker Compose | Orchestrierung aller 5 Dienste |

### Warum Python-Only statt Python + Node.js?

| Aspekt | Python + Node.js (vorher) | Python-Only (jetzt) |
|---|---|---|
| Laufzeitumgebungen | 2 (Python + Node.js) | 1 (Python) |
| Dependency-Systeme | pip + npm | nur pip |
| Codebase | 2 Projekte, 2 Sprachen | 1 Projekt, 1 Sprache, 1 Docker-Image |
| APRS-Bibliotheken | Python (bestes Oekosystem) | Python (nativ) |
| WebSocket-Performance | Node.js (sehr gut) | Uvicorn/Starlette (gleichwertig) |
| Deployment-Komplexitaet | Hoch (heterogene Stacks) | Moderat (homogener Stack) |
| Latenz Beacon->WebSocket | ~5-10ms (Redis-Hop) | ~3-5ms (Redis-Hop, aber gleiche Sprache) |

---

## 2. OGN-Datenanbindung (Kritische Verbesserung)

### Kernprinzip: Grossflaechige Verfolgung

**Jedes Flugzeug, das am Heimatplatz gestartet ist, wird ueberall verfolgt** - egal ob es 5 km oder 500 km entfernt ist. Der Flugleiter im Tower muss jederzeit wissen, wo sich jedes seiner gestarteten Flugzeuge befindet, in welcher Richtung (QDR) und auf welcher Hoehe.

Das bedeutet: Der OGN-Empfangsbereich muss **grossflaechig** sein. Nicht nur der Platzbereich, sondern der gesamte Aktionsradius der Flugzeuge (Alpen, Sueddeutschland, angrenzende Laender).

### Ist-Zustand (Probleme)

- HTTP-Polling auf `live.glidernet.org/lxml.php` alle 3 Sekunden
- Undokumentierte, interne API - kann jederzeit brechen
- Hohe Latenz, Server-Last, keine garantierte Datenvollstaendigkeit
- Gesamter Alpenraum wird abgerufen, aber Flugzeuge werden nur im Platzradius (0.8km) verfolgt

### Soll-Zustand: APRS-IS TCP Stream mit grossflaechigem Tracking

**Verbindung zu `aprs.glidernet.org:14580`** mit grossem Geo-Filter.

```
Protokoll: APRS-IS ueber TCP
Server:    aprs.glidernet.org:14580
Login:     user FLIGHTMON pass -1 vers FlightMonitor 2.0
Filter:    r/47.5/11.5/500  (500km Radius - deckt gesamten Alpenraum + Sueddeutschland ab)
```

**Tracking-Strategie (2-stufig):**

```
Stufe 1: Grossflaechiger Empfang (APRS Filter 500km)
    Alle Beacons im Empfangsbereich werden empfangen und gepuffert.

Stufe 2: Selektives Tracking pro Flugplatz
    Nur Flugzeuge, die am Heimatplatz GESTARTET sind, werden
    aktiv verfolgt. Ueberfliegende Flugzeuge werden ignoriert.

    Erkennung "gestartet am Heimatplatz":
    -> Flugzeug war im Platzradius (z.B. 800m um Ohlstadt)
    -> Geschwindigkeit stieg ueber Threshold
    -> Hoehe stieg ueber Platzhoehe + Offset
    => Ab diesem Moment wird das Flugzeug ueberall verfolgt,
       bis es am Heimatplatz landet oder als vermisst gilt.
```

**Warum 500km Radius:**
- Streckenflug-Segelflugzeuge koennen 500+ km fliegen
- Alpenraum: Ohlstadt -> Brenner (100km), -> Wien (400km), -> Suedfrankreich (500km)
- Auch bei Notlandung in 200km Entfernung muss der Flugleiter das sehen

**Vorteile gegenueber XML-Polling:**

| Aspekt | XML-Polling (alt) | APRS-Stream (neu) |
|---|---|---|
| Latenz | 3+ Sekunden | < 1 Sekunde |
| Datenvollstaendigkeit | Snapshots, Luecken moeglich | Jedes Beacon |
| Serverbelastung OGN | Hoch (HTTP pro Zyklus) | Gering (1 TCP-Verbindung) |
| Datenreichtum | Zusammenfassung | Steigrate, Drehrate, Signalstaerke, GPS-Qualitaet |
| Stabilitaet | Undokumentierte API | Definiertes APRS-IS Protokoll |
| Filterung | Bounding Box via URL | APRS Filter-Syntax (Radius, Prefix, Typ) |
| Reichweite | Fest definiertes Rechteck | Grossflaechig, dynamisch anpassbar |

### Implementierung: APRS-Worker + API-Server (2-Prozess-Modell)

**Dasselbe Docker-Image, zwei Startvarianten:**

- **APRS-Worker** (`python -m app.worker`): Singleton. Verbindet sich mit OGN,
  parst Beacons, fuehrt Flight Logic aus, schreibt Ergebnisse in Redis.
- **API-Server** (`uvicorn app.main:app --workers 4`): Multi-Worker. Liest aus Redis,
  bedient REST + WebSocket, synct nach PostgreSQL.

```
Gemeinsame Codebase (1 Docker-Image):

app/
  main.py                  # FastAPI App (API-Server Entry)
  worker.py                # APRS Worker Entry (asyncio Main-Loop)
  config.py                # Pydantic Settings (Umgebungsvariablen)

  aprs/                    # NUR vom Worker genutzt
    client.py              # APRS-IS TCP Verbindung + Auto-Reconnect + Keepalive
    beacon_parser.py       # OGN-spezifisches Beacon-Parsing (APRS -> Dataclass)
    filter_builder.py      # Dynamische APRS-Filter aus aktiven Flugplaetzen (Konsolidierung)

  tracking/                # NUR vom Worker genutzt
    flight_tracker.py      # Verwaltet aktive Fluege pro Flugplatz
    flight_state_machine.py # Flugzustandslogik (verbessert)
    flight_profile_analyzer.py # Flugprofil-Analyse bei Signalverlust
    flight_profile_buffer.py   # Ringpuffer letzte 10 Min. Beacons
    launch_detector.py     # Startart-Erkennung (Winde/F-Schlepp/Eigen)
    geo_calc.py            # QDR, Distanz, Azimut (wird auch von API fuer REST-Fallback genutzt)

  data/                    # Von Worker UND API genutzt
    aircraft_resolver.py   # FLARM-ID -> Kennzeichen (In-Memory Cache + DB)
    ddb_updater.py         # OGN DDB + FlarmNet Sync (taeglich, laeuft im Worker)
    airfield_manager.py    # Laedt aktive Flugplaetze, Cache mit Reload

  api/                     # NUR vom API-Server genutzt
    auth.py                # Register, Login, JWT, E-Mail-Verifikation
    tenants.py             # Mandanten-CRUD
    airfields.py           # Flugplatz-CRUD
    aircraft.py            # Vereinsflugzeuge CRUD + CSV-Import
    monitor.py             # REST Fallback fuer Monitor-Daten (liest aus Redis)
    flights.py             # Fluglog, Statistik, CSV-Export
    admin.py               # DDB-Sync, OGN-Status, Health
    websocket.py           # WebSocket Handler (/ws/monitor/{slug})

  db/
    connection.py          # asyncpg Connection Pool
    queries.py             # SQL Queries (parametrisiert)
    migrations/            # Alembic Migrationen

  redis_client.py          # Redis Verbindung (aioredis)
  dependencies.py          # FastAPI Dependencies (Auth, Tenant-Context)
```

**Beacon-Verarbeitungs-Pipeline (APRS-Worker -> Redis -> API):**

```
=== APRS WORKER (Singleton-Prozess) ===

APRS Beacon empfangen (asyncio TCP Stream, alle Flugzeuge im konsolidierten Radius)
    |
    v
beacon_parser: APRS-String -> Beacon-Dataclass (lat, lon, alt, speed, vs, track, flarm_id)
    |
    v
flight_tracker: Ist die FLARM-ID bereits als "aktiver Flug" registriert?
    |-- JA -> Position updaten, QDR/Distanz berechnen (geo_calc)
    |         -> Redis HSET flight:{airfield}:{flarm_id} (Hot State, < 1ms)
    |         -> Redis PUBLISH beacon:{airfield} (Notification fuer API-Server)
    |         -> launch_detector: Startart pruefen (erste 3 Min.)
    |         -> profile_buffer: Beacon in Ringpuffer speichern
    |
    |-- NEIN -> Im Platzradius eines konfigurierten Flugplatzes?
                    |-- JA -> Starterkennung pruefen (Hoehe, Geschwindigkeit)
                    |           -> Wenn Start erkannt: als "aktiver Flug" registrieren
                    |           -> Redis HSET + PUBLISH event:{airfield} "flight_added"
                    |-- NEIN -> Beacon verwerfen (ueberfliegendes Flugzeug)


=== API SERVER (Multi-Worker Prozess) ===

Redis SUBSCRIBE beacon:* event:*  (jeder Worker subscribed)
    |
    v
WebSocket broadcast an alle Clients des jeweiligen Flugplatzes
    -> Sendet Delta-Update (nur geaenderte Felder aus Redis Hash)
    -> Latenz: ~3-5ms (Redis Pub/Sub Hop)
```

**Echtzeit QDR- und Distanz-Berechnung:**
Wird **im Worker** bei jedem Beacon berechnet und als fertige Werte in Redis geschrieben:
- **QDR (Peilung):** Azimut vom Heimatplatz zum Flugzeug in Grad (0-360)
- **Distanz:** Entfernung vom Heimatplatz in km/m
- **Richtungstext:** Himmelsrichtung (N, NNO, NO, ONO, O, OSO, SO, SSO, S, SSW, SW, WSW, W, WNW, NW, NNW)

Die API-Server lesen diese fertig berechneten Werte aus Redis und pushen sie per WebSocket.

**Dynamisches Filtering fuer mehrere Mandanten (Konsolidierung):**

APRS-IS erlaubt max. 9 Filter pro Verbindung. Bei vielen Mandanten muessen
ueberlappende Regionen zu einem optimalen Filter konsolidiert werden.

```python
class APRSFilterBuilder:
    """
    Konsolidiert Flugplatz-Positionen zu minimalen APRS-Filtern.
    Ziel: Moeglichst wenig Filter, die alle Flugplaetze abdecken.
    """
    MAX_FILTERS_PER_CONNECTION = 9

    def build_filters(self, airfields: list[Airfield]) -> list[str]:
        if len(airfields) == 0:
            return []

        if len(airfields) == 1:
            af = airfields[0]
            return [f"r/{af.latitude:.3f}/{af.longitude:.3f}/{af.ogn_filter_radius_km}"]

        # Clustering: Nahe Flugplaetze zu einer Region zusammenfassen
        clusters = self._cluster_airfields(airfields)

        filters = []
        for cluster in clusters:
            # Kleinsten umschliessenden Kreis (Minimum Enclosing Circle) berechnen
            center_lat, center_lon, radius_km = self._minimum_enclosing_circle(cluster)
            # Radius = max(Tracking-Radius aller Plaetze im Cluster, Kreis-Radius + Puffer)
            max_tracking = max(af.ogn_filter_radius_km for af in cluster)
            total_radius = max(max_tracking, radius_km + 100)
            filters.append(f"r/{center_lat:.3f}/{center_lon:.3f}/{int(total_radius)}")

        # Wenn > 9 Filter: Weiter zusammenfassen
        while len(filters) > self.MAX_FILTERS_PER_CONNECTION:
            filters = self._merge_closest_filters(filters)

        return filters

# Beispiele:
#   1 Mandant Ohlstadt:
#     -> r/47.658/11.234/500
#
#   3 Mandanten Sueddeutschland (Ohlstadt + Unterwossen + Koenigsdorf):
#     -> r/47.650/11.400/500  (ein Kreis deckt alle 3 ab, Radius reicht)
#
#   Ohlstadt + Hamburg (weit entfernt):
#     -> r/47.658/11.234/500 r/53.500/10.000/400  (zwei separate Filter)
#
#   15 Mandanten deutschlandweit:
#     -> Clustering in max. 9 Regionen, je ein Filter
```

Bei Bedarf: Mehrere APRS-IS Verbindungen (eine pro Region) fuer noch bessere Skalierung.

### FLARM-ID Aufloesung (verbessert, mit In-Memory Cache)

**Drei Datenquellen, taeglicher Sync:**

1. **OGN Device Database (DDB):** `http://ddb.glidernet.org/download/?j=1` (JSON)
   - DEVICE_TYPE, DEVICE_ID, AIRCRAFT_MODEL, REGISTRATION, CN, TRACKED, IDENTIFIED
   - Taeglich herunterladen und in `aircraft_registry`-Tabelle importieren

2. **FlarmNet Database:** `https://www.flarmnet.org/files/downloads/data.fln`
   - FLN-Format (172 Hex-Zeichen pro Zeile = 86 Bytes binaer)
   - Felder: ID, Pilot, Airfield, Aircraft Model, Registration, CN, Freq
   - Woechentlich herunterladen, mit OGN DDB zusammenfuehren

3. **Mandanten-eigene Zuordnungen:** Lokale Vereins-Flugzeuge manuell pflegen
   - Ueberschreibt OGN/FlarmNet-Daten fuer diesen Mandanten
   - Ermoeglicht Zuordnung unregistrierter FLARM-IDs

**Priorisierung:** Mandant-lokal > OGN DDB > FlarmNet > OGN-Stream `reg`-Feld

**In-Memory Cache (Performance-kritisch):**

Bei jedem Beacon wird die FLARM-ID aufgeloest. Das darf **kein DB-Query** sein.

```python
class AircraftResolver:
    """
    Haelt die gesamte Aircraft-Registry im Speicher.
    Lookup: O(1) per Dict, kein DB-Roundtrip.
    """
    def __init__(self):
        self.cache: dict[str, AircraftInfo] = {}  # flarm_id -> AircraftInfo
        self.negative_cache: dict[str, float] = {}  # Unbekannte IDs (Timestamp)
        self.NEGATIVE_TTL = 300  # 5 Min. bevor unbekannte ID erneut geprueft wird
        self.CACHE_RELOAD_INTERVAL = 3600  # 1 Stunde: Cache komplett neu laden

    async def startup_load(self):
        """Beim App-Start: Gesamte Registry + alle Tenant-Aircraft in Cache laden."""
        # 1. OGN DDB + FlarmNet aus aircraft_registry
        rows = await db.fetch_all("SELECT device_id, registration, aircraft_model, ...")
        for row in rows:
            self.cache[row.device_id] = AircraftInfo(...)

        # 2. Tenant-Aircraft (ueberschreibt globale Daten)
        rows = await db.fetch_all("SELECT flarm_id, registration, ... FROM tenant_aircraft")
        for row in rows:
            self.cache[row.flarm_id] = AircraftInfo(..., source="tenant")

    def resolve(self, flarm_id: str) -> AircraftInfo | None:
        """Synchron, < 1 Mikrosekunde. Kein await, kein DB-Query."""
        return self.cache.get(flarm_id)

    async def periodic_reload(self):
        """Alle 60 Min: Cache komplett neu aus DB laden (nach DDB-Sync)."""
        while True:
            await asyncio.sleep(self.CACHE_RELOAD_INTERVAL)
            await self.startup_load()
            self.negative_cache.clear()
```

---

## 3. Mandantenfaehigkeit

### Datenmodell

```sql
-- Mandant (Verein/Organisation)
CREATE TABLE tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,          -- "LSV Ohlstadt"
    slug            VARCHAR(64) UNIQUE NOT NULL,     -- "ohlstadt" (fuer URL)
    email           VARCHAR(255) NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    plan            VARCHAR(32) DEFAULT 'free',      -- free / pro
    is_active       BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- Flugplatz-Konfiguration (1 Mandant = 1..n Flugplaetze)
CREATE TABLE airfields (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID REFERENCES tenants(id) ON DELETE CASCADE,
    name            VARCHAR(255) NOT NULL,          -- "Ohlstadt"
    icao_code       VARCHAR(8),                      -- "EDMO" (optional)
    latitude        DOUBLE PRECISION NOT NULL,       -- 47.658600
    longitude       DOUBLE PRECISION NOT NULL,       -- 11.234790
    elevation_m     INT NOT NULL,                    -- 660
    home_radius_m   INT DEFAULT 800,                 -- Radius fuer Start/Lande-Erkennung
    takeoff_alt_offset_m INT DEFAULT 20,             -- Hoehe ueber Platz fuer Starterkennung
    landing_speed_threshold_kmh INT DEFAULT 50,      -- Max. Geschwindigkeit bei Landung
    alarm_timeout_s INT DEFAULT 600,                 -- Sekunden ohne Update fuer ALARM
    ogn_filter_radius_km INT DEFAULT 150,            -- APRS Filter-Radius
    timezone        VARCHAR(64) DEFAULT 'Europe/Berlin',
    is_active       BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Mandanten-eigene Flugzeug-Zuordnungen
CREATE TABLE tenant_aircraft (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID REFERENCES tenants(id) ON DELETE CASCADE,
    flarm_id        VARCHAR(16) NOT NULL,            -- FLARM Hex-ID
    registration    VARCHAR(16) NOT NULL,             -- "D-KMSF"
    aircraft_model  VARCHAR(64),                      -- "ASG-29"
    competition_sign VARCHAR(8),                      -- "SF"
    is_club_aircraft BOOLEAN DEFAULT false,           -- Vereinsflugzeug
    notes           TEXT,
    UNIQUE(tenant_id, flarm_id)
);

-- Benutzer (optional, fuer Multi-User pro Mandant)
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID REFERENCES tenants(id) ON DELETE CASCADE,
    email           VARCHAR(255) UNIQUE NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    role            VARCHAR(32) DEFAULT 'viewer',    -- admin / editor / viewer
    name            VARCHAR(255),
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Globale Aircraft-Registry (OGN DDB + FlarmNet)
CREATE TABLE aircraft_registry (
    id              SERIAL PRIMARY KEY,
    device_type     CHAR(1) NOT NULL,                -- F/I/O
    device_id       VARCHAR(16) UNIQUE NOT NULL,      -- FLARM Hex-ID
    aircraft_model  VARCHAR(128),
    registration    VARCHAR(32),
    competition_sign VARCHAR(8),
    tracked         BOOLEAN DEFAULT true,
    identified      BOOLEAN DEFAULT true,
    source          VARCHAR(16) NOT NULL,             -- 'ogn_ddb' / 'flarmnet'
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- Live-Flugstatus (pro Flugplatz)
-- Kernidee: Jedes am Heimatplatz gestartete Flugzeug wird hier gefuehrt,
-- egal wie weit es fliegt. QDR und Distanz werden bei jedem Beacon aktualisiert.
CREATE TABLE flight_status (
    id              BIGSERIAL PRIMARY KEY,
    airfield_id     UUID REFERENCES airfields(id) ON DELETE CASCADE,
    flarm_id        VARCHAR(16) NOT NULL,
    registration    VARCHAR(32),
    aircraft_model  VARCHAR(128),
    competition_sign VARCHAR(8),
    status          SMALLINT NOT NULL,                -- 0=ground,1=takeoff,2=flying,3=landing,4=outlanding,5=alarm,7=outlanding_pending
    latitude        DOUBLE PRECISION,                 -- Aktuelle Position
    longitude       DOUBLE PRECISION,                 -- Aktuelle Position
    altitude_m      INT,                              -- Hoehe MSL
    altitude_agl    INT,                              -- Hoehe ueber Heimatplatz (altitude_m - elevation_m)
    speed_kmh       INT,                              -- Geschwindigkeit ueber Grund
    vertical_speed_ms FLOAT,                          -- Steigen/Sinken m/s
    track_deg       INT,                              -- Flugrichtung/Kurs
    distance_m      INT,                              -- Distanz zum Heimatplatz (Echtzeit berechnet)
    qdr_deg         INT,                              -- Peilung vom Heimatplatz (Echtzeit berechnet)
    bearing_text    VARCHAR(4),                        -- Himmelsrichtung (N,NNO,NO,ONO,...SSW,SW,...)
    takeoff_time    TIMESTAMPTZ,
    landing_time    TIMESTAMPTZ,
    last_seen       TIMESTAMPTZ NOT NULL,
    elapsed_s       INT DEFAULT 0,                    -- Sekunden seit letztem OGN-Signal
    max_altitude_m  INT DEFAULT 0,                    -- Maximale Hoehe im Flug
    max_distance_m  INT DEFAULT 0,                    -- Maximale Distanz im Flug
    receiver        VARCHAR(64),                       -- Letzte OGN-Empfaengerstation
    outlanding_pending_since TIMESTAMPTZ,              -- Beginn Aussenlandungs-Verdacht
    -- Startart-Erkennung
    launch_type     VARCHAR(16),                       -- 'winch' / 'aerotow' / 'self' / 'unknown'
    tow_plane_flarm_id VARCHAR(16),                    -- FLARM-ID des Schleppflugzeugs (bei aerotow)
    tow_plane_registration VARCHAR(32),                -- Kennzeichen des Schleppflugzeugs
    release_altitude_m INT,                            -- Ausklink-Hoehe (bei aerotow/winch)
    release_time    TIMESTAMPTZ,                       -- Zeitpunkt des Ausklinkens
    UNIQUE(airfield_id, flarm_id)
);

-- Fluglog (Historie)
CREATE TABLE flight_log (
    id              BIGSERIAL PRIMARY KEY,
    airfield_id     UUID REFERENCES airfields(id) ON DELETE CASCADE,
    flarm_id        VARCHAR(16) NOT NULL,
    registration    VARCHAR(32),
    takeoff_time    TIMESTAMPTZ,
    landing_time    TIMESTAMPTZ,
    landing_type    VARCHAR(16),                      -- 'home' / 'outlanding' / 'unknown'
    landing_location GEOGRAPHY(POINT, 4326),          -- PostGIS Punkt
    -- Startart und Schlepp-Daten (fuer Startschreiber + Abrechnung)
    launch_type     VARCHAR(16),                      -- 'winch' / 'aerotow' / 'self' / 'unknown'
    tow_plane_flarm_id VARCHAR(16),                   -- Schleppflugzeug FLARM-ID
    tow_plane_registration VARCHAR(32),               -- Schleppflugzeug Kennzeichen
    release_altitude_m INT,                           -- Ausklink-Hoehe MSL
    release_altitude_agl INT,                         -- Ausklink-Hoehe ueber Platz (fuer Abrechnung!)
    release_time    TIMESTAMPTZ,                      -- Zeitpunkt des Ausklinkens
    tow_duration_s  INT,                              -- Dauer des F-Schlepps in Sekunden
    max_altitude_m  INT,
    max_distance_m  INT,
    duration_s      INT,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_flight_log_airfield_date ON flight_log(airfield_id, takeoff_time DESC);

-- Positionslog (Rolling, 24h)
CREATE TABLE position_log (
    id              BIGSERIAL PRIMARY KEY,
    flarm_id        VARCHAR(16) NOT NULL,
    location        GEOGRAPHY(POINT, 4326) NOT NULL,
    altitude_m      INT,
    speed_kmh       INT,
    vertical_speed_ms FLOAT,
    track_deg       INT,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
    receiver        VARCHAR(64)
);
CREATE INDEX idx_position_log_flarm_time ON position_log(flarm_id, timestamp DESC);
```

### Registrierungsprozess

```
1. Benutzer oeffnet /register
2. Eingabe: Vereinsname, E-Mail, Passwort
3. E-Mail-Verifikation (Link mit JWT-Token)
4. Nach Verifikation: Login -> Dashboard
5. Flugplatz anlegen:
   - Name, ICAO-Code (optional)
   - Koordinaten (Karte mit Pin oder manuelle Eingabe)
   - Platzhoehe, Pistenrichtung
   - Schwellwerte (Radius, Starthoehe, Landegeschwindigkeit, Alarm-Timeout)
6. Vereinsflugzeuge pflegen:
   - FLARM-ID + Kennzeichen manuell hinzufuegen
   - CSV-Import moeglich
7. Monitor ist sofort live unter /{slug}
```

### API-Endpoints (FastAPI mit automatischer OpenAPI-Doku unter /docs)

```
Auth:
  POST   /api/auth/register          Registrierung
  POST   /api/auth/login             Login (JWT)
  POST   /api/auth/verify-email      E-Mail-Verifikation
  POST   /api/auth/refresh           Token erneuern
  POST   /api/auth/forgot-password   Passwort zuruecksetzen

Tenant:
  GET    /api/tenant                 Eigene Mandanten-Daten
  PUT    /api/tenant                 Mandant aktualisieren
  DELETE /api/tenant                 Mandant loeschen

Airfields:
  GET    /api/airfields              Alle Flugplaetze des Mandanten
  POST   /api/airfields              Neuen Flugplatz anlegen
  GET    /api/airfields/{id}         Flugplatz-Details
  PUT    /api/airfields/{id}         Flugplatz aktualisieren
  DELETE /api/airfields/{id}         Flugplatz loeschen

Aircraft (Mandanten-eigene):
  GET    /api/airfields/{id}/aircraft             Liste
  POST   /api/airfields/{id}/aircraft             Hinzufuegen
  PUT    /api/airfields/{id}/aircraft/{flarm_id}  Aktualisieren
  DELETE /api/airfields/{id}/aircraft/{flarm_id}  Loeschen
  POST   /api/airfields/{id}/aircraft/import-csv  CSV-Import

Monitor (oeffentlich, kein Auth):
  GET    /api/monitor/{slug}         Aktueller Flugstatus (REST Fallback, aus Redis)
  WS     /ws/monitor/{slug}          WebSocket Echtzeit-Feed (Delta-Protokoll)

Flight Log:
  GET    /api/airfields/{id}/flights              Fluglog mit Pagination
  GET    /api/airfields/{id}/flights/stats        Tages-/Wochen-/Monats-Statistik
  GET    /api/airfields/{id}/flights/export-csv   CSV-Export Startschreiber

Health + System:
  GET    /health                     App-Health (fuer Docker Healthcheck)
  GET    /api/health/ogn             OGN-Verbindungsstatus (Beacons/Min, Uptime, Reconnects)
  POST   /api/admin/ddb-sync         DDB/FlarmNet Sync manuell ausloesen
  GET    /docs                       Automatische API-Dokumentation (Swagger UI via FastAPI)
```

### WebSocket Echtzeit-Protokoll (Delta-basiert)

**Design-Prinzip:** Statt alle 3 Sekunden den gesamten Status aller Fluege zu senden,
verwendet das Protokoll **Deltas**: Nur geaenderte Felder werden uebertragen.
Das reduziert den Traffic um ~90%.

```
Client verbindet: ws://host/ws/monitor/ohlstadt

=== 1. ERSTVERBINDUNG: Einmalig vollstaendiger Status ===

Server -> Client (sofort nach Connect + nach jedem Reconnect):
{
  "type": "full_state",
  "airfield": "ohlstadt",
  "timestamp": "2026-03-11T14:23:45Z",
  "flights": [
    {
      "flarmId": "000239",
      "registration": "D-KMSF",
      "aircraftModel": "Ventus ct",
      "competitionSign": "SF",
      "status": "flying",
      "qdrDeg": 195,
      "bearingText": "SSW",
      "distanceM": 32500,
      "altitudeM": 2100,
      "altitudeAgl": 1440,
      "speedKmh": 85,
      "verticalSpeedMs": 1.5,
      "trackDeg": 210,
      "latitude": 47.3812,
      "longitude": 11.1945,
      "takeoffTime": "2026-03-11T10:23:00Z",
      "lastSeen": "2026-03-11T14:23:44Z",
      "elapsedS": 1,
      "maxAltitudeM": 2680,
      "maxDistanceM": 45200,
      "launchType": "aerotow",
      "towPlaneReg": "D-ENNU",
      "releaseAltM": 1150
    }
    // ... weitere Fluege
  ],
  "stats": { "flying": 3, "landed": 4, "alarm": 1, "outlanding": 0 }
}


=== 2. DELTA-UPDATES: Nur geaenderte Felder (bei jedem Beacon, alle 1-5s) ===

Server -> Client:
{
  "type": "flight_update",
  "flarmId": "000239",
  "ts": "2026-03-11T14:23:48Z",
  "d": {                              // "d" = delta (nur geaenderte Felder)
    "qdrDeg": 196,
    "distanceM": 32800,
    "altitudeM": 2120,
    "altitudeAgl": 1460,
    "speedKmh": 87,
    "verticalSpeedMs": 2.1,
    "latitude": 47.3815,
    "longitude": 11.1940,
    "lastSeen": "2026-03-11T14:23:47Z",
    "elapsedS": 0
  }
}
// -> Client merged "d" in sein lokales Flight-Objekt (nur ~200 Bytes statt ~800)


=== 3. STRUKTUR-EVENTS: Neuer Flug / Flug entfernt ===

Server -> Client (Start erkannt):
{
  "type": "flight_added",
  "flight": { ... komplettes Flight-Objekt ... }
}

Server -> Client (Landung, Flug archiviert):
{
  "type": "flight_removed",
  "flarmId": "000239",
  "reason": "landing_home",            // landing_home / outlanding / archived
  "summary": {
    "landingTime": "2026-03-11T14:15:00Z",
    "flightDurationS": 14040,
    "maxAltitudeM": 2680,
    "maxDistanceM": 45200,
    "launchType": "aerotow"
  }
}


=== 4. ALARM-EVENTS: Sofort bei Statuswechsel (hoechste Prioritaet) ===

Server -> Client (Alarm/Emergency/Outlanding):
{
  "type": "alarm",
  "severity": "CRITICAL",              // CRITICAL / HIGH / MEDIUM / LOW
  "flarmId": "000239",
  "registration": "D-KMSF",
  "competitionSign": "SF",
  "aircraftModel": "ASG-29",
  "scenario": "EMERGENCY",             // EMERGENCY / ALARM / OUTLANDED / DIVERTED / SIGNAL_LOST
  "message": "Ploetzlicher Signalverlust mit abnormalem Flugprofil",
  "lastPosition": {
    "latitude": 47.2145,
    "longitude": 11.1823,
    "altitudeM": 1850,
    "qdrDeg": 185,
    "bearingText": "S",
    "distanceKm": 45.2,
    "lastSeen": "2026-03-11T14:11:00Z",
    "elapsedMinutes": 12
  },
  "abnormalFlags": ["Hohe Sinkrate: -8.3 m/s", "Kurs instabil"],
  "nearestAirport": null,
  "timestamp": "2026-03-11T14:23:45Z"
}


=== 5. HEARTBEAT: Verbindung am Leben halten ===

Server -> Client (alle 15 Sekunden):
{ "type": "ping", "ts": "2026-03-11T14:23:45Z" }

Client -> Server (Antwort):
{ "type": "pong" }

// Kein Pong nach 30s -> Server schliesst Verbindung
// Client erkennt Disconnect -> Auto-Reconnect -> full_state anfordern
```

**Traffic-Vergleich (20 aktive Fluege, 1 Beacon/3s):**

| Protokoll | Daten pro Sekunde | pro Minute |
|---|---|---|
| Full-State alle 3s (alt) | ~5 KB (20 Fluege x ~250 Bytes) | ~100 KB |
| Delta-Updates (neu) | ~1.4 KB (7 Deltas x ~200 Bytes) | ~28 KB |
| **Reduktion** | **~72%** | **~72%** |

**Datenfluss (Echtzeit-Pipeline, Worker -> Redis -> API -> Browser):**
```
OGN APRS Beacon empfangen (< 1 Sekunde Latenz)
    |
    v
APRS Worker: Flight Tracker + QDR/Distanz-Berechnung
    |
    v
Redis: HSET (Hot State) + PUBLISH (Notification an API-Server)
    |
    v
API Server: Redis SUBSCRIBE -> WebSocket broadcast (Delta-Update)
    |
    v
Browser: Tabelle + Karte aktualisieren (< 100ms Rendering)
    -> Flugleiter sieht sofort neue QDR, Distanz, Hoehe

Latenz Beacon -> Browser: typisch ~5ms
```

---

## 3b. Redis Hot State Strategie

### Problem: PostgreSQL ist zu langsam fuer Live-Daten

Der Flight Monitor empfaengt alle 1-5 Sekunden ein Beacon pro aktivem Flug.
Bei 50 Mandanten mit je 10 Fluegen = **100-170 PostgreSQL UPDATEs pro Sekunde**.
Das ist unnoetige I/O-Last. Die Live-Daten aendern sich staendig - PostgreSQL
ist dafuer nicht gemacht.

### Loesung: 3-Schichten-Datenhaltung

```
Schicht 1: In-Memory (Python Dict)     <- Schnellste Schicht, < 1 Mikrosekunde
  Im APRS-Worker:
  - flight_tracker.active_flights       Dict[str, FlightState]
  - profile_buffer.beacons              Ringpuffer pro Flugzeug
  - aircraft_resolver.cache             FLARM-ID -> Kennzeichen
  -> Direkt im Worker-Prozess, kein Netzwerk-Overhead
  -> Geht bei Prozess-Neustart verloren (Recovery aus Redis Schicht 2)

Schicht 2: Redis (Hot State)            <- Persistiert ueber Prozess-Neustarts
  - HSET flight:{airfield}:{flarm_id}   Alle Felder des Live-Flugstatus
  - STREAM positions:{airfield}          Positions-Stream (statt position_log in PG)
  -> Ueberlebt App-Restart, Redis-Daten bleiben
  -> Beim Start: Active Flights aus Redis wiederherstellen
  -> TTL: Automatisch bereinigt nach 24h

Schicht 3: PostgreSQL (Cold Storage)    <- Persistente Daten, Abfragen, Historie
  - flight_log                           Archivierte Fluege (bei Start/Landung)
  - flight_profile_snapshot              Profilanalyse bei Alarm (bei Alarm)
  - Mandanten, Flugplaetze, Config       Selten geaendert, beim Start geladen
  - aircraft_registry                    Taeglich aktualisiert (DDB-Sync)
  -> PostgreSQL-Writes nur bei Statuswechsel (Start/Landung/Alarm)
     statt bei jedem Beacon
```

### Redis-Schema fuer Live-Flugstatus

```
# Pro aktiver Flug: Redis Hash mit allen Feldern
HSET flight:ohlstadt:000239
    flarm_id       "000239"
    registration   "D-KMSF"
    aircraft_model "Ventus ct"
    competition_sign "SF"
    status         "2"                  # FLYING
    latitude       "47.3812"
    longitude      "11.1945"
    altitude_m     "2100"
    altitude_agl   "1440"
    speed_kmh      "85"
    vertical_speed_ms "1.5"
    track_deg      "210"
    distance_m     "32500"
    qdr_deg        "195"
    bearing_text   "SSW"
    takeoff_time   "2026-03-11T10:23:00Z"
    last_seen      "2026-03-11T14:23:44Z"
    max_altitude_m "2680"
    max_distance_m "45200"
    launch_type    "aerotow"
    tow_plane_reg  "D-ENNU"
    release_alt_m  "1150"

# Expire nach 24h (Sicherheits-Cleanup)
EXPIRE flight:ohlstadt:000239 86400

# Index aller aktiven Fluege pro Flugplatz
SADD flights:ohlstadt "000239" "001A4F" "00B3C2"

# Positions-Stream (statt PostgreSQL position_log)
XADD positions:ohlstadt MAXLEN ~5000 *
    flarm_id "000239" lat "47.3812" lon "11.1945" alt "2100"
    speed "85" vs "1.5" track "210"
```

### Sync-Strategie: Redis -> PostgreSQL

```python
class StateSynchronizer:
    """Synchronisiert Redis Hot State periodisch nach PostgreSQL."""

    SYNC_INTERVAL = 30  # Sekunden

    async def periodic_sync(self):
        """Alle 30s: Bulk-UPDATE der flight_status-Tabelle aus Redis."""
        while True:
            await asyncio.sleep(self.SYNC_INTERVAL)

            # Alle aktiven Fluege aus Redis lesen
            for airfield_slug in await redis.smembers("active_airfields"):
                flarm_ids = await redis.smembers(f"flights:{airfield_slug}")
                if not flarm_ids:
                    continue

                # Bulk-Read aus Redis (Pipeline)
                pipe = redis.pipeline()
                for fid in flarm_ids:
                    pipe.hgetall(f"flight:{airfield_slug}:{fid}")
                flights = await pipe.execute()

                # Ein einziger Bulk-UPDATE statt 170 Einzel-UPDATEs/Sek
                await self._bulk_update_pg(airfield_slug, flights)

    async def on_status_change(self, flight, old_status, new_status):
        """Sofort nach PostgreSQL bei Statuswechsel (Start/Landung/Alarm)."""
        if new_status in (TAKEOFF, LANDING, ALARM, OUTLANDING, EMERGENCY):
            await self._write_to_flight_log(flight)
```

### Recovery nach Neustart

```python
async def recover_state_from_redis():
    """Beim App-Start: Aktive Fluege aus Redis wiederherstellen."""
    for airfield_slug in await redis.smembers("active_airfields"):
        flarm_ids = await redis.smembers(f"flights:{airfield_slug}")
        for fid in flarm_ids:
            data = await redis.hgetall(f"flight:{airfield_slug}:{fid}")
            if data:
                flight = FlightState.from_redis(data)
                flight_tracker.restore_flight(airfield_slug, flight)
    # -> Kein Datenverlust bei App-Restart oder Deployment!
```

### Write-Reduktion: Vorher vs. Nachher

| Operation | Vorher (alles in PostgreSQL) | Nachher (Redis Hot State) |
|---|---|---|
| Beacon-Update (pro Flug, alle 3s) | 1 PostgreSQL UPDATE | 1 Redis HSET (< 1ms) |
| 50 Fluege aktiv, 1 Beacon/3s | ~17 PG Writes/Sek | 0 PG Writes |
| Periodischer Sync (alle 30s) | - | 1 Bulk-UPDATE (50 Zeilen) |
| Statuswechsel (Start/Landung) | 1 PG Write | 1 PG Write (sofort) |
| **PG Writes/Minute bei 50 Fluegen** | **~1000** | **~5** (nur Sync + Events) |

---

## 4. Verbesserte Flight State Machine

### Kernprinzip: "Gestartet = Verfolgt bis gelandet"

Sobald ein Flugzeug am Heimatplatz als gestartet erkannt wird, wird es **ueberall im gesamten APRS-Empfangsbereich verfolgt** - ob 2 km im Aufwind oder 300 km auf Strecke. Der Flugleiter sieht zu jedem Zeitpunkt:
- **QDR (Peilung):** "Das Flugzeug ist in Richtung 185 Grad (Sueden)"
- **Distanz:** "Es ist 45 km entfernt"
- **Hoehe:** "Es fliegt auf 2100m"
- **Steigen/Sinken:** "+1.5 m/s - es steigt gerade im Bart"

Erst wenn das Flugzeug am Heimatplatz **gelandet** ist, eine **Aussenlandung** erkannt wird, oder es als **vermisst** gilt, endet die Verfolgung.

### Ist-Zustand (Bugs)

- Tracking nur im 0.8km Platzradius - Flugzeuge "verschwinden" beim Wegfliegen
- Status-Werte in `save_status()` sind invertiert (Start schreibt 4, Fliegt schreibt 3)
- Status 2 (Aussenlandung) wird nie gesetzt
- Alarm-Logik inkonsistent zwischen `getogn.php` (3600s) und `get_json.php` (600s)
- Keine Erkennung von Aussenlandungen
- Kein Hysterese-Mechanismus (Flugzeug das kurz unter 50 km/h faellt wird sofort gelandet)
- QDR und Distanz nur als Nebensache angezeigt

### Soll-Zustand

```
Status-Werte (klar definiert):
  GROUND            = 0   Auf dem Boden am Heimatplatz, kein aktiver Flug
  TAKEOFF           = 1   Start erkannt, Flugzeug hebt ab
  FLYING            = 2   Aktiv fliegend (egal wie weit entfernt)
  LANDING           = 3   Landung am Heimatplatz erkannt
  OUTLANDING        = 4   Aussenlandung erkannt (auf Fremdplatz oder im Gelaende)
  ALARM             = 5   Vermisst (kein OGN-Signal seit alarm_timeout_s)
  TOWING            = 6   Im Schlepp (optional)
  OUTLANDING_PENDING = 7  Verdacht auf Aussenlandung (Bestaetigung laeuft)
  EMERGENCY         = 8   Notfall-Verdacht (abnormales Flugprofil vor Signalverlust)
  DIVERTED          = 9   Auf Fremdplatz gelandet (kontrolliert)
  SIGNAL_LOST       = 10  Funkloch (normaler Flug vor Signalverlust)
```

Bei Signalverlust wird das Flugprofil der letzten 10 Minuten analysiert, um zwischen
kontrollierter Landung, Aussenlandung, Funkloch und Notfall zu unterscheiden.
Siehe Abschnitt "Flugprofil-Analyse und Notfallerkennung" fuer Details.

### Tracking-Lebenszyklus eines Fluges

```
Flugzeug steht am Platz (nicht getrackt, nur im APRS-Stream sichtbar)
    |
    v  Starterkennung: Im Platzradius, Hoehe steigt, Speed > 50 km/h
    |
[TAKEOFF] -----> Flug wird registriert, ab jetzt grossflaechig verfolgt
    |
    v  Flugzeug verlaesst Platzradius
    |
[FLYING] ------> Jedes Beacon aktualisiert: QDR, Distanz, Hoehe, Speed, VS
    |             Egal ob 5km oder 500km entfernt
    |             Flugleiter sieht permanent die aktuelle Position
    |
    +--------> Flugzeug kehrt in Platzradius zurueck, Speed < 50 km/h
    |          [LANDING] -> Flug beendet, in flight_log archiviert
    |
    +--------> Flugzeug ist weit weg, niedrig, langsam
    |          [OUTLANDING_PENDING] -> 5 Min. warten
    |              |-> Bewegt sich wieder: zurueck zu [FLYING]
    |              |-> Bleibt stehen: [OUTLANDING]
    |                   -> Naechster Flugplatz wird ermittelt
    |                   -> "Gelandet bei EDMA (Augsburg), 85 km NW"
    |
    +--------> Kein OGN-Signal seit alarm_timeout_s
               [ALARM] -> Letzte bekannte Position wird angezeigt
                   -> "Letzter Kontakt vor 12 Min, 45 km S, 1850m, QDR 185"
                   -> Audio-Alarm im Tower
```

### Verbesserte Logik

```python
class FlightStateMachine:
    """
    Konfigurierbare Parameter pro Flugplatz:
    - home_radius_m:          800    Radius fuer Platznaehe
    - takeoff_alt_offset_m:   20     Hoehe ueber Platz fuer Start
    - landing_speed_kmh:      50     Max. Geschwindigkeit bei Landung
    - alarm_timeout_s:        600    Sekunden ohne OGN-Signal -> ALARM
    - outlanding_timeout_s:   300    Sek. langsam+niedrig ausserhalb -> OUTLANDING
    - hysteresis_s:           10     Geschwindigkeit muss X Sek. unter Threshold
                                     bleiben bevor Landung erkannt wird
    - tracking_radius_km:     500    Maximaler Tracking-Radius (APRS-Filter)
    """

    def process_beacon(self, beacon, flight, config):
        """
        Wird bei JEDEM Beacon eines aktiven Fluges aufgerufen.
        Berechnet QDR und Distanz in Echtzeit.
        """
        # Immer: QDR und Distanz zum Heimatplatz berechnen
        dist_m = haversine(beacon.lat, beacon.lon, config.lat, config.lon)
        qdr = azimuth(config.lat, config.lon, beacon.lat, beacon.lon)
        bearing_text = degrees_to_compass(qdr)  # z.B. "SSW"

        # Position-Update fuer den Flugleiter
        flight.latitude = beacon.lat
        flight.longitude = beacon.lon
        flight.altitude_m = beacon.altitude
        flight.speed_kmh = beacon.speed
        flight.vertical_speed_ms = beacon.vs
        flight.track_deg = beacon.track
        flight.distance_m = dist_m
        flight.qdr_deg = qdr
        flight.bearing_text = bearing_text
        flight.last_seen = utcnow()
        flight.elapsed_s = 0  # Reset: wir haben gerade ein Signal

        # Max-Werte fuer Fluglog
        flight.max_altitude_m = max(flight.max_altitude_m, beacon.altitude)
        flight.max_distance_m = max(flight.max_distance_m, dist_m)

        at_home = dist_m <= config.home_radius_m
        is_slow = beacon.speed < config.landing_speed_kmh

        # --- Starterkennung (nur fuer Flugzeuge OHNE aktiven Flug) ---
        if flight.status == GROUND or flight is None:
            if at_home:
                is_high = beacon.altitude > config.elevation_m + config.takeoff_alt_offset_m
                if is_high and not is_slow:
                    flight.status = TAKEOFF
                    flight.takeoff_time = utcnow()
                    self.publish_event("takeoff", flight)
            return  # Nicht am Platz und kein aktiver Flug -> ignorieren

        # --- Landeerkennung am Heimatplatz ---
        if at_home and is_slow and flight.status in (TAKEOFF, FLYING):
            if self.speed_below_threshold_for(flight, config.hysteresis_s):
                flight.status = LANDING
                flight.landing_time = utcnow()
                self.publish_event("landing", flight)
                self.archive_to_flight_log(flight, landing_type="home")
                return

        # --- Aktiver Flug ausserhalb Platzradius ---
        if flight.status in (TAKEOFF, FLYING):
            flight.status = FLYING
            # Aussenlandungs-Verdacht pruefen
            if not at_home and is_slow and beacon.altitude < self.ground_level_estimate(beacon):
                flight.status = OUTLANDING_PENDING
                flight.outlanding_pending_since = utcnow()
            return

        # --- Aussenlandungs-Bestaetigung ---
        if flight.status == OUTLANDING_PENDING:
            if not is_slow or beacon.altitude > self.ground_level_estimate(beacon) + 100:
                # Flugzeug bewegt sich wieder -> zurueck zu FLYING
                flight.status = FLYING
                flight.outlanding_pending_since = None

    def check_timeouts(self, active_flights, configs):
        """Alle 30 Sekunden aufrufen fuer zeitbasierte Statuswechsel."""
        now = utcnow()
        for flight in active_flights:
            config = configs[flight.airfield_id]
            elapsed = (now - flight.last_seen).total_seconds()
            flight.elapsed_s = int(elapsed)

            # ALARM: Kein Signal seit alarm_timeout_s
            if elapsed > config.alarm_timeout_s:
                if flight.status in (TAKEOFF, FLYING):
                    flight.status = ALARM
                    self.publish_event("alarm", flight, message=
                        f"Kein Signal seit {int(elapsed/60)} Min. "
                        f"Letzte Position: {flight.distance_m/1000:.1f} km "
                        f"{flight.bearing_text}, {flight.altitude_m}m, "
                        f"QDR {flight.qdr_deg}°")

            # OUTLANDING: Steht seit outlanding_timeout_s still
            if flight.status == OUTLANDING_PENDING:
                pending_elapsed = (now - flight.outlanding_pending_since).total_seconds()
                if pending_elapsed > config.outlanding_timeout_s:
                    flight.status = OUTLANDING
                    nearest = self.find_nearest_airport(flight.latitude, flight.longitude)
                    if nearest and nearest.distance_m < 2000:
                        landing_info = f"Gelandet bei {nearest.name} ({nearest.code}), " \
                                       f"{flight.distance_m/1000:.0f} km {flight.bearing_text}"
                    else:
                        landing_info = f"Aussenlandung, " \
                                       f"{flight.distance_m/1000:.1f} km {flight.bearing_text}, " \
                                       f"Position: {flight.latitude:.4f}, {flight.longitude:.4f}"
                    self.publish_event("outlanding", flight, message=landing_info)
                    self.archive_to_flight_log(flight, landing_type="outlanding",
                        landing_location=(flight.latitude, flight.longitude))

    def find_nearest_airport(self, lat, lon):
        """PostGIS-Query: Naechster Flugplatz innerhalb 5km."""
        return self.db.query("""
            SELECT name, code, ST_Distance(
                ST_MakePoint(%s, %s)::geography,
                ST_MakePoint(lon, lat)::geography
            ) as distance_m
            FROM airports
            WHERE ST_DWithin(
                ST_MakePoint(%s, %s)::geography,
                ST_MakePoint(lon, lat)::geography,
                5000
            )
            ORDER BY distance_m ASC LIMIT 1
        """, (lon, lat, lon, lat))
```

### Flugprofil-Analyse und Notfallerkennung

Das System analysiert die **letzten Minuten des Flugprofils** vor dem Signalverlust, um zu erkennen, was mit dem Flugzeug passiert ist. Das ist entscheidend fuer die Sicherheit: Der Flugleiter muss sofort wissen, ob er die Rettungsleitstelle alarmieren muss.

#### Datengrundlage: Position-Ringpuffer

Fuer jedes aktive Flugzeug werden die **letzten 10 Minuten an Beacons** im Speicher gehalten (typisch 1 Beacon alle 3-5 Sekunden = ca. 120-200 Datenpunkte). Daraus laesst sich ein vollstaendiges Flugprofil ableiten.

```python
class FlightProfileBuffer:
    """Ringpuffer der letzten ~10 Minuten Beacons pro Flugzeug."""
    max_age_s = 600  # 10 Minuten

    def add_beacon(self, flarm_id, beacon):
        self.buffer[flarm_id].append({
            "timestamp": beacon.timestamp,
            "lat": beacon.lat,
            "lon": beacon.lon,
            "alt": beacon.altitude,      # Hoehe MSL
            "speed": beacon.speed,        # km/h
            "vs": beacon.vs,              # m/s
            "track": beacon.track,        # Kurs
        })
        self.cleanup_old(flarm_id)
```

#### 4 Szenarien bei Signalverlust

Wenn ein Flugzeug im Status FLYING/TAKEOFF kein OGN-Signal mehr sendet, analysiert das System die letzten Beacons und klassifiziert:

```
SZENARIO 1: Landung auf Fremdplatz (DIVERTED)
  Warnstufe: NIEDRIG (gruen/gelb)
  Erkennung:
    - Kontrollierter Sinkflug in den letzten Minuten
    - VS zwischen -0.5 und -2.0 m/s (normales Sinken)
    - Geschwindigkeit nimmt ab (Landeanflug-Muster)
    - Letzte Position < 2 km zu einem bekannten Flugplatz
    - Letzte Hoehe nahe der Flugplatz-Elevation
    - Kein abrupter Hoehenverlust
  Meldung: "D-KMSF wahrscheinlich gelandet bei EDMA (Augsburg)
            85 km NW, QDR 320°, Landung ca. 14:15 UTC"

SZENARIO 2: Aussenlandung im Gelaende (OUTLANDED)
  Warnstufe: MITTEL (orange)
  Erkennung:
    - Kontrollierter Sinkflug (wie Szenario 1)
    - VS zwischen -0.5 und -2.0 m/s
    - ABER: Kein bekannter Flugplatz in der Naehe (> 2 km)
    - Letzte Hoehe niedrig (< geschaetztes Bodenniveau + 100m)
    - Geschwindigkeit zuletzt < 80 km/h
    - Kein ploetzlicher Abbruch der Daten bei hoher Geschw.
  Meldung: "D-KMSF wahrscheinlich aussengelandet
            45 km S, QDR 185°, Position: 47.2145°N 11.1823°E
            Kontrollierter Sinkflug erkannt. Kein Flugplatz in Naehe.
            Naechster Ort: Mittenwald (2.3 km)"

SZENARIO 3: Ploetzlicher Signalverlust - Verdacht auf Notfall (EMERGENCY)
  Warnstufe: HOCH (rot, blinkend, Audio)
  Erkennung:
    - Signal bricht ab waehrend normalem Flug oder
    - Letzter Beacon zeigt ABNORMALES Profil:
      * Hohe Sinkrate (VS < -5 m/s) in den letzten Beacons
      * ODER ploetzlicher Hoehenverlust (> 200m in < 30 Sek.)
      * ODER stark schwankender Kurs (Trudeln?)
      * ODER hohe Geschwindigkeit + starkes Sinken
      * ODER letzter Beacon auf mittelhoher/grosser Hoehe
        (Signal bricht nicht wegen "hinter Berg" ab -
         Flugzeug war in Sichtweite von OGN-Empfaengern)
    - UND: Keine Anzeichen fuer kontrollierten Anflug
  Meldung: "!!! NOTFALL-VERDACHT: D-KMSF !!!
            Ploetzlicher Signalverlust mit abnormalem Flugprofil.
            Letzte Position: 47.2145°N 11.1823°E
            42 km S, QDR 185°, Hoehe 1850m
            Letzter VS: -8.3 m/s (starkes Sinken)
            Letzter Kurs: stark schwankend (Trudeln?)
            Signal verloren seit: 3 Min.
            >>> Rettungsleitstelle informieren? <<<"

SZENARIO 4: Signalverlust durch OGN-Abdeckungsluecke (SIGNAL_LOST)
  Warnstufe: NIEDRIG (gelb)
  Erkennung:
    - Flugzeug flog normal (stabiler Kurs, normale VS)
    - Signal bricht auf grosser Hoehe ab (> 1500m)
    - Letzte Position in bekanntem Funkloch-Gebiet
      (z.B. Alpentaeler ohne OGN-Empfaenger)
    - Kein abnormales Flugprofil vor Signalverlust
    - Geschwindigkeit und Kurs waren stabil
  Meldung: "D-KMSF: Signal verloren (wahrscheinlich OGN-Abdeckung)
            Letzter Kontakt: 45 km S, QDR 185°, 2100m
            Normaler Flug vor Signalverlust.
            Erwarte Wiederaufnahme bei besserer Abdeckung."
```

#### Profil-Analyse Algorithmus

```python
class FlightProfileAnalyzer:
    """Analysiert die letzten Beacons eines Fluges bei Signalverlust."""

    # Schwellwerte (konfigurierbar pro Flugplatz)
    CRITICAL_SINK_RATE = -5.0       # m/s - abnormal starkes Sinken
    RAPID_ALT_LOSS = 200            # Meter in < 30 Sekunden
    COURSE_DEVIATION_THRESHOLD = 90 # Grad Kursaenderung in < 10 Sek.
    NORMAL_APPROACH_VS_MIN = -3.0   # m/s - noch normaler Sinkflug
    NORMAL_APPROACH_VS_MAX = -0.3   # m/s
    LOW_ALTITUDE_BUFFER = 150       # Meter ueber geschaetztem Boden

    def analyze(self, flight, beacons, airports_db) -> FlightEndClassification:
        if len(beacons) < 3:
            return FlightEndClassification(
                scenario="UNKNOWN",
                severity="HIGH",
                message="Zu wenig Daten fuer Profil-Analyse"
            )

        last = beacons[-1]
        profile = self._compute_profile(beacons)

        # --- Schritt 1: Abnormales Flugprofil erkennen ---
        is_abnormal = self._detect_abnormal_profile(profile)

        # --- Schritt 2: Naechsten Flugplatz pruefen ---
        nearest_airport = self._find_nearest_airport(
            last.lat, last.lon, airports_db
        )

        # --- Schritt 3: Kontrollierter Anflug? ---
        is_controlled_descent = self._detect_controlled_descent(profile)

        # --- Schritt 4: Klassifizierung ---

        # SZENARIO 3: Notfall-Verdacht
        if is_abnormal:
            return FlightEndClassification(
                scenario="EMERGENCY",
                severity="CRITICAL",
                message=self._build_emergency_message(flight, last, profile),
                last_position=(last.lat, last.lon),
                last_altitude=last.alt,
                last_vs=profile.last_vs,
                abnormal_indicators=profile.abnormal_flags,
                action_required="Rettungsleitstelle informieren"
            )

        # SZENARIO 1: Fremdplatz-Landung
        if nearest_airport and nearest_airport.distance_m < 2000:
            if is_controlled_descent or last.speed < 80:
                return FlightEndClassification(
                    scenario="DIVERTED",
                    severity="LOW",
                    message=f"Wahrscheinlich gelandet bei "
                            f"{nearest_airport.name} ({nearest_airport.code})",
                    nearest_airport=nearest_airport
                )

        # SZENARIO 2: Aussenlandung
        if is_controlled_descent or last.alt < self._estimate_ground(last) + self.LOW_ALTITUDE_BUFFER:
            return FlightEndClassification(
                scenario="OUTLANDED",
                severity="MEDIUM",
                message="Wahrscheinlich aussengelandet. "
                        "Kontrollierter Sinkflug erkannt.",
                last_position=(last.lat, last.lon),
                nearest_airport=nearest_airport  # kann None sein
            )

        # SZENARIO 4: Funkloch
        if last.alt > 1500 and not is_abnormal and profile.course_stable:
            return FlightEndClassification(
                scenario="SIGNAL_LOST",
                severity="LOW",
                message="Signal verloren, normaler Flug vor Signalverlust. "
                        "Wahrscheinlich OGN-Abdeckungsluecke."
            )

        # Fallback: Unklare Situation -> sicherheitshalber Warnung
        return FlightEndClassification(
            scenario="UNKNOWN",
            severity="HIGH",
            message="Signalverlust ohne klare Ursache. Situation pruefen.",
            action_required="Funkruf an Pilot versuchen"
        )

    def _detect_abnormal_profile(self, profile) -> bool:
        """Erkennt Anzeichen fuer Absturz/Trudeln/Kontrollverlust."""
        flags = []

        # Extrem hohe Sinkrate
        if profile.last_vs < self.CRITICAL_SINK_RATE:
            flags.append(f"Hohe Sinkrate: {profile.last_vs:.1f} m/s")

        # Schneller Hoehenverlust
        if profile.alt_loss_30s > self.RAPID_ALT_LOSS:
            flags.append(f"Schneller Hoehenverlust: {profile.alt_loss_30s}m in 30s")

        # Kurs-Schwankungen (Trudeln, Spirale)
        if profile.max_course_change_10s > self.COURSE_DEVIATION_THRESHOLD:
            flags.append(f"Starke Kursaenderung: {profile.max_course_change_10s}° in 10s")

        # Kombination: Hohe Speed + Sinken (Sturzflug?)
        if profile.last_speed > 150 and profile.last_vs < -3.0:
            flags.append(f"Hohe Geschw. + Sinken: {profile.last_speed}km/h, "
                         f"{profile.last_vs:.1f}m/s")

        # Beschleunigung nach unten (VS wird immer negativer)
        if profile.vs_trend < -0.5:  # m/s pro Sekunde
            flags.append(f"Zunehmende Sinkrate (Beschleunigung)")

        profile.abnormal_flags = flags
        return len(flags) > 0

    def _detect_controlled_descent(self, profile) -> bool:
        """Erkennt normalen Landeanflug."""
        return (
            self.NORMAL_APPROACH_VS_MIN < profile.avg_vs_last_60s < self.NORMAL_APPROACH_VS_MAX
            and profile.speed_decreasing
            and profile.course_stable
            and profile.alt_decreasing_steadily
        )

    def _compute_profile(self, beacons) -> FlightProfile:
        """Berechnet Profil-Metriken aus den letzten Beacons."""
        profile = FlightProfile()

        # Letzte Werte
        profile.last_vs = beacons[-1].vs
        profile.last_speed = beacons[-1].speed
        profile.last_alt = beacons[-1].alt

        # Hoehenverlust in den letzten 30 Sekunden
        cutoff_30s = beacons[-1].timestamp - timedelta(seconds=30)
        beacons_30s = [b for b in beacons if b.timestamp >= cutoff_30s]
        if len(beacons_30s) >= 2:
            profile.alt_loss_30s = beacons_30s[0].alt - beacons_30s[-1].alt

        # Durchschnittliche VS der letzten 60 Sekunden
        cutoff_60s = beacons[-1].timestamp - timedelta(seconds=60)
        beacons_60s = [b for b in beacons if b.timestamp >= cutoff_60s]
        if beacons_60s:
            profile.avg_vs_last_60s = sum(b.vs for b in beacons_60s) / len(beacons_60s)

        # Kursaenderungen (10-Sekunden-Fenster)
        cutoff_10s = beacons[-1].timestamp - timedelta(seconds=10)
        beacons_10s = [b for b in beacons if b.timestamp >= cutoff_10s]
        if len(beacons_10s) >= 2:
            courses = [b.track for b in beacons_10s]
            max_change = max(
                abs((courses[i+1] - courses[i] + 180) % 360 - 180)
                for i in range(len(courses)-1)
            )
            profile.max_course_change_10s = max_change
            profile.course_stable = max_change < 30

        # Speed-Trend (nimmt ab = Anflug)
        if len(beacons_60s) >= 2:
            speeds = [b.speed for b in beacons_60s]
            profile.speed_decreasing = speeds[-1] < speeds[0] - 10

        # VS-Trend (wird negativer = Beschleunigung nach unten)
        if len(beacons_30s) >= 2:
            vs_values = [b.vs for b in beacons_30s]
            profile.vs_trend = (vs_values[-1] - vs_values[0]) / 30.0

        # Stabiler Sinkflug?
        if beacons_60s:
            alts = [b.alt for b in beacons_60s]
            profile.alt_decreasing_steadily = all(
                alts[i] >= alts[i+1] - 20  # Toleranz 20m
                for i in range(len(alts)-1)
            )

        return profile
```

#### Warnstufen im Tower-Monitor

```
WARNSTUFE    FARBE         VERHALTEN IM MONITOR
-----------------------------------------------------------------------
CRITICAL     Rot blinkend  - Zeile blinkt 1x/Sek
(EMERGENCY)                - Audio-Alarm (laut, wiederholend)
                           - Popup mit allen Notfall-Details
                           - Button: "Rettungsleitstelle anrufen"
                           - Koordinaten gross fuer Weitergabe
                           - Flugprofil-Grafik der letzten 5 Min.
                           - Abnormale Indikatoren aufgelistet

HIGH         Rot statisch  - Zeile rot hinterlegt
(UNKNOWN)                  - Audio-Alarm (einmalig)
                           - "Situation unklar - Pilot kontaktieren"
                           - Letzte Position prominent

MEDIUM       Orange        - Zeile orange hinterlegt
(OUTLANDED)                - Kein Audio (Aussenlandung ist normal)
                           - "Aussengelandet bei [Position/Ort]"
                           - GPS-Koordinaten fuer Rueckholung

LOW          Gelb          - Zeile gelb hinterlegt
(DIVERTED/                 - Kein Audio
 SIGNAL_LOST)              - Informativ: "Gelandet bei [Flugplatz]"
                           - oder "Funkloch, erwarte Signal"
```

#### Flugprofil-Visualisierung bei Alarm

Bei EMERGENCY und HIGH-Alarmen zeigt der Monitor eine **Mini-Flugprofil-Grafik**:

```
Hoehe (m)
2000 |         xxxxxxxx
1800 |        x        x
1600 |       x          x
1400 |      x            x
1200 |     x              x
1000 |    x                xx
 800 |   x                   xxxxx   <-- letzter Kontakt (Signalverlust)
 600 |  x                         ?  <-- Was ist hier passiert?
     +--+---+---+---+---+---+---+---
       -8  -7  -6  -5  -4  -3  -2  -1  min

  VS (m/s):  +1.2  +0.8  -0.3  -1.5  -2.8  -5.1  -8.3  ???
              ^^^^normal^^^^    ^^^^^abnormal^^^^^
```

Diese Grafik hilft dem Flugleiter **sofort** zu erkennen:
- War es ein kontrollierter Sinkflug? (Normaler Anflug-Verlauf)
- Gab es einen ploetzlichen Hoehenverlust? (Strukturversagen, Trudeln)
- Wie waren die letzten Sekunden vor dem Signalverlust?

#### Zusaetzliche Datenfelder fuer die Profil-Analyse

```sql
-- Erweiterung der flight_status-Tabelle:
ALTER TABLE flight_status ADD COLUMN
    signal_loss_scenario VARCHAR(16),        -- DIVERTED/OUTLANDED/EMERGENCY/SIGNAL_LOST/UNKNOWN
    signal_loss_severity VARCHAR(8),         -- CRITICAL/HIGH/MEDIUM/LOW
    signal_loss_message  TEXT,               -- Menschenlesbare Meldung
    signal_loss_flags    TEXT[],             -- Abnormale Indikatoren (Array)
    last_vs_before_loss  FLOAT,             -- Letzte Sinkrate vor Signalverlust
    last_speed_before_loss INT,             -- Letzte Geschwindigkeit
    alt_loss_30s         INT,               -- Hoehenverlust letzte 30s vor Verlust
    nearest_airport_code VARCHAR(8),        -- Naechster Flugplatz bei Signalverlust
    nearest_airport_dist_m INT;             -- Distanz zum naechsten Flugplatz

-- Flugprofil-Snapshots fuer Alarm-Analyse (letzte 10 Min. vor Signalverlust)
CREATE TABLE flight_profile_snapshot (
    id              BIGSERIAL PRIMARY KEY,
    flight_status_id BIGINT REFERENCES flight_status(id) ON DELETE CASCADE,
    beacons         JSONB NOT NULL,         -- Array der letzten ~200 Beacons
    analysis_result JSONB NOT NULL,         -- Ergebnis der Profil-Analyse
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

#### Eskalationslogik

```
Signal verloren (alarm_timeout_s ueberschritten)
    |
    v
Profil-Analyse durchfuehren
    |
    +-- DIVERTED (Fremdplatz) -> Warnstufe LOW
    |     -> Kein Audio, nur Info
    |     -> Nach 30 Min. ohne Nachricht: Hochstufen auf HIGH
    |
    +-- OUTLANDED (Gelaende) -> Warnstufe MEDIUM
    |     -> Kein Audio
    |     -> Nach 15 Min. ohne Nachricht: Hochstufen auf HIGH
    |     -> Nach 30 Min: Hochstufen auf CRITICAL
    |
    +-- SIGNAL_LOST (Funkloch) -> Warnstufe LOW
    |     -> Kein Audio
    |     -> Nach 20 Min. ohne Signal: Hochstufen auf MEDIUM
    |     -> Nach 40 Min: Hochstufen auf HIGH
    |
    +-- EMERGENCY (Notfall) -> Warnstufe CRITICAL (sofort)
    |     -> Audio-Alarm
    |     -> Popup mit Notfall-Details
    |     -> Button "Rettungsleitstelle"
    |
    +-- UNKNOWN -> Warnstufe HIGH
          -> Audio-Alarm (einmalig)
          -> "Situation pruefen"
          -> Nach 10 Min: Hochstufen auf CRITICAL

Jede Stufe kann vom Flugleiter manuell:
  - Quittiert werden ("Ich weiss Bescheid, Pilot hat angerufen")
  - Hochgestuft werden ("Pilot meldet sich nicht -> EMERGENCY")
  - Aufgeloest werden ("Pilot hat angerufen, alles OK")
```

#### Bodenniveau-Schaetzung

Fuer die Erkennung "niedrig = wahrscheinlich gelandet" braucht man eine Schaetzung des Bodenniveaus an der letzten Position. Zwei Ansaetze:

1. **Flugplatz-Elevationen:** Wenn Letztposition nahe einem Flugplatz -> dessen Elevation verwenden
2. **SRTM/DEM-Daten (optional, Phase 2):** Digitales Hoehenmodell (90m Aufloesung).
   Download von NASA SRTM-Daten fuer den Alpenraum (~500MB).
   Erlaubt praezise Bodenniveau-Schaetzung an jeder Position.
3. **Fallback:** Wenn kein DEM verfuegbar -> konservative Schaetzung:
   `geschaetzter_boden = max(airfield_elevation, 300)` Meter MSL

```python
def ground_level_estimate(self, lat, lon):
    """Schaetzt Bodenniveau an einer Position."""
    # Prioritaet 1: Naechster Flugplatz
    airport = self.find_nearest_airport(lat, lon, max_distance=10000)
    if airport:
        return airport.elevation_m

    # Prioritaet 2: DEM-Daten (wenn verfuegbar)
    if self.dem_available:
        return self.dem.get_elevation(lat, lon)

    # Fallback: Konservative Schaetzung
    return max(self.home_elevation, 300)
```

---

## 4b. Startart-Erkennung und F-Schlepp-Tracking

### Warum das wichtig ist

Der Flugleiter und Startschreiber muss wissen:
- **Wie** ist das Flugzeug gestartet? (Windenstart, F-Schlepp, Eigenstart)
- **Bei F-Schlepp:** Welches Schleppflugzeug? In welcher Hoehe ausgeklinkt?
- Die **Ausklink-Hoehe** wird fuer die Abrechnung benoetigt (F-Schlepps werden oft nach Hoehe abgerechnet)

All das laesst sich aus den OGN-Livedaten ableiten, da FLARM-Daten Hoehe, Geschwindigkeit und Position in Echtzeit liefern.

### Die drei Startarten und ihre Signaturen

```
WINDENSTART (winch)
=====================================
Signatur:
  - Segelflugzeug beschleunigt am Boden (0 -> 90+ km/h in wenigen Sekunden)
  - Extrem steiler Steigflug: VS > +8 m/s (oft +10 bis +15 m/s)
  - Kurze Dauer: 30-60 Sekunden vom Start bis Ausklinken
  - Hoehe bei Ausklinken: typisch 300-500m AGL
  - Nach Ausklinken: VS faellt schlagartig (von +12 auf +0...-1 m/s)
  - Geschwindigkeit sinkt (von 90+ auf 70-80 km/h)
  - Kein zweites Flugzeug in der Naehe

Profil:
  Hoehe (m AGL)
  500 |            x  <- Ausklinken (VS bricht ein)
  400 |          x
  300 |        x
  200 |      x
  100 |    x
    0 |--x
      +--+--+--+--+--+--+
        0  10  20  30  40  50 Sek.

  VS: 0 -> +12 -> +15 -> +12 -> +2 -> -0.5 (Ausklinken!)


F-SCHLEPP (aerotow)
=====================================
Signatur:
  - Segelflugzeug UND Schleppflugzeug starten gleichzeitig
  - Beide Flugzeuge sind < 100m voneinander entfernt (Seil: 40-60m)
  - Langsamer, flacherer Steigflug: VS +2 bis +4 m/s
  - Laengere Dauer: 5-15 Minuten
  - Geschwindigkeit: 100-130 km/h (Schleppgeschwindigkeit)
  - Ausklink-Hoehe: typisch 400-1000m AGL (je nach Auftrag)
  - Nach Ausklinken: Segelflugzeug wird langsamer, Schleppflugzeug sinkt ab
  - Schleppflugzeug kehrt zum Platz zurueck (schneller Sinkflug)

Paar-Erkennung:
  Zeitpunkt t:    Segler (lat,lon,alt)    Schlepper (lat,lon,alt)
  10:23:00        47.6586, 11.2348, 670    47.6585, 11.2347, 668   <- Start, <10m Abstand
  10:23:30        47.6590, 11.2355, 700    47.6589, 11.2354, 698   <- zusammen steigend
  10:25:00        47.6620, 11.2380, 820    47.6619, 11.2379, 818   <- zusammen
  10:28:00        47.6680, 11.2420, 1050   47.6679, 11.2419, 1048  <- zusammen
  10:30:00        47.6710, 11.2440, 1150   47.6760, 11.2460, 1100  <- GETRENNT! >100m
  10:30:30        47.6715, 11.2438, 1160   47.6780, 11.2475, 1050  <- Schlepper sinkt

  => Ausklink-Hoehe: 1150m MSL = 490m AGL (bei Platzhoehe 660m)
  => Ausklink-Zeit: 10:30:00 UTC
  => Schlepp-Dauer: 7 Minuten


EIGENSTART (self-launch)
=====================================
Signatur:
  - Motorsegler/TMG beschleunigt am Boden
  - Mittlerer Steigflug: VS +2 bis +5 m/s
  - Hohe Geschwindigkeit beim Start: 100-140 km/h
  - KEIN zweites Flugzeug in der Naehe
  - Steigrate niedriger als Windenstart, hoeher als F-Schlepp ohne Schlepper
  - Aircraft-Typ im OGN-Daten: typ 8 (Motorplane) oder bestimmte Modelle
  - Kein ploetzlicher VS-Einbruch (kein "Ausklinken")

Abgrenzung zum Windenstart:
  - Windenstart: VS > +8 m/s, < 60 Sek, VS bricht schlagartig ein
  - Eigenstart: VS +2...+5 m/s, laenger, kein abrupter VS-Einbruch
```

### Erkennungs-Algorithmus

```python
class LaunchTypeDetector:
    """
    Erkennt Startart anhand der ersten 2-3 Minuten nach dem Start.
    Wird aufgerufen sobald ein Flugzeug als TAKEOFF erkannt wird.
    """

    # Schwellwerte (konfigurierbar pro Flugplatz)
    WINCH_MIN_VS = 8.0              # m/s - Mindest-Steigrate fuer Windenstart
    WINCH_MAX_DURATION_S = 90       # Sek. - Max. Dauer Windenstart
    WINCH_VS_DROP_THRESHOLD = 5.0   # m/s - VS-Abfall beim Ausklinken

    AEROTOW_MAX_DISTANCE_M = 150    # Meter - Max. Abstand Segler-Schlepper
    AEROTOW_MIN_DURATION_S = 120    # Sek. - Min. Dauer F-Schlepp (2 Min.)
    AEROTOW_SEPARATION_DIST_M = 200 # Meter - Ab diesem Abstand = getrennt
    AEROTOW_SPEED_RANGE = (90, 150) # km/h - Typische Schleppgeschwindigkeit

    SELF_LAUNCH_AIRCRAFT_TYPES = [8] # OGN Typ 8 = Motorplane
    SELF_LAUNCH_MODELS = [           # Bekannte Eigenstarter
        "Arcus M", "DG-808", "Ventus 2cM", "ASH-31 MI",
        "Stemme", "Nimbus 4DT", "DG-500 M", "ASG-32 Mi"
    ]

    def __init__(self, airfield_config):
        self.config = airfield_config
        self.pending_detections = {}  # flarm_id -> LaunchDetectionState

    def on_takeoff(self, flight, all_active_flights):
        """Wird aufgerufen wenn ein neuer Start erkannt wird."""
        state = LaunchDetectionState(
            flarm_id=flight.flarm_id,
            takeoff_time=flight.takeoff_time,
            beacons=[],
            is_resolved=False
        )
        self.pending_detections[flight.flarm_id] = state

        # Sofort-Check: Ist es ein bekannter Eigenstarter?
        if self._is_known_self_launcher(flight):
            self._resolve(flight, "self", reason="Bekannter Motorsegler/TMG")

    def on_beacon(self, flight, beacon, all_active_flights):
        """Bei jedem Beacon in den ersten 3 Minuten nach Start."""
        state = self.pending_detections.get(flight.flarm_id)
        if not state or state.is_resolved:
            return

        state.beacons.append(beacon)
        elapsed = (beacon.timestamp - state.takeoff_time).total_seconds()

        # Nach 3 Minuten: Finale Entscheidung treffen
        if elapsed > 180 and not state.is_resolved:
            self._final_classification(flight, state, all_active_flights)
            return

        # --- Laufende Analyse ---

        # Windenstart-Check: Hohe VS in den ersten 60 Sekunden?
        if elapsed < self.WINCH_MAX_DURATION_S:
            if beacon.vs > self.WINCH_MIN_VS:
                state.had_winch_vs = True
                state.max_vs = max(getattr(state, 'max_vs', 0), beacon.vs)

            # Windenstart-Ausklinken erkennen: VS-Einbruch
            if state.had_winch_vs and len(state.beacons) >= 2:
                prev_vs = state.beacons[-2].vs if hasattr(state.beacons[-2], 'vs') else 0
                vs_drop = prev_vs - beacon.vs
                if vs_drop > self.WINCH_VS_DROP_THRESHOLD:
                    self._resolve(flight, "winch",
                        release_altitude=beacon.altitude,
                        release_time=beacon.timestamp,
                        reason=f"Windenstart: max VS {state.max_vs:.1f} m/s, "
                               f"Ausklinken bei {beacon.altitude - self.config.elevation_m}m AGL")
                    return

        # F-Schlepp-Check: Anderes Flugzeug in < 150m Naehe?
        tow_plane = self._find_nearby_aircraft(
            beacon, all_active_flights, flight.flarm_id
        )
        if tow_plane:
            state.tow_plane_id = tow_plane.flarm_id
            state.tow_plane_reg = tow_plane.registration
            state.tow_beacons.append((beacon, tow_plane))

            # Pruefen ob Trennung stattgefunden hat
            if hasattr(state, 'was_paired') and state.was_paired:
                dist_to_tow = haversine(
                    beacon.lat, beacon.lon,
                    tow_plane.latitude, tow_plane.longitude
                )
                if dist_to_tow > self.AEROTOW_SEPARATION_DIST_M:
                    # Trennung erkannt!
                    self._resolve(flight, "aerotow",
                        release_altitude=beacon.altitude,
                        release_time=beacon.timestamp,
                        tow_plane_flarm_id=tow_plane.flarm_id,
                        tow_plane_registration=tow_plane.registration,
                        reason=f"F-Schlepp mit {tow_plane.registration}, "
                               f"Ausklinken bei {beacon.altitude - self.config.elevation_m}m AGL")
                    return

            state.was_paired = True

    AEROTOW_MAX_TRACK_DIFF_DEG = 20  # Max. Kurs-Abweichung im Schlepp

    def _find_nearby_aircraft(self, beacon, all_flights, exclude_flarm_id):
        """
        Sucht ein anderes Flugzeug innerhalb AEROTOW_MAX_DISTANCE_M.

        Prueft 3 Kriterien:
        1. Distanz < 150m (Seil-Laenge)
        2. Hoehendifferenz < 80m (Seil-Durchhang)
        3. Kurs-Abweichung < 20° (beide fliegen in dieselbe Richtung)

        Kriterium 3 verhindert False Positives bei parallelen F-Schlepp-Starts
        oder Wettbewerbsstarts, wo mehrere Gespanne dicht hintereinander starten.
        """
        for other in all_flights:
            if other.flarm_id == exclude_flarm_id:
                continue
            if other.status not in (TAKEOFF, FLYING):
                continue
            dist = haversine(beacon.lat, beacon.lon,
                             other.latitude, other.longitude)
            if dist < self.AEROTOW_MAX_DISTANCE_M:
                # Check 1: Aehnliche Hoehe? (Seil = max 60m Hoehendiff.)
                alt_diff = abs(beacon.altitude - other.altitude_m)
                if alt_diff > 80:
                    continue

                # Check 2: Aehnlicher Kurs? (Im Schlepp fliegen beide gleiche Richtung)
                # Kursdifferenz mit Wrap-Around bei 360°
                track_diff = abs(beacon.track - other.track_deg)
                if track_diff > 180:
                    track_diff = 360 - track_diff
                if track_diff > self.AEROTOW_MAX_TRACK_DIFF_DEG:
                    continue

                return other
        return None

    def _is_known_self_launcher(self, flight):
        """Prueft ob Flugzeugmodell ein bekannter Eigenstarter ist."""
        if flight.aircraft_type in self.SELF_LAUNCH_AIRCRAFT_TYPES:
            return True
        if flight.aircraft_model:
            return any(model in flight.aircraft_model
                       for model in self.SELF_LAUNCH_MODELS)
        return False

    def _final_classification(self, flight, state, all_flights):
        """Finale Entscheidung nach 3 Minuten."""
        if getattr(state, 'had_winch_vs', False):
            # Hohe VS aber kein klarer Ausklink-Moment
            self._resolve(flight, "winch",
                release_altitude=max(b.altitude for b in state.beacons),
                reason="Windenstart (VS-Profil)")
        elif getattr(state, 'tow_plane_id', None):
            # Schleppflugzeug erkannt aber noch nicht getrennt
            # -> F-Schlepp laeuft noch, weiter ueberwachen
            pass  # Wird bei Trennung aufgeloest
        elif self._is_known_self_launcher(flight):
            self._resolve(flight, "self", reason="Eigenstarter (Modell)")
        else:
            self._resolve(flight, "unknown", reason="Startart nicht erkannt")

    def _resolve(self, flight, launch_type, **kwargs):
        """Setzt die Startart und aktualisiert den Flug."""
        flight.launch_type = launch_type
        flight.release_altitude_m = kwargs.get('release_altitude')
        flight.release_time = kwargs.get('release_time')
        flight.tow_plane_flarm_id = kwargs.get('tow_plane_flarm_id')
        flight.tow_plane_registration = kwargs.get('tow_plane_registration')

        state = self.pending_detections.get(flight.flarm_id)
        if state:
            state.is_resolved = True

        # Event an Frontend pushen
        self.publish_event("launch_detected", flight,
            launch_type=launch_type,
            release_altitude_agl=kwargs.get('release_altitude', 0) - self.config.elevation_m,
            **kwargs)
```

### F-Schlepp Paar-Tracking im Detail

Der F-Schlepp ist der komplexeste Fall, weil zwei Flugzeuge als zusammengehoerig erkannt werden muessen.

**Paar-Erkennung (Schritt fuer Schritt):**

```
1. Segelflugzeug A startet am Platz -> Status TAKEOFF
2. Bei jedem Beacon von A: Suche alle anderen aktiven Fluege
   mit Abstand < 150m UND aehnlicher Hoehe (< 80m Differenz)
3. Flugzeug B gefunden? -> Paar erkannt!
   - B ist das Schleppflugzeug
   - A.tow_plane_flarm_id = B.flarm_id
   - Ab jetzt: Bei jedem Beacon Abstand A-B pruefen

4. Solange Abstand < 150m: Paar fliegt zusammen
   - Beide steigen
   - Monitor zeigt: "D-KMSF (SF) im F-Schlepp mit D-ENNU, aktuell 850m AGL"

5. Abstand > 200m: TRENNUNG!
   - Ausklink-Hoehe = Hoehe von A beim letzten Beacon mit Abstand < 150m
   - Ausklink-Zeit = Zeitpunkt dieses Beacons
   - Monitor zeigt: "D-KMSF (SF) ausgeklinkt bei 850m AGL, Schlepp: D-ENNU"
   - Schleppflugzeug B kehrt typischerweise zum Platz zurueck (sinkend)
```

**Sonderfaelle:**
- **Schleppflugzeug nicht im System:** Wenn B kein registriertes Flugzeug ist (z.B. FLARM aus, oder Pilot hat "no tracking"), kann A trotzdem als F-Schlepp erkannt werden anhand des Steigprofils (flacher als Windenstart, laenger als 2 Min, Geschwindigkeit 100-130 km/h)
- **Mehrere F-Schlepps gleichzeitig:** Jedes Paar wird separat verfolgt
- **Abbruch:** Wenn Trennung < 200m AGL -> "F-Schlepp abgebrochen bei niedrig Hoehe" (Warnung)

### Anzeige im Tower-Monitor

```
  FLIEGEND (4)
  +----------+--------+---------------------------------------------------+------+
  |          |        |                                                   |      |
  |  D-KMSF  | 10:23  |   QDR 195°  (SSW)   32.5 km     2100 m     ^    |  OK  |
  |  SF       | F-Schlp|   85 km/h    +1.5 m/s    Kurs 210°              |      |
  |  Ventus  | D-ENNU |   Ausgeklinkt: 490m AGL (1150m MSL) um 10:30    |      |
  +----------+--------+---------------------------------------------------+------+
  |          |        |                                                   |      |
  |  D-1052  | 11:05  |   QDR 320°  (NW)    8.3 km      1280 m     v    |  OK  |
  |  W2       | Winde  |   65 km/h    -0.8 m/s    Kurs 145°              |      |
  |  LS-4    | 420m   |   Windenstart, Ausklink: 420m AGL um 11:05      |      |
  +----------+--------+---------------------------------------------------+------+
  |          |        |                                                   |      |
  |  D-KKKY  | 11:42  |   QDR 090°  (O)     1.2 km       780 m     ^    |  OK  |
  |  C        | Eigen  |   42 km/h    +0.3 m/s    Kurs 270°              |      |
  |  Arcus M |        |   Eigenstart (Motor)                             |      |
  +----------+--------+---------------------------------------------------+------+

  IM SCHLEPP (1)      <- Eigene Sektion fuer laufende F-Schlepps
  +----------+--------+---------------------------------------------------+------+
  |  D-6933  | 12:15  |   QDR 045°  (NO)    0.8 km       920 m     ^    |SCHLEP|
  |  BY       |        |   110 km/h   +2.8 m/s    Kurs 050°              | 260m |
  |  LS-3    | D-ENNU |   Im F-Schlepp mit D-ENNU, aktuell 260m AGL    | AGL  |
  +----------+--------+---------------------------------------------------+------+
```

**Spalte 2 zeigt jetzt die Startart:**

| Anzeige | Bedeutung |
|---|---|
| `F-Schlp` + Kennzeichen | F-Schlepp mit [Schleppflugzeug], ausgeklinkt |
| `Winde` + Hoehe | Windenstart, Ausklink-Hoehe AGL |
| `Eigen` | Eigenstart (Motorsegler/TMG) |
| `SCHLEP` + Hoehe | Aktuell noch im F-Schlepp, aktuelle Hoehe AGL |

**Zusatzinfo in Zeile 3:**
- Bei F-Schlepp: "Ausgeklinkt: 490m AGL (1150m MSL) um 10:30"
- Bei Winde: "Windenstart, Ausklink: 420m AGL um 11:05"
- Bei Eigenstart: "Eigenstart (Motor)"
- Bei laufendem Schlepp: "Im F-Schlepp mit D-ENNU, aktuell 260m AGL"

### F-Schlepp-Abrechnung (Startschreiber-Export)

Der CSV-Export fuer den Startschreiber enthaelt alle Schlepp-Daten:

```csv
Datum;Kennzeichen;Pilot;Startart;Schleppflugzeug;Startzeit;Ausklink_Zeit;Ausklink_Hoehe_AGL;Schlepp_Dauer_Min;Landezeit;Flugzeit;Max_Hoehe;Max_Distanz
2026-03-11;D-KMSF (SF);-;F-Schlepp;D-ENNU;10:23;10:30;490m;7;14:15;3:52;2680m;118km
2026-03-11;D-1052 (W2);-;Winde;-;11:05;11:06;420m;1;13:45;2:40;1850m;45km
2026-03-11;D-KKKY (C);-;Eigenstart;-;11:42;-;-;-;12:30;0:48;1200m;12km
2026-03-11;D-6933 (BY);-;F-Schlepp;D-ENNU;12:15;12:23;650m;8;-;-;-;-
```

**Fuer die Abrechnung entscheidend:**
- Schleppflugzeug-Kennzeichen
- Ausklink-Hoehe AGL (Meter ueber Platz, nicht MSL!)
- Schlepp-Dauer in Minuten

### Schleppflugzeug-Perspektive

Das Schleppflugzeug (z.B. D-ENNU) wird ebenfalls als eigener Flug getrackt. Im Monitor sieht der Flugleiter:

```
  SCHLEPPFLUGZEUG
  +----------+--------+---------------------------------------------------+------+
  |  D-ENNU  | 10:20  |   QDR 045°  (NO)    0.8 km       920 m     ^    |  OK  |
  |  NU       |        |   110 km/h   +2.8 m/s    Kurs 050°              |      |
  |  Robin DR | Schlep.|   Schleppt D-6933 (BY), aktuell 260m AGL       |      |
  |          |        |   Heute: 3 Schlepps, 1420m / 1650m / laufend    |      |
  +----------+--------+---------------------------------------------------+------+
```

Tagesstatistik fuer das Schleppflugzeug:
- Anzahl Schlepps heute
- Ausklink-Hoehen aller Schlepps
- Gesamte Schleppzeit

### Konfiguration pro Flugplatz

```sql
-- Erweiterung der airfields-Tabelle:
ALTER TABLE airfields ADD COLUMN
    winch_available     BOOLEAN DEFAULT true,        -- Hat der Platz eine Winde?
    aerotow_available   BOOLEAN DEFAULT true,        -- Gibt es F-Schlepp?
    self_launch_allowed BOOLEAN DEFAULT true,        -- Eigenstart moeglich?
    tow_plane_flarm_ids TEXT[],                      -- Bekannte Schleppflugzeug FLARM-IDs
                                                     -- (hilft bei Erkennung)
    winch_vs_threshold  FLOAT DEFAULT 8.0,           -- Min. VS fuer Windenstart-Erkennung
    aerotow_pair_distance_m INT DEFAULT 150,         -- Max. Abstand fuer Paar-Erkennung
    aerotow_separation_distance_m INT DEFAULT 200;   -- Min. Abstand fuer Trennung
```

Wenn der Platz seine Schleppflugzeuge konfiguriert hat (`tow_plane_flarm_ids`), wird die F-Schlepp-Erkennung noch zuverlaessiger: Sobald eines dieser Flugzeuge nahe einem startenden Segler ist, ist es definitiv ein F-Schlepp.

---

## 4c. Heimatbereich als Polygon (statt Kreisradius)

### Motivation

Der bisherige `home_radius_m` definiert den Heimatbereich als Kreis um die
Platzkoordinate. Das fuehrt in der Praxis zu Falscherkennungen, wenn andere
Luftverkehrsquellen innerhalb dieses Kreises liegen. Beispiel Ohlstadt:

- Hubschrauber-Station der Unfallklinik Murnau, ca. 2 km vom Platz entfernt
- Bei einem Heimatradius >= 2 km wurden Hubschrauberstarts faelschlich als
  Segelflugstarts erfasst
- Bei einem Heimatradius < 2 km fehlt die Abdeckung der Platzrunde

Ein **frei zeichenbares Polygon** loest das Problem:
- Pistenachse + Vorfeld lassen sich exakt umschliessen
- Stoerquellen wie Murnau bleiben sicher ausserhalb
- Asymmetrische Plaetze (lange Bahn, schmales Vorfeld) werden besser abgedeckt

### Datenmodell

```sql
ALTER TABLE airfields
    ADD COLUMN home_polygon geometry(Polygon, 4326);

CREATE INDEX idx_airfields_home_polygon
    ON airfields USING GIST(home_polygon);
```

`home_radius_m` bleibt erhalten und wirkt als **Fallback**: wenn kein Polygon
gesetzt ist, gilt weiterhin der Kreis. So ist die Migration nicht-brechend.

### Backend

- **Loader** (`worker.py`): liest `ST_AsGeoJSON(home_polygon)` und konvertiert
  zu einem `shapely.geometry.Polygon`
- **AirfieldConfig**: neues optionales Feld `home_polygon: shapely.Polygon | None`
- **State Machine**: in `process_beacon` wird `at_home` so berechnet:
  - wenn Polygon vorhanden: `polygon.contains(Point(lon, lat))`
  - sonst: bisherige Kreisberechnung mit `home_radius_m`
- **Performance**: Punkt-in-Polygon ist im sub-Mikrosekundenbereich, da pro
  Worker nur eine Handvoll Polygone aktiv ist. Kein Geo-Index noetig.

### API

- `GET /api/airfields/{id}` liefert das Polygon als GeoJSON-Feature mit
- `PUT /api/airfields/{id}` akzeptiert ein optionales `home_polygon`-Feld
  (GeoJSON Polygon, oder `null` zum Entfernen)
- Validierung: mindestens 4 Stuetzpunkte (geschlossener Ring), max. 100,
  korrekt geschlossener Ring (`first == last`)

### Frontend

- Neue Karte in `AirfieldConfigPage` (MapLibre + OpenStreetMap-Tiles)
- Werkzeuge:
  - "Polygon zeichnen" (Klick fuer Stuetzpunkte, Doppelklick zum Schliessen)
  - "Bestehendes Polygon bearbeiten" (Stuetzpunkte verschieben/loeschen)
  - "Polygon loeschen" (Fallback auf Kreis)
- Die Karte zeigt:
  - Platzposition (Marker auf `latitude`/`longitude`)
  - Aktuellen Kreisradius als gestrichelte Referenz
  - Aktives Polygon als gefuelltes Overlay

### Migration bestehender Plaetze

- Keine Pflichtmigration: Plaetze ohne Polygon nutzen weiterhin den Kreis
- Pro Platz kann der Betreiber im UI ein Polygon zeichnen, sobald er das
  Bedarf hat (z. B. weil eine Stoerquelle im Kreis liegt)

---

## 5. Frontend (Modernes Dashboard)

### Technologie

- **React 19** mit TypeScript
- **Vite** als Build-Tool
- **TailwindCSS** fuer Styling
- **Zustand** fuer State Management
- **React Router** fuer Navigation
- **Native WebSocket** API fuer Echtzeit-Updates
- **Leaflet** oder **MapLibre GL** fuer optionale Kartenansicht

### Seiten

```
/                           Landing Page (Projektbeschreibung, Login/Register)
/register                   Registrierung
/login                      Login
/dashboard                  Mandanten-Dashboard (nach Login)
/dashboard/airfields        Flugplaetze verwalten
/dashboard/airfields/:id    Flugplatz konfigurieren
/dashboard/aircraft         Vereinsflugzeuge verwalten
/dashboard/flights          Fluglog / Startschreiber-Export
/dashboard/settings         Account-Einstellungen
/:slug                      Oeffentlicher Monitor (kein Login noetig)
/:slug/map                  Monitor mit Kartenansicht
```

### Monitor-Ansicht (Kernfeature) - "Tower-Radar"

Der Monitor ist das primaere Arbeitswerkzeug des Flugleiters. Die wichtigsten Informationen auf einen Blick: **Wo ist das Flugzeug (QDR + Distanz) und auf welcher Hoehe?**

**Design-Prinzip:** QDR, Distanz und Hoehe sind die groessten, prominentesten Elemente. Der Flugleiter muss diese Werte aus 3 Metern Entfernung am Tower-Monitor ablesen koennen.

```
+================================================================================+
|  FLIGHT MONITOR - Ohlstadt (EDMO)                    11.03.2026  14:23:45 UTC  |
|  [Tabelle]  [Karte]                                    Verbunden | 7 getrackt  |
+================================================================================+

  !!! NOTFALL (1) !!!                                        [Ton an/aus] [112]
  +----------+--------+---------------------------------------------------+------+
  |          |        |                                                   |      |
  |  D-KXYZ  | 10:15  |   QDR 185°  (S)     45.2 km     1850 m          |NOTFAL|
  |  00A3F2  |        |                                                   | seit |
  |  ASG-29  |        |   !!! Abnormales Flugprofil vor Signalverlust    |3 min |
  |          |        |   Letzter VS: -8.3 m/s, Kurs instabil           |      |
  |          |        |   Pos: 47.2145° N  11.1823° E                    |      |
  |          |        |   [Flugprofil anzeigen] [Quittieren] [Entwarnung]|      |
  +----------+--------+---------------------------------------------------+------+

  SIGNAL VERLOREN (1)                                                [Ton an/aus]
  +----------+--------+---------------------------------------------------+------+
  |  D-6322  | 09:45  |   QDR 210°  (SSW)   62.0 km     2200 m          | LOST |
  |  HUN     |        |   Normaler Flug vor Signalverlust                | 8 min|
  |  Discus  |        |   Wahrsch. OGN-Funkloch. Erwarte Signal.        |      |
  +----------+--------+---------------------------------------------------+------+

  FLIEGEND (3)
  +----------+--------+---------------------------------------------------+------+
  |          |        |         QDR        Distanz       Hoehe            |      |
  |  D-KMSF  | 10:23  |   QDR 195°  (SSW)   32.5 km     2100 m     ^    |  OK  |
  |  SF       |        |   85 km/h    +1.5 m/s    Kurs 210°              |      |
  +----------+--------+---------------------------------------------------+------+
  |          |        |                                                   |      |
  |  D-1052  | 11:05  |   QDR 320°  (NW)    8.3 km      1280 m     v    |  OK  |
  |  W2       |        |   65 km/h    -0.8 m/s    Kurs 145°              |      |
  +----------+--------+---------------------------------------------------+------+
  |          |        |                                                   |      |
  |  HB-2393 | 11:42  |   QDR 090°  (O)     1.2 km       780 m     ^    | Time |
  |  RM       |        |   42 km/h    +0.3 m/s    Kurs 270°              | out  |
  +----------+--------+---------------------------------------------------+------+
  |          |        |                                                   |      |
  |  D-6933  | 12:15  |   QDR 175°  (S)    112.8 km     2450 m     ^    |  OK  |
  |  BY       |        |   95 km/h    +2.1 m/s    Kurs 180°              |      |
  |  LS-3    |        |   Streckenflug, max 2680m, max 118km             |      |
  +----------+--------+---------------------------------------------------+------+

  GELANDET (4)
  +----------+--------+---------------------------------------------------+------+
  |  D-8997  | 09:30  |   Landung 12:15 UTC am Heimatplatz               |  OK  |
  |  IV       | LDG    |   Flugzeit: 2:45    Max: 1850m / 45km            |      |
  +----------+--------+---------------------------------------------------+------+
  |  D-KKKY  | 08:45  |   Aussenlandung 11:30 UTC bei EDMA (Augsburg)    | OUT  |
  |  C        | OUT    |   85 km NW    Flugzeit: 2:45                     |      |
  +----------+--------+---------------------------------------------------+------+

+================================================================================+
|  Letztes Update: vor 1s  |  OGN: verbunden  |  Naechster Alarm-Check: 28s     |
+================================================================================+
```

**Detail-Erklaerung der Spalten:**

```
Spalte 1 - KENNZEICHEN:
  Zeile 1: Registration (gross, fett, farbcodiert)
  Zeile 2: Wettbewerbsnummer (CN)
  Zeile 3: Flugzeugtyp (optional, bei Platz)

Spalte 2 - ZEIT:
  Startzeit UTC (bei fliegenden)
  "LDG" oder "OUT" Label (bei gelandeten)

Spalte 3 - POSITION (Hauptinformation fuer Flugleiter):
  Zeile 1: QDR [Grad]° ([Himmelsrichtung])   [Distanz] km   [Hoehe] m   [^/v Steig/Sink-Pfeil]
  Zeile 2: [Speed] km/h   [VS] m/s   Kurs [Track]°
  Zeile 3: Zusatzinfo (Streckenflug-Stats, Aussenlandungs-Ort, Alarm-Details)

Spalte 4 - STATUS:
  OK (gruen), Timeout (gelb), ALARM (rot blinkend), OUT (orange)
  Bei Alarm: Minuten seit letztem Signal
```

**Farbschema (Flugleiter-optimiert):**

| Element | Farbe | Bedeutung |
|---|---|---|
| Hintergrund | Dunkelgrau `#1a1a2e` | Augenschonend fuer langen Tower-Einsatz |
| ALARM-Zeile | Rot blinkend `#dc3545` | Sofort ins Auge springend |
| ALARM QDR/Hoehe | Weiss auf Rot | Letzte bekannte Position hervorgehoben |
| FLYING-Zeile | Dunkelblau `#16213e` | Normaler Flugbetrieb |
| QDR-Grad | Gross, weiss, fett `24px` | Primaere Info fuer Flugleiter |
| Distanz | Gross, cyan `#00d4ff` | Gut sichtbar |
| Hoehe | Gross, gelb `#ffc107` | Gut sichtbar, anders als Distanz |
| Steigpfeil ^ | Gruen `#28a745` | Steigt |
| Sinkpfeil v | Orange `#fd7e14` | Sinkt |
| GELANDET-Zeile | Dunkelgruen `#1b4332` | Abgeschlossen, weniger prominent |
| OUTLANDING-Zeile | Dunkelorange `#7c4a03` | Aufmerksamkeit, aber kein Alarm |

**Standard-Darstellung: Dark Mode** (Tower-Betrieb bei allen Lichtverhaeltnissen). Light Mode als Option.

### Steig/Sink-Indikator

Visuell sofort erkennbar:
- **Pfeil hoch (^)** gruen: Flugzeug steigt (VS > +0.3 m/s)
- **Pfeil runter (v)** orange: Flugzeug sinkt (VS < -0.3 m/s)
- **Strich (-)** grau: Geradeausflug (VS zwischen -0.3 und +0.3)
- **Doppelpfeil (^^)** gruen fett: Starkes Steigen (> +2.0 m/s) - im Bart!
- **Doppelpfeil (vv)** rot: Starkes Sinken (< -3.0 m/s) - Achtung

### Alarm-Verhalten im Tower

Das Alarmverhalten unterscheidet sich je nach Ergebnis der Flugprofil-Analyse:

**EMERGENCY (Notfall-Verdacht - abnormales Flugprofil):**
1. **Gesamte Zeile blinkt rot** (CSS Animation, 0.5s Intervall - schneller als normaler Alarm)
2. **Lauter Audio-Alarm** (wiederholend bis Quittierung)
3. **Popup-Overlay** mit allen Notfall-Details:
   - Letzte Position (QDR, Distanz, Koordinaten - gross, kopierbar)
   - Flugprofil-Grafik der letzten 5 Minuten (Hoehe + VS)
   - Abnormale Indikatoren aufgelistet ("Hohe Sinkrate: -8.3 m/s", "Kurs instabil")
   - Button **"Rettungsleitstelle anrufen"** (prominent, rot)
   - Button "Quittieren" (Flugleiter weiss Bescheid)
   - Button "Entwarnung" (Pilot hat sich gemeldet)
4. NOTFALL-Sektion steht **ueber allen anderen Sektionen**

**ALARM (Signalverlust ohne klare Ursache):**
1. **Zeile blinkt rot** (1s Intervall)
2. **Audio-Signal** (einmalig)
3. Letzte Position mit QDR, Distanz, Hoehe, Koordinaten
4. Buttons: "Quittieren", "Hochstufen auf NOTFALL", "Entwarnung"

**OUTLANDING (Aussenlandung erkannt):**
1. **Zeile orange** (kein Blinken)
2. **Kein Audio** (Aussenlandung ist normal im Segelflug)
3. Position + naechster Ort/Flugplatz
4. Nach 15 Min. ohne Nachricht vom Piloten: automatisch hochstufen auf ALARM

**SIGNAL_LOST (Funkloch):**
1. **Zeile gelb** (kein Blinken)
2. Kein Audio
3. "Normaler Flug vor Signalverlust" + letzte Position
4. Nach 20 Min.: hochstufen auf ALARM, nach 40 Min.: EMERGENCY

**Automatische Entwarnung:**
- Flugzeug sendet wieder ein OGN-Signal -> sofort zurueck zu FLYING
- Alle Alarm-Anzeigen verschwinden, Audio stoppt
- Event wird im Fluglog protokolliert ("Signal verloren 14:11-14:23, Ursache: Funkloch")

**Manuelle Aktionen des Flugleiters:**
- **Quittieren:** "Ich habe den Alarm gesehen" (Audio stoppt, Blinken bleibt)
- **Entwarnung:** "Pilot hat sich gemeldet, alles OK" (Alarm aufgeloest)
- **Hochstufen:** "Pilot meldet sich nicht" -> naechste Warnstufe
- **Rettung:** "Rettungsleitstelle informiert" (wird im Log protokolliert)

### Kartenansicht (gleichberechtigt neben Tabelle)

Die Karte ist **nicht optional** sondern ein gleichwertiger View fuer den Flugleiter. Umschaltbar per Tab [Tabelle] / [Karte] oder Side-by-Side auf grossen Monitoren.

- **OpenStreetMap + Topographische Karte** (Gelaende wichtig fuer Segelflieger)
- Flugzeuge als **Richtungspfeile** mit Kennzeichen-Label
- Farbcodierung: blau=fliegend, gruen=gelandet, rot=alarm, orange=aussenlandung
- **Heimatplatz** mit Radius-Kreis (800m)
- **QDR-Linie** vom Heimatplatz zum Flugzeug (gestrichelt, mit Gradzahl)
- **Flugspur** der letzten 60 Minuten (aus position_log), eingefaerbt nach Hoehe
- Klick auf Flugzeug: Detail-Popup mit allen Werten
- **Alarmierte Flugzeuge:** Pulsierender roter Kreis an letzter Position
- Zoom-Level: Automatisch angepasst, dass alle aktiven Fluege sichtbar sind
- Manueller Zoom und Pan moeglich

---

## 6. Docker-Deployment

### Container-Struktur (5 Container)

```
docker-compose.yml
  |
  +-- nginx          (Reverse Proxy + Frontend Static Files, Multi-Stage Build)
  +-- api            (FastAPI: REST API + WebSocket, --workers 4, skalierbar)
  +-- worker         (APRS Worker: OGN-Verbindung + Flight Logic, Singleton)
  +-- postgres        (PostgreSQL 16 + PostGIS)
  +-- redis           (Redis 7, Hot State + Cache + PubSub)
```

**Worker + API nutzen dasselbe Docker-Image** - nur der Startbefehl unterscheidet sich.
Frontend wird als Multi-Stage Build in das Nginx-Image eingebettet.

### docker-compose.yml (Struktur)

```yaml
services:
  postgres:
    image: postgis/postgis:16-3.4
    environment:
      POSTGRES_DB: flight_monitor
      POSTGRES_USER: ${DB_USER}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./db/init.sql:/docker-entrypoint-initdb.d/01-init.sql
      - ./db/seed-airports.sql:/docker-entrypoint-initdb.d/02-seed-airports.sql
    ports:
      - "${DB_PORT:-5432}:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER}"]
      interval: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    volumes:
      - redisdata:/data
    command: redis-server --appendonly yes --maxmemory 512mb --maxmemory-policy volatile-lru
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 5

  worker:
    build: ./app
    command: python -m app.worker
    environment: &app-env                 # YAML Anchor: gemeinsame Env-Vars
      DATABASE_URL: postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@postgres:5432/flight_monitor
      REDIS_URL: redis://redis:6379
      OGN_CALLSIGN: ${OGN_CALLSIGN:-FLIGHTMON}
      LOG_LEVEL: ${LOG_LEVEL:-info}
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    restart: unless-stopped
    deploy:
      replicas: 1                         # IMMER 1 - Singleton (nur eine OGN-Verbindung)

  api:
    build: ./app
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${API_WORKERS:-4}
    environment:
      <<: *app-env                        # YAML Merge: erbt Worker-Env
      JWT_SECRET: ${JWT_SECRET}
      SMTP_HOST: ${SMTP_HOST:-}
      SMTP_PORT: ${SMTP_PORT:-587}
      SMTP_USER: ${SMTP_USER:-}
      SMTP_PASS: ${SMTP_PASS:-}
      BASE_URL: ${BASE_URL:-http://localhost}
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      worker:
        condition: service_started
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 10s
      retries: 3

  nginx:
    build:
      context: .
      dockerfile: nginx/Dockerfile     # Multi-Stage: baut Frontend + kopiert in Nginx
    ports:
      - "${HTTP_PORT:-80}:80"
      - "${HTTPS_PORT:-443}:443"
    volumes:
      - ./nginx/ssl:/etc/nginx/ssl:ro
    depends_on:
      api:
        condition: service_healthy
    restart: unless-stopped

volumes:
  pgdata:
  redisdata:
```

### Nginx Multi-Stage Dockerfile

```dockerfile
# nginx/Dockerfile

# Stage 1: Frontend bauen
FROM node:20-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Nginx mit statischen Files
FROM nginx:alpine
COPY --from=frontend-build /build/dist /usr/share/nginx/html
COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY nginx/mime.types /etc/nginx/mime.types
```

### App Dockerfile (gemeinsam fuer Worker + API)

```dockerfile
# app/Dockerfile
FROM python:3.12-slim

WORKDIR /app

# System-Dependencies (fuer asyncpg, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: API-Server mit 4 Workern (ueberschrieben in docker-compose.yml)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

**Dasselbe Image, zwei Rollen:**
- `docker-compose.yml` startet den Worker mit: `command: python -m app.worker`
- `docker-compose.yml` startet die API mit: `command: uvicorn ... --workers 4`

**Skalierung:** API-Worker-Anzahl ueber `API_WORKERS` Env-Variable steuerbar.
Default: 4. Pro Worker ~50MB RAM. Bei 50 Mandanten / 1000 WebSocket-Clients: 4-8 Worker.

### Verzeichnisstruktur (Ziel)

```
flight_monitor/
  docker-compose.yml
  .env.example
  .env                              # Lokale Konfiguration (nicht committet)

  app/                               # Python Hauptanwendung (Worker + API im selben Image)
    Dockerfile
    requirements.txt                 # fastapi, uvicorn, asyncpg, aioredis, pydantic, etc.
    app/
      __init__.py
      main.py                        # FastAPI App (API-Server Entry Point)
      worker.py                      # APRS Worker Entry Point (asyncio Main-Loop)
      config.py                      # Pydantic Settings (Umgebungsvariablen)
      dependencies.py                # FastAPI Dependencies (Auth, Tenant-Context, DB)
      redis_client.py                # aioredis Connection

      aprs/                          # APRS-IS / OGN Anbindung
        __init__.py
        client.py                    # TCP Stream, Auto-Reconnect, Keepalive, Health
        beacon_parser.py             # APRS-String -> Beacon Dataclass
        filter_builder.py            # Dynamische APRS-Filter (Konsolidierung)

      tracking/                      # Flight Logic (Kernlogik)
        __init__.py
        flight_tracker.py            # Verwaltet aktive Fluege pro Flugplatz
        flight_state_machine.py      # Flugzustandslogik
        flight_profile_analyzer.py   # Flugprofil-Analyse bei Signalverlust
        flight_profile_buffer.py     # Ringpuffer letzte 10 Min. Beacons
        launch_detector.py           # Startart-Erkennung (Winde/F-Schlepp/Eigen)
        geo_calc.py                  # QDR, Distanz, Azimut, Bodenniveau
        state_synchronizer.py        # Redis -> PostgreSQL Sync (periodisch)

      data/                          # Daten-Layer
        __init__.py
        aircraft_resolver.py         # FLARM-ID -> Kennzeichen (In-Memory Cache)
        ddb_updater.py               # OGN DDB + FlarmNet Sync (taeglich)
        airfield_manager.py          # Aktive Flugplaetze laden, Cache

      api/                           # REST + WebSocket Endpoints
        __init__.py
        auth.py                      # Register, Login, JWT, E-Mail-Verifikation
        tenants.py                   # Mandanten-CRUD
        airfields.py                 # Flugplatz-CRUD
        aircraft.py                  # Vereinsflugzeuge CRUD + CSV-Import
        monitor.py                   # REST Fallback fuer Monitor-Daten
        flights.py                   # Fluglog, Statistik, CSV-Export
        admin.py                     # DDB-Sync, OGN-Status, Health
        websocket.py                 # WebSocket Handler (/ws/monitor/{slug})

      db/                            # Datenbank
        __init__.py
        connection.py                # asyncpg Connection Pool
        queries.py                   # SQL Queries (parametrisiert)
        migrations/                  # Alembic Migrationen
          env.py
          versions/

  frontend/                          # React SPA (wird in Nginx eingebettet)
    package.json
    vite.config.ts
    tailwind.config.ts
    tsconfig.json
    src/
      main.tsx
      App.tsx
      pages/
        LandingPage.tsx
        LoginPage.tsx
        RegisterPage.tsx
        DashboardPage.tsx
        AirfieldConfigPage.tsx
        AircraftManagePage.tsx
        FlightLogPage.tsx
        MonitorPage.tsx              # Oeffentlicher Echtzeit-Monitor
        MapViewPage.tsx              # Kartenansicht
      components/
        FlightTable.tsx              # Flugzeug-Tabelle (fly/ldg/alarm)
        FlightCard.tsx               # Einzelnes Flugzeug
        StatusBadge.tsx              # OK / Timeout / ALARM
        ConnectionStatus.tsx         # WebSocket-Verbindungsanzeige
        AirfieldForm.tsx             # Flugplatz-Konfigurationsformular
        AircraftForm.tsx             # Flugzeug hinzufuegen
        MapView.tsx                  # Leaflet/MapLibre Karte
        AlarmBanner.tsx              # Alarm-Benachrichtigung
        FlightProfileChart.tsx       # Flugprofil-Grafik bei Alarm
      hooks/
        useWebSocket.ts              # WebSocket Hook mit Auto-Reconnect + Delta-Merge
        useAuth.ts                   # JWT Token Management
        useMonitorData.ts            # Flugdaten State (Delta-basiert)
      store/
        authStore.ts                 # Zustand Store fuer Auth
        monitorStore.ts              # Zustand Store fuer Monitor-Daten
      api/
        client.ts                    # Fetch-Wrapper mit JWT
        endpoints.ts                 # API-Endpoint Definitionen
      types/
        flight.ts                    # TypeScript Interfaces
        airfield.ts
        tenant.ts

  db/
    init.sql                         # Schema-Erstellung (alle Tabellen)
    seed-airports.sql                # Flugplatz-Stammdaten

  nginx/
    Dockerfile                       # Multi-Stage: Frontend Build + Nginx
    nginx.conf
    ssl/                             # SSL-Zertifikate (Let's Encrypt)

  README.md
  feature.md
```

### Umgebungsvariablen (.env.example)

```env
# Datenbank
DB_USER=flight_monitor
DB_PASSWORD=<sicheres-passwort>
DB_PORT=5432

# JWT
JWT_SECRET=<zufaelliger-string-min-64-zeichen>

# OGN
OGN_CALLSIGN=FLIGHTMON

# SMTP (fuer E-Mail-Verifikation, optional)
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=noreply@example.com
SMTP_PASS=<smtp-passwort>

# Anwendung
BASE_URL=https://flightmonitor.example.com
LOG_LEVEL=info

# Skalierung
API_WORKERS=4

# Ports (optional, Defaults: 80/443)
HTTP_PORT=80
HTTPS_PORT=443
```

---

## 7. Sicherheitsverbesserungen

| Problem (alt) | Loesung (neu) |
|---|---|
| SQL Injection (String-Konkatenation) | Parameterized Queries / Prepared Statements ueberall |
| Hardcoded DB-Credentials im Code | Umgebungsvariablen via `.env` |
| Keine Authentifizierung | JWT-basierte Auth mit bcrypt Password-Hashing |
| SSL-Verifikation deaktiviert | APRS-IS nutzt kein SSL (TCP), aber Backend-API erzwingt HTTPS via Nginx |
| Kein Rate Limiting | Rate Limiter auf Auth-Endpoints (5 Versuche/Min) und API (100 Req/Min) |
| Kein Input Validation | Pydantic-Schema-Validation auf allen API-Endpoints |
| Kein CORS | FastAPI CORS Middleware, konfiguriert auf erlaubte Origins |
| Keine CSRF-Protection | SameSite Cookies + CSRF Token |
| Kein WebSocket Rate Limiting | Max. Connections pro IP, Heartbeat-Timeout |

---

## 7b. OGN-Verbindungs-Resilience

### Problem: Silent Death

APRS-IS TCP-Verbindungen koennen "leise sterben" - kein Fehler, aber keine Daten mehr.
Das ist kritisch: Der Flugleiter merkt nicht, dass keine Updates kommen.

### Loesung: Multi-Layer Health Monitoring

```python
class ResilientAPRSClient:
    """APRS-IS Client mit robuster Verbindungsueberwachung."""

    KEEPALIVE_INTERVAL = 60       # Sek. - APRS-IS Keepalive senden
    BEACON_TIMEOUT = 120          # Sek. - Keine Daten = Verbindung tot
    RECONNECT_BASE_DELAY = 1      # Sek. - Start Exponential Backoff
    RECONNECT_MAX_DELAY = 300     # Sek. - Max. 5 Min. zwischen Reconnects

    async def run(self):
        """Hauptloop mit Auto-Reconnect und Exponential Backoff."""
        delay = self.RECONNECT_BASE_DELAY
        while True:
            try:
                await self._connect_and_stream()
                delay = self.RECONNECT_BASE_DELAY  # Reset bei Erfolg
            except (ConnectionError, asyncio.TimeoutError) as e:
                log.warning(f"APRS-IS Verbindung verloren: {e}. Reconnect in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.RECONNECT_MAX_DELAY)

    async def _connect_and_stream(self):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("aprs.glidernet.org", 14580),
            timeout=10
        )
        await self._login(writer)
        self.last_data_time = time.monotonic()
        self.connected = True
        self._notify_health("connected")

        while True:
            try:
                line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=self.BEACON_TIMEOUT
                )
            except asyncio.TimeoutError:
                # Keine Daten seit BEACON_TIMEOUT -> Verbindung tot
                self._notify_health("timeout")
                raise ConnectionError("APRS-IS Beacon-Timeout")

            self.last_data_time = time.monotonic()
            if line.startswith(b"#"):
                continue  # Keepalive/Kommentar
            await self._process_beacon(line.decode("utf-8", errors="replace"))

    def health_status(self) -> dict:
        """Fuer /api/health/ogn Endpoint."""
        age = time.monotonic() - self.last_data_time if self.connected else -1
        return {
            "connected": self.connected,
            "last_beacon_age_s": round(age, 1),
            "beacons_per_minute": self.beacon_counter.rate(),
            "reconnect_count": self.reconnect_count,
            "uptime_s": int(time.monotonic() - self.start_time)
        }
```

### Health-Endpoint und Frontend-Anzeige

```
GET /api/health/ogn

{
  "connected": true,
  "last_beacon_age_s": 1.3,
  "beacons_per_minute": 340,
  "reconnect_count": 0,
  "uptime_s": 86400
}
```

Im Frontend-Footer: `OGN: verbunden (340 Beacons/Min)` oder `OGN: GETRENNT! Reconnect...`

**Health-Daten-Flow:** Worker schreibt Health-Status in Redis (`HSET ogn:health ...`).
API-Server liest bei `/api/health/ogn` aus Redis - kein direkter Zugriff auf Worker noetig.

---

## 8. Datenbereinigung und Wartung

### Automatische Jobs (aufgeteilt auf Worker + API-Server)

| Job | Intervall | Aktion |
|---|---|---|
| Redis Hot State Sync | Alle 30 Sekunden | Bulk-UPDATE `flight_status` aus Redis (state_synchronizer.py) |
| Redis Stream Trim | Alle 6 Stunden | XTRIM positions:* MAXLEN ~5000 |
| Flight Status Reset | Taeglich 03:00 UTC | Aktive Fluege archivieren -> `flight_log`, Redis Keys bereinigen |
| OGN DDB Sync | Taeglich 04:00 UTC | `ddb.glidernet.org/download/?j=1` -> `aircraft_registry`, Cache reload |
| FlarmNet Sync | Woechentlich So 04:30 UTC | `data.fln` herunterladen, importieren, Cache reload |
| Stale Alarm Check | Alle 30 Sekunden | Fluege ohne Update > alarm_timeout_s -> ALARM + Profilanalyse |
| Aircraft Cache Reload | Alle 60 Minuten | In-Memory Aircraft-Cache aus DB neu laden |

---

## 9. Implementierungsreihenfolge

### Phase 1: Fundament (Docker + DB + API-Server)

1. Docker Compose Setup (PostgreSQL + Redis + Nginx + Worker + API, YAML Anchors)
2. Gemeinsames Dockerfile (app/) mit zwei Startmodi (worker.py / main.py)
3. Datenbank-Schema erstellen (init.sql mit allen Tabellen inkl. PostGIS + Disclaimer-Felder)
4. FastAPI-Grundgeruest (Uvicorn multi-worker, asyncpg Pool, Redis-Verbindung)
5. Alembic Migrations einrichten
6. Auth-System (Register, Login, JWT, bcrypt, Disclaimer-Akzeptanz)
7. Tenant + Airfield CRUD API (Pydantic Validation)
8. Aircraft CRUD API (Mandanten-eigene Flugzeuge, CSV-Import)

### Phase 2: APRS-Worker + Flight Logic (Echtzeit-Daten)

9. APRS-IS Client im Worker-Prozess (aprs/client.py, Auto-Reconnect, Keepalive)
10. Beacon-Parser (APRS-String -> Dataclass)
11. Flight Tracker: 2-stufige Beacon-Pipeline (Empfang -> Selektives Tracking)
12. Redis Hot State: Worker schreibt HSET + PUBLISH pro Beacon
13. Dynamische Filter-Generierung (filter_builder.py, Konsolidierung)
14. Echtzeit QDR/Distanz/Azimut-Berechnung im Worker (geo_calc.py)
15. Aircraft Resolver mit In-Memory Cache + periodischem Reload
16. OGN DDB + FlarmNet Sync-Job (laeuft im Worker als Background Task)
17. Verbesserte Flight State Machine (inkl. Aussenlandung, Hysterese)
18. Flugprofil-Ringpuffer (letzte 10 Min. Beacons pro Flugzeug)
19. Flugprofil-Analyse bei Signalverlust (4 Szenarien)
20. Eskalationslogik (automatische Hochstufung nach Zeitablauf)
21. Startart-Erkennung (launch_detector.py: Winde/F-Schlepp/Eigen + Track-Matching)
22. F-Schlepp Paar-Tracking + Ausklink-Erkennung (mit Kurs-Validierung)
23. State Synchronizer: Redis -> PostgreSQL (laeuft im API-Server, periodisch + bei Events)
24. OGN Health Monitoring (Beacon-Timeout, Reconnect-Counter, /api/health/ogn)

### Phase 3: WebSocket + REST Monitor (im API-Server)

25. Redis SUBSCRIBE im API-Server (jeder Worker subscribed beacon:* event:*)
26. WebSocket-Server (/ws/monitor/{slug}) in FastAPI
27. Delta-basiertes Protokoll (full_state bei Connect, flight_update Deltas)
28. Broadcast-Logik (nur an Clients des jeweiligen Flugplatzes)
29. REST-Fallback-Endpoint (/api/monitor/{slug}, liest aus Redis)
30. Heartbeat (Ping/Pong alle 15s, Timeout nach 30s)
31. Alarm-, Emergency- und Launch-Events via WebSocket
32. State-Recovery aus Redis bei Worker- oder API-Neustart

### Phase 4: Frontend (Tower-Monitor)

33. React + Vite + TailwindCSS Projekt aufsetzen
34. Auth-Pages (Login, Register, E-Mail-Verifikation, Disclaimer-Akzeptanz)
35. Dashboard-Layout mit Navigation
36. Airfield-Konfigurationsseite (Formular + Karte fuer Koordinaten)
37. Aircraft-Verwaltungsseite (Tabelle + Formular + CSV-Import)
38. Tower-Monitor: Tabellen-Ansicht mit prominentem QDR/Distanz/Hoehe (Dark Mode)
39. Tower-Monitor: Steig/Sink-Indikatoren, Farbschema, Ablesbarkeit aus Entfernung
40. useWebSocket Hook: Auto-Reconnect + Delta-Merge in Zustand Store
41. Startart-Anzeige (Spalte 2: Winde/F-Schlepp/Eigen, IM SCHLEPP Sektion)
42. StatusBadge, ConnectionStatus, OGN-Health Komponenten
43. Alarm-System: Blinkende Zeilen, Audio-Alarm, Quittierungs-Buttons
44. Emergency-Popup: Flugprofil-Grafik, Abnormale Indikatoren, Rettungs-Button
45. Eskalations-UI: Hochstufen, Entwarnung, Quittierung
46. Disclaimer-Footer ("Assistenzsystem - ersetzt nicht die Flugleiter-Pflichten")
47. Responsive Design (Desktop-Monitor primaer, Tablet sekundaer)

### Phase 5: Kartenansicht + Erweiterte Features

48. Kartenansicht mit Flugzeugpositionen und QDR-Linien (MapLibre GL)
49. Topographische Karte (Gelaende fuer Segelflieger)
50. Flugspur-Anzeige (letzte 60 Min. aus Redis Streams, hoehengefaerbt)
51. Alarm-Position: Pulsierender Kreis + Koordinaten auf Karte
52. Split-View: Tabelle links, Karte rechts (grosse Monitore)
53. Fluglog-Seite mit Pagination und Filtern
54. Startschreiber CSV-Export (inkl. F-Schlepp-Abrechnungsdaten)
55. Tages-/Wochen-Statistiken
56. Flugplatz-Stammdaten importieren (Airports aus ogn.sql)
57. DEM/Bodenniveau-Schaetzung (optional, Prioritaet 2 - NASA SRTM via rasterio)

### Phase 6: Produktion + Haertung

58. Nginx Multi-Stage Build (Frontend eingebettet)
59. Nginx SSL-Konfiguration (Let's Encrypt / Certbot)
60. Healthchecks fuer alle Services (Docker + /api/health/*)
61. Strukturiertes Logging (JSON, Log-Level, Rotation)
62. Backup-Strategie (PostgreSQL pg_dump taeglich, Redis RDB)
63. Rate Limiting (Auth: 5/Min, API: 100/Min, WebSocket: max 50 Connections/IP)
64. Graceful Shutdown (APRS-Disconnect, Redis Flush, PG Connection Close)
65. Dokumentation (README, API-Docs via FastAPI /docs, Deployment-Guide)
66. Lasttests (simulierte Beacons, Worker-Failover, WebSocket-Verbindungen)

---

## 10. Migration bestehender Daten

### Einmalige Migration

1. `aircraft`-Tabelle (ogn.sql) -> `aircraft_registry` importieren
2. `airport`-Tabelle (ogn.sql) -> Seed-Datei fuer Flugplatz-Stammdaten
3. Ohlstadt als ersten Mandanten anlegen mit bestehender Konfiguration
4. `glidernet_aircraft` -> `tenant_aircraft` fuer Ohlstadt importieren

### Kompatibilitaet

Die alte Anwendung (PHP) kann waehrend der Migration parallel laufen. Die neue App schreibt in die neue Datenbank. Ein Umschalten erfolgt durch DNS-Aenderung oder Nginx-Proxy-Umstellung.

---

## 11. Skalierbarkeit

| Aspekt | Loesung |
|---|---|
| Viele Mandanten (50+) | Konsolidierter APRS-Filter (filter_builder.py), internes Routing nach Flugplatz |
| Viele WebSocket-Clients | Uvicorn/asyncio skaliert gut fuer I/O (10k+ Connections pro Instanz) |
| Horizontale Skalierung | Mehrere App-Instanzen moeglich, aber APRS-Client nur in einer Instanz (Leader-Election via Redis) |
| Position-Daten | Redis Streams statt PostgreSQL, MAXLEN ~5000 pro Flugplatz, automatisches Trimming |
| APRS-Datenmenge | Port 14580 mit konsolidiertem geo-Filter statt Port 10152 (Full Feed) |
| Mehrere Regionen | Mehrere App-Instanzen (z.B. Alpen, Norddeutschland) mit jeweils eigenem APRS-Filter |
| Redis Memory | Max 512 MB mit volatile-lru (nur Keys mit TTL werden evicted, Hot State ist sicher) |

---

## 11b. Rechtliches: Haftungsausschluss (Disclaimer)

### Problem: Alarm-System und Haftungsrisiko

Begriffe wie "Notfall-Verdacht", Warnstufe "CRITICAL" und Features wie
"Rettungsleitstelle anrufen" koennten den Eindruck erwecken, das System
ersetze die Aufsichtspflicht des Flugleiters. Bei False Negatives (Absturz
nicht erkannt) oder False Positives (Funkloch als Notfall gemeldet)
entsteht ein erhebliches Haftungsrisiko.

### Pflicht-Disclaimer bei Mandanten-Registrierung

Bei der Registrierung muss der Mandant **vor Nutzung des Alarm-Systems**
aktiv einen Disclaimer akzeptieren. Ohne Zustimmung: kein Zugang zum Monitor.

```
=== NUTZUNGSBEDINGUNGEN FLIGHTMONITOR ALARM-SYSTEM ===

WICHTIGER HINWEIS:

FlightMonitor ist ein reines ASSISTENZSYSTEM zur Unterstuetzung
des Flugbetriebs. Es ersetzt NICHT:

- Die Pflichten des Flugleiters nach NFL II-81/13
- Die eigenverantwortliche Luftraumbeobachtung
- Die vorgeschriebenen Kommunikationsverfahren
- Die Verantwortung des Luftfahrzeugfuehrers

EINSCHRAENKUNGEN DES ALARM-SYSTEMS:

- Die Notfallerkennung basiert auf Flugprofil-Analyse und ist
  NICHT fehlerfrei. Funkloecher, OGN-Empfangsluecken und
  fehlerhafte FLARM-Daten koennen zu falschen Alarmen oder
  ausbleibenden Warnungen fuehren.

- Ein AUSBLEIBENDER Alarm bedeutet NICHT, dass kein Notfall vorliegt.

- Die Startart-Erkennung und F-Schlepp-Hoehen-Abrechnung sind
  Schaetzwerte basierend auf GPS-Daten und koennen von der
  tatsaechlichen Hoehe abweichen.

Der Betreiber uebernimmt keinerlei Haftung fuer:
- Nicht erkannte Notfaelle (False Negatives)
- Fehlalarme (False Positives)
- Unvollstaendige oder fehlerhafte Flugdaten
- Ausfaelle des Systems oder der OGN-Infrastruktur

[ ] Ich habe die Nutzungsbedingungen gelesen und akzeptiert.
    Ich bin mir bewusst, dass FlightMonitor ein Assistenzsystem
    ist und keine Aufsichtspflichten ersetzt.
```

### Technische Umsetzung

- Checkbox ist **Pflichtfeld** bei Registrierung
- Akzeptanz-Zeitstempel wird in `tenants.disclaimer_accepted_at` gespeichert
- Disclaimer-Version wird versioniert (`disclaimer_version` Feld)
- Bei Aenderung des Disclaimer-Textes: erneute Zustimmung erforderlich
- Im Monitor-Footer: dezenter Hinweis "Assistenzsystem - ersetzt nicht die Flugleiter-Pflichten"

---

## 12. Zusammenfassung der Verbesserungen

| Bereich | Alt | Neu |
|---|---|---|
| **Tracking-Reichweite** | **Nur 0.8 km Platzradius** | **Grossflaechig 500km - Flugzeug wird ueberall verfolgt** |
| **QDR/Peilung** | Nebensache, kleine Schrift | **Primaere Info, gross und prominent fuer Flugleiter** |
| **Hoehe** | Nebensache | **Primaere Info, gross, gelb, sofort sichtbar** |
| OGN-Anbindung | HTTP Polling (lxml.php, 3s) | APRS-IS TCP Stream (< 1s Latenz) |
| Frontend-Update | XMLHttpRequest Polling 3s | WebSocket Push (Delta-Protokoll, < 5ms) |
| Architektur | Monolithisches PHP | **Python/FastAPI (Worker + API, gleiches Image)** + PostgreSQL + Redis |
| Datenhaltung | Alles in MySQL (langsam) | **3-Schichten: In-Memory + Redis Hot State + PostgreSQL Cold Storage** |
| DB-Writes | Jedes Beacon -> DB UPDATE | **Nur bei Statuswechsel + periodischer Bulk-Sync (99.5% Reduktion)** |
| Datenbank | MySQL, kein Schema-Management | PostgreSQL + PostGIS, Alembic Migrationen |
| Mandanten | Nur Ohlstadt (hardcoded) | Multi-Tenant mit Registrierung |
| Konfiguration | Hardcoded in PHP-Dateien | UI-basiert pro Flugplatz, .env fuer Infrastruktur |
| FLARM-Aufloesung | Nur OGN-Stream reg-Feld | 3-stufig: Lokal + OGN DDB + FlarmNet, **In-Memory Cache (< 1us Lookup)** |
| Aussenlandung | Nicht implementiert | Automatische Erkennung + naechster Flugplatz + Koordinaten |
| **Notfallerkennung** | **Nicht vorhanden** | **Flugprofil-Analyse: 4 Szenarien (Fremdplatz/Aussenlandung/Notfall/Funkloch)** |
| **Absturzerkennung** | **Nicht vorhanden** | **Abnormales Flugprofil erkennen (Sinkrate, Kursinstabilitaet, Hoehenverlust)** |
| **Startart-Erkennung** | **Nicht vorhanden** | **Winde/F-Schlepp/Eigenstart, Paar-Tracking, Ausklink-Hoehe** |
| Alarm | Inkonsistente Logik | 4-stufig (LOW/MEDIUM/HIGH/CRITICAL), auto-Eskalation, Quittierung |
| Sicherheit | SQL Injection, Klartext-Passwoerter | Prepared Statements, JWT, bcrypt, Rate Limiting, Pydantic Validation |
| Deployment | Manuell auf Webserver | **Docker Compose (5 Container), ein Befehl** |
| Frontend | jQuery + Bootstrap 4, HTML-Tables | React 19 + TailwindCSS, Karte + Tabelle, Dark Mode |
| OGN-Verbindung | Kein Error-Handling | **Auto-Reconnect, Exponential Backoff, Health-Monitoring** |
| APRS-Filter | Fester Filter | **Dynamische Konsolidierung (Multi-Tenant, max 9 Filter)** |
| Startschreiber | Nur Anzeige | Export als CSV inkl. F-Schlepp-Abrechnungsdaten |
| Kartenansicht | Nicht vorhanden | Topographische Karte mit QDR-Linien, Flugspuren, Alarmpositionen |
| Container | - | **5 (Nginx+Frontend, API multi-worker, APRS-Worker, PostgreSQL, Redis)** |
| Skalierung | Nicht moeglich | **API horizontal skalierbar (N Worker), Worker als Singleton** |
| Haftung | Kein Disclaimer | **Pflicht-Disclaimer bei Registrierung (Assistenzsystem)** |
| Latenz Beacon->Browser | ~3000ms (HTTP Poll) | **~5ms (Worker -> Redis -> API -> WebSocket)** |
