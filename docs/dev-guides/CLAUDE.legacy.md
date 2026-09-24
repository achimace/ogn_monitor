# OGN FlightMonitor - Agentic Development Guide

## Projektbeschreibung

Echtzeit-Flugmonitoring-System fuer Segelflugplaetze. Nutzt OGN/FLARM-Daten
um alle am Heimatplatz gestarteten Flugzeuge grossflaechig (500km) zu verfolgen.
Zeigt dem Flugleiter im Tower in Echtzeit QDR (Peilung), Distanz und Hoehe.
Erkennt Startarten (Winde/F-Schlepp/Eigenstart), analysiert Flugprofile bei
Signalverlust und warnt bei Notfaellen. Multi-Tenant SaaS fuer beliebige Flugplaetze.

## Architektur (WICHTIG - immer beachten!)

```
5 Docker-Container, 1 Sprache (Python), 1 Docker-Image fuer Worker+API:

  Nginx (+ React Frontend static)
  API Server (FastAPI, --workers 4, skalierbar)
  APRS Worker (Python asyncio, Singleton - NUR 1 Instanz!)
  PostgreSQL 16 + PostGIS
  Redis 7 (Hot State + PubSub + Cache)
```

### Zwei-Prozess-Modell (KRITISCH)

- **APRS-Worker** (`python -m app.worker`): Singleton. Verbindet sich mit OGN,
  fuehrt Flight Logic aus, schreibt Ergebnisse in Redis (HSET + PUBLISH).
- **API-Server** (`uvicorn app.main:app --workers 4`): Multi-Worker. Liest aus
  Redis, bedient REST + WebSocket, synct nach PostgreSQL.
- **Beide nutzen dasselbe Docker-Image und dieselbe Codebase.**

### 3-Schichten-Datenhaltung

1. **In-Memory** (Worker): Flight-State Dicts, Ringpuffer, Aircraft-Cache
2. **Redis** (Hot State): Live-Flugstatus, PubSub, Position-Streams
3. **PostgreSQL** (Cold Storage): Mandanten, Config, Fluglog, Aircraft-Registry

### Datenfluss

```
OGN APRS (TCP) -> Worker (Parse + Flight Logic + QDR) -> Redis (HSET + PUBLISH)
                                                           |
API Server <- Redis SUBSCRIBE <- - - - - - - - - - - - - -+
    |
WebSocket -> Browser (Delta-Updates)
```

## Masterplan

Der komplette Verbesserungsplan mit allen Details steht in **`feature.md`**.
Dort findest du:
- Datenbank-Schema (alle Tabellen)
- API-Endpoints (REST + WebSocket)
- WebSocket Delta-Protokoll
- Flight State Machine (Zustaende, Uebergaenge)
- Flugprofil-Analyse (4 Szenarien)
- Startart-Erkennung (Winde/F-Schlepp/Eigen)
- F-Schlepp Paar-Tracking mit Track-Matching
- APRS-Filter-Konsolidierung
- OGN-Resilience (Auto-Reconnect)
- Haftungsausschluss
- Implementierungsreihenfolge (66 Schritte, 6 Phasen)

**Lies feature.md IMMER bevor du ein neues Feature implementierst!**

## Tech Stack

### Backend (app/)
- Python 3.12
- FastAPI + Uvicorn (API-Server)
- asyncpg (PostgreSQL async)
- aioredis / redis-py (Redis async)
- Pydantic v2 (Validation + Settings)
- passlib + python-jose (Auth: bcrypt + JWT)
- Alembic (DB Migrations)

### Frontend (frontend/)
- React 19
- Vite
- TailwindCSS
- TypeScript
- Zustand (State Management)
- MapLibre GL (Kartenansicht)

### Infrastruktur
- Docker Compose
- PostgreSQL 16 + PostGIS
- Redis 7 (volatile-lru, 512MB)
- Nginx (Reverse Proxy + Static Files)

## Coding Standards

