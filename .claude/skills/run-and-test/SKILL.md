---
name: run-and-test
description: OGN Monitor lokal starten, Tests ausführen, debuggen (Docker Compose, pytest, Frontend-Build, Redis/PostgreSQL inspizieren, Prod-DB lokal ziehen). Nutzen bei "starten", "testen", "debug", "logs", "Fehler".
---

# Lokal starten, testen, debuggen

Alle Befehle aus dem Repo-Root. Lokal greift `docker-compose.override.yml`
(ohne Let's-Encrypt-Mount); `.env` muss existieren (Vorlage `.env.example`).

## Starten
```bash
docker compose up -d                          # alles (inkl. Frontend-Build in nginx)
docker compose up -d postgres redis worker api   # nur Backend
docker compose up -d --build worker api       # nach Python-Änderungen
docker compose logs -f worker                 # APRS/Tracking
docker compose logs -f api
```
Frontend mit Hot Reload: `cd frontend && npm install && npm run dev` (Proxy → :8000).
**Nie** `--scale worker=…` – Worker ist Singleton.

## Tests
```bash
docker compose run --rm --no-deps api pytest -q            # Unit-Tests
docker compose run --rm api pytest -q -k <muster>          # mit postgres/redis
cd frontend && npm run build && npm run lint               # TS-Check + Build + Lint
```
Tests liegen unter `app/tests/` (pytest + pytest-asyncio). Für Tracking-Logik
synthetische Beacon-Sequenzen/Replays verwenden, keine Live-OGN-Abhängigkeit.

## Produktionsdaten lokal
`./scripts/pull-db.sh` – ersetzt die **lokale** DB durch einen Prod-Dump
(vorher Nutzer fragen; Dumps liegen in `backups/`, gitignored, personenbezogen!).

## Debugging
```bash
docker compose exec redis redis-cli
  SMEMBERS flights:<slug>            # aktive Flüge
  HGETALL flight:<slug>:<flarm_id>   # ein Flug
  HGETALL ogn:health
  SUBSCRIBE beacon:<slug> event:<slug>
docker compose exec postgres psql -U ogn_monitor -d ogn_monitor
  SELECT flarm_id, registration, status FROM flight_status;
  SELECT * FROM flight_log ORDER BY takeoff_time DESC LIMIT 10;
curl http://localhost:8000/health
curl http://localhost:8000/api/health/ogn
```

| Problem | Prüfen |
|---|---|
| Worker startet nicht | `docker compose logs worker` – DB/Redis erreichbar? |
| API 500 | `docker compose logs api` – Traceback; Datum als `date`, nicht String an asyncpg |
| WebSocket bricht ab | Nginx `proxy_read_timeout` |
| Keine APRS-Daten | `/api/health/ogn`, Filter/Radius, Beacon-Timeout |
