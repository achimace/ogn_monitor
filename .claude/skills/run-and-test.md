---
name: run-and-test
description: Starte, teste und debugge die Anwendung (Docker, Tests, Logs)
triggers:
  - "starten"
  - "start"
  - "test"
  - "debug"
  - "docker"
  - "logs"
  - "fehler"
  - "error"
---

# Anwendung starten, testen und debuggen

## Docker Compose starten

### Alles starten
```bash
cd C:/_projekte/ogn_monitor
docker compose up -d
```

### Nur Backend (ohne Frontend-Build)
```bash
docker compose up -d postgres redis worker api
```

### Logs anzeigen
```bash
docker compose logs -f worker    # APRS Worker Logs
docker compose logs -f api       # API Server Logs
docker compose logs -f postgres  # DB Logs
```

### Neubauen nach Code-Aenderungen
```bash
docker compose build app         # Worker + API neu bauen
docker compose up -d worker api  # Neustart
```

## Lokale Entwicklung (ohne Docker)

### Voraussetzungen
- PostgreSQL 16 + PostGIS lokal oder via Docker
- Redis 7 lokal oder via Docker
- Python 3.12+
- Node.js 20+

### Backend
```bash
cd C:/_projekte/ogn_monitor/app
pip install -r requirements.txt

# API Server (mit Hot Reload)
uvicorn app.main:app --reload --port 8000

# Worker (separates Terminal)
python -m app.worker
```

### Frontend
```bash
cd C:/_projekte/ogn_monitor/frontend
npm install
npm run dev
```

## Tests

### Python Tests
```bash
cd C:/_projekte/ogn_monitor/app
pytest -v

# Einzelnen Test
pytest -v tests/test_beacon_parser.py

# Mit Coverage
pytest --cov=app --cov-report=term-missing
```

### Test-Pattern
```python
# tests/test_geo_calc.py
import pytest
from app.tracking.geo_calc import haversine, azimuth

def test_haversine_ohlstadt_to_brenner():
    # Ohlstadt (47.6586, 11.2348) -> Brenner (~100km)
    dist = haversine(47.6586, 11.2348, 47.0, 11.5)
    assert 70_000 < dist < 80_000  # ~73km

@pytest.mark.asyncio
async def test_flight_tracker_start_detection():
    ...
```

## Debugging

### Redis inspizieren
```bash
# Redis CLI
docker compose exec redis redis-cli

# Alle aktiven Fluege eines Platzes
SMEMBERS flights:ohlstadt

# Einzelnen Flug anzeigen
HGETALL flight:ohlstadt:000239

# OGN Health
HGETALL ogn:health

# PubSub mitlesen
SUBSCRIBE beacon:ohlstadt event:ohlstadt
```

### PostgreSQL inspizieren
```bash
docker compose exec postgres psql -U ogn_monitor -d ogn_monitor

# Aktive Fluege
SELECT flarm_id, registration, status FROM flight_status;

# Fluglog
SELECT * FROM flight_log ORDER BY takeoff_time DESC LIMIT 10;
```

### Health-Endpoints
```bash
curl http://localhost:8000/health
curl http://localhost:8000/api/health/ogn
```

## Haeufige Probleme

| Problem | Loesung |
|---|---|
| Worker startet nicht | `docker compose logs worker` - DB/Redis erreichbar? |
| API 500 Error | `docker compose logs api` - Traceback pruefen |
| WebSocket disconnects | Nginx `proxy_read_timeout` erhoehen (Default: 86400) |
| Redis out of memory | `redis-cli INFO memory` - maxmemory erhoehen |
| DB Connection Refused | `docker compose ps` - PostgreSQL laeuft? |
| APRS keine Daten | OGN Health pruefen: `curl /api/health/ogn` |