### Python
- Type Hints ueberall (PEP 484)
- Pydantic Models fuer alle API Request/Response
- Async/await konsequent (kein sync I/O im Event Loop!)
- SQL: Parametrisierte Queries, NIEMALS String-Konkatenation
- Logging: structlog mit JSON-Output
- Docstrings: Google-Style
- Tests: pytest + pytest-asyncio

### TypeScript/React
- Functional Components mit Hooks
- TypeScript strict mode
- Zustand fuer globalen State
- Custom Hooks fuer Logik (useWebSocket, useMonitorData, useAuth)
- TailwindCSS fuer Styling (kein CSS-in-JS)

### Allgemein
- Keine hardcoded Werte - alles ueber config.py / .env
- Keine Secrets im Code
- Jede Datei hat einen klaren Zweck (Single Responsibility)
- Feature.md ist die Wahrheit - bei Widerspruechen gilt feature.md

## Verzeichnisstruktur

```
ogn_monitor/
  docker-compose.yml
  .env.example
  feature.md                    # Masterplan (IMMER zuerst lesen!)

  app/                          # Python (Worker + API, gleiches Image)
    Dockerfile
    requirements.txt
    app/
      __init__.py
      main.py                   # FastAPI App (API Entry)
      worker.py                 # APRS Worker Entry
      config.py                 # Pydantic Settings
      redis_client.py
      dependencies.py
      aprs/                     # OGN-Anbindung (Worker)
      tracking/                 # Flight Logic (Worker)
      data/                     # Daten-Layer (Worker + API)
      api/                      # REST + WebSocket (API)
      db/                       # DB Connection + Migrations

  frontend/                     # React SPA
    src/
      pages/
      components/
      hooks/
      store/
      api/
      types/

  db/                           # DB Init Scripts
    init.sql
    seed-airports.sql

  nginx/                        # Reverse Proxy
    Dockerfile
    nginx.conf
```

## Wichtige Domain-Konzepte

| Begriff | Bedeutung |
|---|---|
| FLARM | Kollisionswarngeraet in Flugzeugen, sendet Position |
| OGN | Open Glider Network - empfaengt FLARM-Daten |
| APRS-IS | Protokoll fuer OGN-Datenstream (TCP Port 14580) |
| QDR | Peilung (Bearing) vom Flugplatz zum Flugzeug in Grad |
| AGL | Above Ground Level - Hoehe ueber Grund |
| MSL | Mean Sea Level - Hoehe ueber Meeresspiegel |
| F-Schlepp | Flugzeugschlepp (Aerotow) - Segler wird von Motorflugzeug gezogen |
| Windenstart | Winch Launch - Segler wird per Seilwinde gestartet |
| Eigenstart | Self-Launch - Motorsegler startet mit eigenem Motor |
| Aussenlandung | Outlanding - Landung ausserhalb eines Flugplatzes |
| Flugleiter | Tower Controller - verantwortlich fuer den Flugbetrieb |
| DDB | OGN Device Database - FLARM-ID zu Kennzeichen Zuordnung |
| Beacon | Einzelne Positionsmeldung eines FLARM-Geraets |
| Hot State | Live-Flugdaten in Redis (nicht in PostgreSQL!) |

## Haeufige Fehlerquellen (ACHTUNG)

1. **APRS-Worker ist Singleton** - NIEMALS mehr als 1 Instanz starten!
2. **Redis Hot State** - Live-Daten gehoeren in Redis, NICHT in PostgreSQL
3. **PostgreSQL nur fuer Cold Storage** - Writes nur bei Statuswechsel + periodischer Sync
4. **APRS-Verbindung kann leise sterben** - Beacon-Timeout immer pruefen
5. **Geo-Berechnungen sind CPU-bound** - Deshalb Worker und API getrennt
6. **FLARM-IDs sind nicht permanent** - Koennen sich aendern, Aircraft-Cache regelmaessig reloaden
7. **Disclaimer ist Pflicht** - Kein Monitor-Zugang ohne akzeptierten Haftungsausschluss
8. **Track-Matching bei F-Schlepp** - Distanz + Hoehe + Kurs pruefen (parallele Starts!)
