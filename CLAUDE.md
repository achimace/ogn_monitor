# OGN FlightMonitor – Claude Code Guide

Echtzeit-Flugmonitoring für Segelflugplätze (OGN/FLARM → QDR, Distanz, Höhe,
Startart, Alarme). Multi-Tenant SaaS, produktiv unter https://flight-monitor.de
(Hetzner, Docker Compose). Pilot-Mandant: SFG Werdenfels / Ohlstadt-Pömetsried.

## Architektur (immer beachten)

```
Nginx (+ React-SPA static)  ─►  API (FastAPI, --workers 4)  ◄─ Redis SUBSCRIBE
                                     │                           ▲
                               PostgreSQL 16 + PostGIS           │ HSET + PUBLISH
                                     ▲                           │
OGN APRS-IS (TCP 14580) ─► APRS-Worker (python -m app.worker, SINGLETON)
```

- **Worker und API = dasselbe Image** (`app/`), unterschiedlicher Startbefehl.
- **APRS-Worker ist Singleton** – niemals skalieren, niemals zweite Instanz starten.
- **Live-Daten → Redis** (Hot State). **PostgreSQL = Cold Storage** (nur bei
  Statuswechsel / periodischem Sync schreiben).
- Flight-Logic (CPU-bound) gehört in den Worker, nicht in die API.
- Kein sync I/O im Event Loop. SQL nur parametrisiert.

## Wo steht was

| Thema | Quelle |
|---|---|
| Masterplan Monitor (Schema, WS-Protokoll, State Machine, Startart) | `feature.md` – **nur den relevanten Abschnitt lesen** (`grep -n "^## " feature.md`), nicht die ganze Datei |
| Nächstes großes Feature: Vereinsflieger-Sync | `docs/konzept-vf-sync.md` (verbindliche Spezifikation, Arbeitspakete AP-0…AP-13) |
| Detail-Guides je Schicht (APRS, Redis, WS, DB, Frontend …) | `docs/dev-guides/*.md` |
| Server-Setup von Null | `DEPLOYMENT.md` |
| Deploy / DB-Pull | `scripts/deploy.sh`, `scripts/pull-db.sh` (Konfig: `.deploy.env`) |

## Code-Landkarte

- `app/app/aprs/` – APRS-Client, Beacon-Parser, Filter (Worker)
- `app/app/tracking/` – State Machine, LaunchDetector, Profil-Analyse, Redis-Writer, State-Sync (Worker)
- `app/app/api/` – REST + WebSocket (API)
- `app/app/data/` – Aircraft-Resolver, DDB-Updater
- `app/app/config.py` – alle Settings/Schwellwerte (keine hardcoded Werte!)
- `frontend/src/` – React 19, Vite, Tailwind, Zustand, MapLibre
- `db/init.sql` – vollständiges Schema für Neuinstallation
- `db/migrations/NNN_*.sql` – **die maßgeblichen Migrationen** (siehe unten)

## Datenbank-Migrationen (wichtig)

- Maßgeblich sind **idempotente SQL-Dateien in `db/migrations/`**. `deploy.sh`
  führt bei *jedem* Deploy *alle* Dateien erneut aus → jede Migration muss
  beliebig oft fehlerfrei laufen (`IF NOT EXISTS`, `DO $$ … $$`-Guards).
- Jede Schemaänderung zusätzlich in `db/init.sql` nachziehen.
- Alembic unter `app/app/db/migrations/` wird im Deploy **nicht** ausgeführt
  (Altlast). Wo `docs/konzept-vf-sync.md` „Alembic-Migration“ sagt, wird eine
  SQL-Migration in `db/migrations/` angelegt.
- Details: Skill `db-migration`.

## Deployment (muss funktionsfähig bleiben)

- Deploy **nur** über `/deploy` (Skill) bzw. `./scripts/deploy.sh` – nie
  manuell per SSH am Server Code ändern.
- `deploy.sh`: Clean-Tree-Check → `git push` → SSH → `git pull --ff-only` →
  alle `db/migrations/*.sql` → `docker compose up -d --build` → Health-Check.
- Branch `master` = Produktion.
- **Nginx-Falle:** Auf dem Server ist `nginx/nginx.conf` lokal durch
  `nginx.prod.conf` überschrieben. Änderungen an Nginx immer in **beiden**
  Dateien machen und vor dem Deploy ansprechen – sonst scheitert
  `git pull --ff-only` auf dem Server.
- `docker-compose.override.yml` ist nur lokal (gitignored, entfernt Let's-Encrypt-Mount).
- Nicht anfassen ohne Rückfrage: `docker-compose.yml` (Service-Namen, Volumes,
  `replicas: 1`), `scripts/deploy.sh`, `.deploy.env*`, `.env*`, `nginx/*.conf`.

## Arbeitsweise mit Agenten

| Agent / Skill | Wofür |
|---|---|
| `backend` (Agent) | Python: Worker, Tracking, API, DB, VF-Sync |
| `frontend` (Agent) | React-SPA, Tower-UI |
| `reviewer` (Agent, read-only) | Review vor Commit/Deploy gegen die Projekt-Invarianten |
| `/run-and-test` | Lokal starten, testen, debuggen |
| `/db-migration` | Neue idempotente Migration anlegen |
| `/vfsync-ap <AP-n>` | Ein Arbeitspaket aus dem VF-Sync-Konzept umsetzen |
| `/deploy` | Geprüftes Produktiv-Deployment (nur manuell auslösbar) |

Ablauf für Features: Plan → `backend`/`frontend` implementieren (mit Tests) →
`reviewer` → Commit (kleine, thematische Commits, Englisch, Imperativ wie in der
bestehenden History) → bei Bedarf `/deploy`.

## Coding Standards (Kurzform)

- Python 3.12, Type Hints, Pydantic v2 für alle Request/Response-Modelle,
  structlog, Google-Docstrings, Tests mit pytest + pytest-asyncio unter `app/tests/`.
- TypeScript strict, Functional Components + Hooks, Zustand, Tailwind (kein CSS-in-JS).
- Keine Secrets in Code, Tests, Logs, Commits. Konfiguration über `config.py` / `.env`.

## Domain-Glossar

FLARM (Kollisionswarner, sendet Position) · OGN (Empfängernetz) · APRS-IS (Datenstrom) ·
QDR (Peilung Platz→LFZ) · AGL/MSL · F-Schlepp / Winde / Eigenstart · Außenlandung ·
Flugleiter (Tower) · DDB (FLARM-ID → Kennzeichen) · Hot State (Live-Daten in Redis) ·
VF (Vereinsflieger.de)

## Häufige Fehlerquellen

1. Zweite Worker-Instanz (z. B. `docker compose up --scale worker=2`) → doppelte Events.
2. Live-Daten in PostgreSQL statt Redis.
3. APRS-Verbindung stirbt leise → Beacon-Timeout prüfen.
4. FLARM-IDs sind nicht permanent → Aircraft-Cache regelmäßig neu laden.
5. Disclaimer ist Pflicht – kein Monitor-Zugang ohne Akzeptanz.
6. F-Schlepp-Track-Matching: Distanz + Höhe + Kurs (parallele Starts!).
7. Datum/Zeit: DB/Redis in UTC; `date.fromisoformat` an asyncpg übergeben, keine Strings.
8. Nicht-idempotente Migration → Deploy bricht beim nächsten Lauf ab.
