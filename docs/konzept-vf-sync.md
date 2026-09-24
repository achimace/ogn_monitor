# Umsetzungskonzept: VF-Sync — Vereinsflieger-Integration für ogn_monitor

**Automatische Erfassung von Startzeiten, Landezeiten, Startarten, Schlepphöhen und Landungsanzahl in Vereinsflieger.de auf Basis des bestehenden OGN-Monitors**

| | |
|---|---|
| **Version** | 1.0 |
| **Datum** | 24.09.2026 |
| **Basis** | Lastenheft v0.9, Architekturheft v0.9, Review-Findings, Codebasis `achimace/ogn_monitor` |
| **Zweck** | Implementierungs-Blueprint für agentenbasierte Entwicklung mit Claude Code |

> Repo-Hinweis: Wo dieses Dokument „Alembic-Migration“ sagt, wird im Repo eine
> idempotente SQL-Migration unter `db/migrations/NNN_*.sql` (+ `db/init.sql`)
> angelegt – nur diese werden von `scripts/deploy.sh` ausgeführt (siehe CLAUDE.md).

---

## 0. Anleitung für den Coding-Agenten

Dieses Dokument ist die maßgebliche Spezifikation. Regeln für die Umsetzung:

1. **Arbeitspakete (Kap. 10) strikt in Reihenfolge** abarbeiten; jedes Paket endet mit grünen Tests und einem Commit. Kein Paket beginnen, dessen Vorgänger nicht abgeschlossen ist.
2. **Bestehenden ogn_monitor-Code nicht umbauen**, außer an den in Kap. 6 explizit benannten Härtungspunkten. Der Monitor ist produktiv gedacht; VF-Sync dockt an, ersetzt nichts.
3. **Niemals gegen die echte Vereinsflieger-API entwickeln oder testen.** Alle Entwicklung läuft gegen den Mock (AP-2). Echte API nur in den explizit markierten Spike-/Abnahme-Schritten, nur mit Wegwerf-Testflügen, nur nach menschlicher Freigabe.
4. **Schreiboperationen sind heilig:** Jede Funktion, die `flight/edit` oder `flight/add` aufruft, muss die Invarianten aus Kap. 5.4 (Read-before-Write, Nur-leere-Felder, Nie-verringern, Konfidenz-Gate, Budget-Guard, Audit) erfüllen. Tests, die diese Invarianten prüfen, dürfen niemals gelockert werden, um grün zu werden.
5. **Secrets niemals in Code, Tests, Fixtures, Logs oder Commits** (Kap. 8). `.env`-Dateien stehen in `.gitignore`; Beispielwerte nur in `.env.example` mit Platzhaltern.
6. Bei Widersprüchen zwischen diesem Dokument und dem Code-Ist-Stand: dieses Dokument gewinnt; Abweichung als Kommentar `# SPEC-DEVIATION:` markieren und im PR-Text auflisten.
7. Unklarheiten nicht durch Annahmen lösen, sondern als `OPEN-QUESTION` im PR-Text sammeln und den Menschen entscheiden lassen.

---

## 1. Kontext und Zielbild

### 1.1 Ausgangslage

Die Sportfliegergruppe Werdenfels e.V. (Sonderlandeplatz Ohlstadt-Pömetsried) nutzt Vereinsflieger.de (VF) für Flugbuch und Abrechnung. Schleppgebühren werden höhenbasiert abgerechnet (Zeit-Fallback in der Preisformel vorhanden). Unter der Woche fliegt der Verein ohne Betriebsleiter — die VF-Flugdatenerfassung mit OGN-Zuordnung läuft dann nicht, Zeiten und Schlepphöhen fehlen.

Es existiert bereits **ogn_monitor** (`github.com/achimace/ogn_monitor`): ein mandantenfähiger, dockerisierter OGN-Flugleitstand (Python 3.12/asyncio, FastAPI, React, PostgreSQL+PostGIS, Redis). Er verbindet sich mit `aprs.glidernet.org`, parst FLARM-Beacons, führt je Platz eine Flug-Zustandsmaschine (Start-, Lande-, Outlanding-, Signal-Loss-Erkennung) und klassifiziert Startarten inkl. F-Schlepp-Paar-Tracking mit Ausklinkhöhe (`launch_detector.py`). Flugzustände werden in Redis (Hot State, PubSub, Streams) gehalten und nach PostgreSQL (`flight_status`, `flight_log`) synchronisiert.

### 1.2 Zielbild

Jeder Pilot legt seinen Flug in VF lediglich **leer** an (LFZ, Pilot, Startart). VF-Sync erledigt den Rest automatisch, 7 Tage die Woche:

- **Startzeit** wird sofort beim Abheben in den VF-Flug geschrieben (Live-Sichtbarkeit „wer ist in der Luft").
- **Landezeit, Ausklinkhöhe (AGL), Schleppzeit und finale Landungsanzahl** werden nach bestätigter Landung in **einem** gebündelten Edit geschrieben.
- Erfasst werden alle Startarten: F-Schlepp (inkl. Höhe), Winde, Eigenstart, Motorflug — sowie Touch & Gos als Landungszähler.
- Grundprinzip: **Schreibgenauigkeit vor Vollständigkeit.** Im Zweifel wird nicht geschrieben, sondern zur Prüfung markiert; der Zeit-Fallback der Preisformel fängt alles ab („Abstention is a feature").

### 1.3 Lösungsansatz

Kein Neubau. VF-Sync wird als **neuer, eigenständiger Worker-Container** in den bestehenden ogn_monitor-Stack integriert. Er konsumiert die bereits vorhandenen Ereignisse und persistierten Flugdaten des Monitors und kapselt ausschließlich die VF-spezifische Logik (Matching, Schreiben, Budget, Audit). Ergänzend werden fünf gezielte Härtungen an der Tracking-Schicht des Monitors vorgenommen (Kap. 6).

### 1.4 Nicht-Ziele (Out of Scope)

- Keine Änderung der VF-Preisformeln; der Zeit-Fallback bleibt dauerhaft aktiv.
- Keine Motorlaufzeiten (motorstart/motorend) — FLARM liefert keine Zählerstände.
- Kein automatisches Anlegen von Flügen (`flight/add`) und kein `jointowflights` in V1 (Feature-Flags, default aus).
- Keine Höhenermittlung für Winde/Eigenstart/Motorflug (nur F-Schlepp).
- Keine Erfassung von LFZ ohne FLARM / mit No-Track (laufen in Fallback bzw. manuelle Erfassung).
- Keine lückenlose Streckenflug-Aufzeichnung; relevant sind Start-, Schlepp- und Landeabschnitte im Platzradius.

---

## 2. Anforderungen (konsolidiert)

Vollständige Fassung im Lastenheft; hier die für die Implementierung bindende Kurzform.

### 2.1 Funktional

| ID | Anforderung |
|---|---|
| R-01 | Startzeit (`departuretime`, UTC) sofort nach erkanntem Abheben in den zugeordneten VF-Flug schreiben. |
| R-02 | Nach bestätigter Landung ein gebündelter Edit: `arrivaltime`, bei F-Schlepp `towheight` (Meter, **AGL**) + `towtime` (Minuten), finaler `landingcount`. |
| R-03 | Startartklassifikation: F-Schlepp (Paar-Korrelation), Winde (Steigsignatur), Eigenstart (Rollen-Flag + Profil), Motorflug (a priori aus Rolle). Nur F-Schlepps erhalten Höhen. |
| R-04 | Erkannte Startart ≠ im VF-Flug eingetragene Startart → **nicht** korrigieren, Prüfmarkierung. Leere Startart bei eindeutiger Erkennung darf ergänzt werden (erst nach Spike-Klärung des Mappings, s. Kap. 11). |
| R-05 | Touch & Go: Zwischenlandung mit Wiederstart < 90 s zählt `landing_count` hoch, beendet den Flug nicht. Geschrieben wird nur der finale Stand mit dem Lande-Update, nur bei hoher Konfidenz. |
| R-06 | Matching OGN-Flug ↔ VF-Flug: Kennzeichen + Zeitfenster + Startartplausibilität; mehrere offene Kandidaten desselben Kennzeichens → Anlegereihenfolge; Mehrdeutigkeit → `ambiguous`, kein Schreiben. |
| R-07 | Nachhol-Logik: Jeder Edit schreibt alle bekannten, in VF noch leeren Felder. Retry: 15 min (Flüge in der Luft), stündlich (gelandet), 21:00-Abschlusslauf; Aufbewahrung offener Sessions 7 Tage. |
| R-08 | Manuelle VF-Eingaben gewinnen immer: Read-before-Write, nur leere Felder befüllen; `landingcount` nur erhöhen, nie verringern. |
| R-09 | API-Budget: harter Guard (Stopp bei 450/500 Requests/Tag), automatische Degradation (Kap. 5.6). |
| R-10 | Audit-Log: jeder Schreibvorgang mit Session-Daten, Methode, Konfidenz, Vorher-Zustand nachvollziehbar. |
| R-11 | Dry-Run-Modus als erstklassiger Betriebsmodus (Validierungsphase; keine Schreibzugriffe, volles Audit). |
| R-12 | Monitoring: Health-Status (Feed-Alter, Queue-Tiefe, Budget), Alarmierung bei Feed tot > 30 min, Budget > 80 %, Session > 24 h pending, Schreibfehlerserie. |

### 2.2 Nichtfunktional

| ID | Anforderung |
|---|---|
| N-01 | Genauigkeit: Zeiten ±2 min; Ausklinkhöhe ±50 m in ≥ 90 % der Fälle; **Write Precision 100 %** (kein Wert am falschen Flug — hartes Abnahmekriterium). |
| N-02 | Verfügbarkeit 06–22 Uhr ≥ 99 %; Container-Restart-Policy; persistente Queue — kein Verlust erkannter, ungeschriebener Sessions. Während eines Ausfalls Geflogenes ist nicht rekonstruierbar → Zeit-Fallback (akzeptiert). |
| N-03 | VF-API-Konformität: ≤ 500 Requests/Tag je AppKey, technischer User mit Minimalrechten, keine kommerzielle Nutzung (s. Kap. 8.6). |
| N-04 | Datenschutz: nur DDB-getrackte LFZ; Rohspuren max. 30 Tage rollierend; Details Kap. 8.5. |
| N-05 | Konfiguration ohne Codeänderung (DB-Konfig je Mandant + ENV); Feature-Flags: `dry_run` (default an), `live_release`, `live_touchgo`, `auto_create`, `join_towflights` (alle default aus). |

---

## 3. Systemarchitektur

### 3.1 Zielbild im ogn_monitor-Stack

```
                       +----------------------------+
                       |  Nginx (+ React Frontend)  |
                       +-------------+--------------+
                                     |
                       +-------------v--------------+
                       |  API Server (FastAPI)      |◄─── NEU: /api/vfsync/* (Status,
                       |  REST + WebSocket          |     Audit-Ansicht, Tenant-Konfig)
                       +------+---------------+-----+
                              |               |
                  +-----------v---+   +-------v--------+
                  | PostgreSQL 16 |   | Redis 7        |
                  | + PostGIS     |   | Hot State      |
                  |               |   | PubSub:        |
                  | BESTAND:      |   |  event:{slug}  |
                  |  flight_status|   |  beacon:{slug} |
                  |  flight_log   |   | Streams (Pos.) |
                  | NEU:          |   +---+--------+---+
                  |  vf_sync_*    |       |        |
                  +-------^-------+       |        |
                          |               |        |
              +-----------+----+   +-----v---+ +--v-------------------+
              | APRS Worker    |──►| (HSET/  | | NEU: VF-SYNC WORKER  |
              | (BESTAND +     |   | PUBLISH)| | python -m app.vfsync |
              |  Härtungen     |   +---------+ |                      |
              |  Kap. 6)       |               | - Event Consumer     |
              | - APRS Client  |               | - Session Store      |
              | - StateMachine |               | - VF Matcher         |
              | - LaunchDetect.|               | - VF Writer          |
              | - Redis Writer |               | - Budget Guard       |
              +-------+--------+               | - Scheduler/Retry    |
                      |                        | - Audit Logger       |
              +-------v-----------+            +----------+-----------+
              | aprs.glidernet.org|                       |
              +-------------------+            +----------v-----------+
                                               | www.vereinsflieger.de|
                                               | interface/rest/*     |
                                               +----------------------+
```

**Kernentscheidung:** VF-Sync ist ein eigener Prozess/Container (gleiches Docker-Image, anderer Startbefehl — analog zum bestehenden Muster APRS-Worker vs. API-Server). Er hat **keinen** eigenen OGN-Zugang und keine eigene Erkennungslogik; er konsumiert ausschließlich, was Tracker und State-Synchronizer produzieren.

### 3.2 Datenfluss (Normalfall F-Schlepp)

1. APRS-Worker erkennt Abheben → State Machine emittiert `takeoff`-Event → Redis `PUBLISH event:{slug}` (Bestand).
2. VF-Sync-Worker (subscribed auf `event:*` der aktivierten Mandanten) legt eine `vf_sync_session` an (Status `tracking`), matcht gegen die VF-Tagesflüge und schreibt — Read-before-Write — `departuretime` (Status `departure_written`).
3. LaunchDetector fixiert Ausklinken (`release_alt_m`, `release_time`, Schlepper) → Event `launch_type_detected` → VF-Sync speichert Höhe/Zeit **nur intern** in der Session (`live_release` ist aus).
4. Touch & Gos (nach Härtung Kap. 6.1) → Event `touch_and_go` → Session-Zähler hoch, kein API-Call.
5. Landung bestätigt → Event `landing` → VF-Sync: frisches `flight/get`, gebündelter `flight/edit` (arrivaltime, towheight AGL, towtime, landingcount), Audit, Status `completed`.
6. Kein VF-Match vorhanden (Flug noch nicht angelegt) → Status `awaiting_match`, Retry-Scheduler übernimmt (R-07).

### 3.3 Warum Events + DB statt nur DB-Polling

PubSub liefert die Latenz für die Live-Startzeit (Sekunden statt Sync-Intervall); die `vf_sync_sessions`-Tabelle liefert Crash-Sicherheit und Retry-Grundlage. Beim Worker-Start werden offene Sessions aus der DB geladen und gegen `flight_status`/`flight_log` abgeglichen (Recovery); verpasste PubSub-Events während eines Neustarts werden so nachgeholt, solange der APRS-Worker lief.

---

## 4. Komponentendesign VF-Sync-Worker

Neues Paket `app/app/vfsync/` (bewusst parallel zu `tracking/`, `aprs/`, `api/`):

```
app/app/vfsync/
├── __init__.py
├── main.py               # Entrypoint: python -m app.vfsync
├── event_consumer.py     # Redis-PubSub-Subscriber (event:{slug}), Filter auf aktivierte Tenants
├── session_store.py      # CRUD für vf_sync_sessions (asyncpg), Recovery beim Start
├── matcher.py            # VF-Tagesflüge ↔ Session-Zuordnung (R-06)
├── writer.py             # Schreiblogik inkl. aller Invarianten (Kap. 5.4)
├── scheduler.py          # Retry-Läufe (15 min / 60 min / 21:00), Session-Expiry (7 d)
├── budget.py             # Tages-Request-Zähler, Guard, Degradationsstufen
├── audit.py              # Audit-Writer (vf_sync_audit)
├── confidence.py         # Konfidenz-Gates (Pairing, Landung, T&G, Höhe-Plausibilität)
├── vf_client/
│   ├── __init__.py
│   ├── client.py         # HTTP-Client: Auth-Lifecycle, Session-Reuse, Fehlerklassen
│   ├── models.py         # Pydantic-Modelle der VF-Antworten
│   ├── mapping.py        # Domain-Mapping (starttype E/W/F ↔ 1/3/5 ↔ intern; Zeiten UTC; Rundung)
│   └── mock.py           # Vollständiger Mock der genutzten Endpunkte (Testdouble, s. AP-2)
└── health.py             # /healthz-Endpoint (eigener kleiner aiohttp/FastAPI-Server im Worker)
```

### 4.1 Datenmodell (neue Migration `00X_vf_sync` – im Repo als SQL unter `db/migrations/`)

```sql
-- Mandanten-Konfiguration (1:1 zu airfields)
CREATE TABLE vf_sync_config (
  airfield_id      UUID PRIMARY KEY REFERENCES airfields(id) ON DELETE CASCADE,
  enabled          BOOLEAN NOT NULL DEFAULT FALSE,
  dry_run          BOOLEAN NOT NULL DEFAULT TRUE,
  vf_base_url      TEXT NOT NULL DEFAULT 'https://www.vereinsflieger.de',
  vf_cid           INTEGER,                    -- Vereinsnummer (optional)
  vf_username      TEXT,                       -- technischer User
  vf_password_enc  BYTEA,                      -- verschlüsselt, s. Kap. 8.2 (NIE Klartext)
  vf_appkey_enc    BYTEA,                      -- verschlüsselt
  flags            JSONB NOT NULL DEFAULT '{}'::jsonb,  -- live_release, live_touchgo, auto_create, join_towflights
  daily_budget     INTEGER NOT NULL DEFAULT 450,
  created_at       TIMESTAMPTZ DEFAULT NOW(),
  updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Führendes Aggregat je erkanntem Flug (Idempotenz-Anker)
CREATE TABLE vf_sync_sessions (
  session_id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  airfield_id          UUID NOT NULL REFERENCES airfields(id),
  flarm_id             VARCHAR(16) NOT NULL,
  registration         VARCHAR(16),
  takeoff_ts           TIMESTAMPTZ,
  landing_ts           TIMESTAMPTZ,
  landing_method       VARCHAR(16),            -- observed | silence
  start_type_detected  VARCHAR(16),            -- aerotow | winch | self | powered | unknown
  tow_registration     VARCHAR(16),
  release_ts           TIMESTAMPTZ,
  release_alt_agl_m    INTEGER,                -- bereits AGL-konvertiert!
  release_method       VARCHAR(24),            -- pair_separation | towplane_max
  tow_time_min         INTEGER,
  landing_count        INTEGER NOT NULL DEFAULT 1,
  conf_pairing         REAL,                   -- 0..1
  conf_landing         REAL,
  conf_touchgo         REAL,
  matched_flid         BIGINT,                 -- VF-Flugnummer
  state                VARCHAR(24) NOT NULL DEFAULT 'tracking',
    -- tracking | awaiting_match | matched | departure_written | completed | review | expired
  review_reason        TEXT,
  attempts             INTEGER NOT NULL DEFAULT 0,
  last_attempt         TIMESTAMPTZ,
  created_at           TIMESTAMPTZ DEFAULT NOW(),
  updated_at           TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (airfield_id, flarm_id, takeoff_ts)
);
CREATE INDEX idx_vfsync_open ON vf_sync_sessions(airfield_id, state)
  WHERE state NOT IN ('completed','expired');

-- Audit: jeder API-Schreibversuch, unveränderlich (append-only)
CREATE TABLE vf_sync_audit (
  id            BIGSERIAL PRIMARY KEY,
  session_id    UUID REFERENCES vf_sync_sessions(session_id),
  airfield_id   UUID NOT NULL,
  ts            TIMESTAMPTZ DEFAULT NOW(),
  action        VARCHAR(24) NOT NULL,   -- get | edit | match | abstain | error | dryrun_edit
  flid          BIGINT,
  fields_sent   JSONB,                  -- exakt gesendete Felder
  pre_state     JSONB,                  -- VF-Zustand aus dem Read-before-Write
  http_status   INTEGER,
  detail        TEXT
);

-- API-Budget je Mandant und Tag
CREATE TABLE vf_sync_budget (
  airfield_id  UUID NOT NULL,
  day          DATE NOT NULL,
  used         INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (airfield_id, day)
);
```

### 4.2 Matcher (R-06)

Eingabe: Session + Ergebnis von `flight/list/today` (Cache ≤ 5 min pro Mandant). Kandidatenfilter in dieser Reihenfolge:

1. `callsign` = Session-Registration (normalisiert: Bindestriche/Whitespace/Case).
2. Flug am selben Tag, `departuretime` leer **oder** innerhalb ±30 min der Session-Startzeit.
3. Startartplausibilität: VF-`starttype` leer oder kompatibel zur erkannten Startart (Mapping Kap. 5.3). Inkompatibel → Kandidat verwerfen und Session-Flag für Review setzen.
4. Zielfeld-Check: mindestens eines der zu schreibenden Felder ist leer.

Entscheidung: genau 1 Kandidat → Match. Mehrere → sortiere nach Anlegereihenfolge (`flid` aufsteigend) und nimm den ersten **nur**, wenn kein zweiter Kandidat im selben ±30-min-Fenster liegt; sonst `review` mit `review_reason='ambiguous_match'`. Null Kandidaten → `awaiting_match` (Retry).

### 4.3 Scheduler

- Alle 15 min: Sessions in `awaiting_match` mit `landing_ts IS NULL` (Flug in der Luft — spät angelegter Flug bekommt Startzeit noch im Flug).
- Stündlich: alle offenen Sessions.
- 21:00 lokal: Abschlusslauf.
- Täglich 03:00: Sessions > 7 Tage → `expired`; Budget-Tabelle Vortagsabschluss ins Log.
- Recovery beim Start: offene Sessions laden, gegen `flight_status`/`flight_log` abgleichen (Landung während Downtime? → Landedaten aus `flight_log` übernehmen, Quelle im Audit vermerken).

---

## 5. VF-API-Adapter (vf_client)

### 5.1 Genutzte Endpunkte

| Endpunkt | Zweck | Methode |
|---|---|---|
| `interface/rest/auth/accesstoken` | Token holen | GET |
| `interface/rest/auth/signin` | Login (md5(password), appkey, optional cid) | POST |
| `interface/rest/flight/list/today` | Tagesflüge fürs Matching | POST |
| `interface/rest/flight/get/[flid]` | Read-before-Write | POST |
| `interface/rest/flight/edit/[flid]` | Schreiben | PUT |
| `interface/rest/flight/list/plane` | Spike/Fallback-Leseweg | POST |
| `interface/rest/flight/list/modified` | Spike/Fallback-Leseweg | POST |
| (`flight/add`, `flight/jointowflights`) | nur hinter Flags, V1 aus | POST/PUT |

Session-Handling: Accesstoken cachen und wiederverwenden; bei HTTP 401 genau ein Re-Login, dann Fehlerpfad. Jeder HTTP-Call inkrementiert das Budget (auch fehlgeschlagene).

### 5.2 Fehlerklassen

| HTTP | Verhalten |
|---|---|
| 200 | ok |
| 400 | **kein** Blind-Retry: Session → `review`, Audit mit Payload |
| 401 | einmal Re-Auth, dann wie 5xx |
| 403 (Login) | Alarm „Credentials/2FA", Mandant pausieren |
| 5xx / Netz / Timeout | Retry mit exponentiellem Backoff (max. 3), dann Scheduler |

### 5.3 Domain-Mapping

```python
class StartType(str, Enum):
    AEROTOW = "aerotow"; WINCH = "winch"; SELF = "self"; POWERED = "powered"; UNKNOWN = "unknown"

WRITE_MAP = {AEROTOW: "F", WINCH: "W", SELF: "E"}          # POWERED: erst nach Spike-Klärung (Kap. 11)
READ_MAP  = {1: SELF_OR_POWERED, 3: AEROTOW, 5: WINCH, 7: BUNGEE, 9: VEHICLE}
```

- Zeiten: intern durchgehend UTC-`datetime`; VF-Format `YYYY-mm-dd HH:ii` (Minutengranularität, Sekunden abschneiden).
- `towtime`: Sekunden → Minuten; Rundungsregel ist Spike-Ergebnis (Default bis dahin: kaufmännisch runden, als Konstante `TOWTIME_ROUNDING` zentral).
- `towheight`: **immer AGL** = Session-Wert `release_alt_agl_m` (Konvertierung passiert in der Tracking-Härtung, Kap. 6.3 — der Adapter konvertiert nicht selbst).

### 5.4 Schreib-Invarianten (nicht verhandelbar, testgesichert)

Jeder Aufruf von `writer.write(session)` durchläuft:

1. **Budget-Guard:** verbleibendes Tagesbudget ≥ 2 (Get+Edit), sonst nur Queue + Alarm.
2. **Konfidenz-Gate** (`confidence.py`): je Feld — towheight nur bei `conf_pairing ≥ 0.9` und Plausibilität 100 ≤ AGL ≤ 2000 m; landingcount-Erhöhung nur bei `conf_touchgo ≥ 0.9`; arrivaltime bei `landing_method='silence'` nur mit `conf_landing ≥ 0.8`. Nicht bestanden → Feld auslassen, `review_reason` ergänzen, Rest schreiben.
3. **Read-before-Write:** `flight/get/[flid]`; `pre_state` ins Audit.
4. **Feld-Filter:** nur Felder senden, die in VF **jetzt** leer/0 sind. `landingcount`: nur senden, wenn Session-Wert > VF-Wert (nie verringern); VF-Wert > Session-Wert → nicht senden + `review`.
5. **Startart-Konflikt:** VF-`starttype` gesetzt und inkompatibel → gesamten Edit abbrechen, `review` (R-04).
6. **Dry-Run:** `dry_run=true` → Schritte 1–5 vollständig ausführen, statt PUT ein Audit-Eintrag `dryrun_edit` mit exakt dem Payload, der gesendet worden wäre.
7. **Edit + Audit:** PUT senden; Antwort, Status, Payload ins Audit; Session-State fortschreiben.
8. **Idempotenz:** Anker ist `session_id`; ein erneuter Lauf derselben Session erzeugt wegen Schritt 4 keinen Doppel-Effekt.

### 5.5 Live-Startzeit (R-01)

Beim `takeoff`-Event: Match (ggf. `list/today` laden) → `writer.write(session, fields={'departuretime'})` mit denselben Invarianten. Kein Match → `awaiting_match` (kein Fehler).

### 5.6 Budget-Degradation

Stufen je Mandant/Tag (Basis `daily_budget=450`):
- **< 60 %:** Normalbetrieb (Startzeit live + gebündeltes Lande-Update; ~4 Calls/Flug inkl. Gets).
- **≥ 60 % oder > 80 Flugbewegungen:** neue Flüge ohne Live-Startzeit (nur Lande-Bündel, 2 Calls/Flug); Alarm-Info.
- **≥ 90 %:** nur noch F-Schlepp-Sessions schreiben (Abrechnungspriorität), Rest queued auf Folgetag-Retry; Alarm.
- **≥ 100 % (450):** Hard-Stop, alles queued, Alarm kritisch.

Kalkulation Spitzentag (70 Bewegungen, Normalbetrieb): 70 × 4 + ~20 `list/today` + Auth/Retries ≈ 310–340 — unter 450.

---

## 6. Härtungen an ogn_monitor (minimal-invasiv)

Nur diese Eingriffe in Bestandscode sind zulässig. Jede Härtung: eigenes Arbeitspaket, eigene Tests, keine Verhaltensänderung für das bestehende Frontend außer wo benannt.

### 6.1 Touch & Go (flight_state_machine.py)

**Ist:** Wiederstart nach Landung → `flight_restarted`, alter Flug archiviert, neuer Flug. Das widerspricht der VF-Semantik (ein Flug, `landingcount++`).
**Soll:** Neuer Konfig-Parameter `touch_go_max_ground_s: int = 90` in `AirfieldConfig`. Erfolgt der Wiederstart desselben LFZ innerhalb dieser Zeit nach der Landung (bzw. wird das Landekriterium nur kurz durchlaufen, ohne dass `landing_speed`-Hysterese voll greift): **kein** Restart — Status zurück auf FLYING, neues Feld `FlightState.landing_count += 1`, `landing_time` leeren, Event `touch_and_go` emittieren. „Hüpfer" < 15 s Abstand = eine Landung (Entprellung). Nach `touch_go_max_ground_s` gilt die Landung als final (bestehendes Verhalten unverändert). `landing_count` in `to_redis_dict`/`from_redis`, `flight_status`- und `flight_log`-Migration (Spalte `landing_count INT DEFAULT 1`) ergänzen; Frontend darf den Zähler anzeigen (optional, kein Muss).
**Konfidenz:** `touch_and_go`-Event trägt `confidence` (Kombination aus AGL-Minimum über Pistenbereich, Speed-Minimum, Wiederanstieg); der VF-Sync wertet sie (Kap. 5.4).

### 6.2 Schlepp-Paarung One-to-One (launch_detector.py)

**Ist:** `_find_tow_plane` nimmt den ersten passenden Kandidaten; keine Ambiguitätsbehandlung; keine Konfidenz.
**Soll:** Kandidaten-Scoring statt First-Match: Score aus Distanz, Höhendifferenz, Startzeitversatz, Kursähnlichkeit, Korrelationsdauer. Ein Schlepper wird zeitgleich höchstens **einem** Segler zugeordnet (globale Zuordnung über alle pending Detections eines Platzes; bei Konflikt gewinnt der höhere Score, der andere bleibt unklassifiziert bis Fenster-Ende). Liegen zwei Kandidaten-Scores näher als `PAIRING_AMBIGUITY_MARGIN` beieinander → `launch_type='aerotow_ambiguous'`, keine `release_alt`. Ergebnisfelder: `FlightState.pairing_confidence: float`, im Event `launch_type_detected` mitliefern.

### 6.3 Ausklinkhöhe AGL + Fallback (launch_detector.py)

**Ist:** `release_alt_m = beacon.altitude` (MSL).
**Soll:** `release_alt_agl_m = beacon.altitude − config.elevation_m` (neues Feld; MSL-Feld für Anzeige-Kompatibilität behalten). Fallback bei lückenhafter Seglerspur: maximale Schlepperhöhe im Steigflug − elevation, gekennzeichnet `release_method='towplane_max'`. Plausibilitätsfenster 100–2000 m AGL wird nicht hier erzwungen (Anzeige darf alles zeigen), sondern im VF-Sync-Gate.

### 6.4 LFZ-Rollen (aircraft-Tabelle + Resolver)

**Ist:** Eigenstarter via globale Modell-Liste; Schlepper via `tow_plane_flarm_ids`-Array in der Airfield-Konfig.
**Soll:** Migration: `aircraft.role VARCHAR(16) DEFAULT NULL` (`towplane | glider | motorglider_sl | powered`). LaunchDetector nutzt Rolle vorrangig, Modell-Liste nur als Fallback; `powered`/`towplane` → Klassifikation a priori POWERED. Frontend-Pflege der Rolle über bestehende Aircraft-Verwaltung (kleines Feld, optional in V1 per SQL/Seed).

### 6.5 Silence-Landung (flight_state_machine.py)

**Ist:** SIGNAL_LOST/ALARM-Eskalation existiert; eine Landung ohne beobachtete Bodenphase wird aber nicht als Landung gewertet.
**Soll:** Neuer Pfad: letzte Beacons im Platzbereich ∧ AGL < `near_ground_band_m` ∧ sinkend ∧ Speed stark fallend, danach Funkstille > `silence_landing_s` (default 180) → Status LANDING mit `landing_method='silence'`, `landing_time` = Zeit der letzten Bodennähe-Position, reduzierte `landing_confidence`. Wieder auftauchende Luft-Beacons vor Ablauf → Reset (kein Phantom). Bestehende ALARM-Logik bleibt unberührt (Silence-Landung unterdrückt den Alarm für dieses LFZ).

---

## 7. Konfiguration

### 7.1 ENV (Worker-Prozess, `config.py`-Erweiterung)

```
VFSYNC_ENABLED=true
VFSYNC_CRED_KEY=<32-Byte-Schlüssel, base64>   # Fernet-Key für vf_password_enc/vf_appkey_enc
VFSYNC_HEALTH_PORT=8090
VFSYNC_ALERT_NTFY_URL= / VFSYNC_ALERT_SMTP_*   # mind. ein Kanal
```

### 7.2 Je Mandant (vf_sync_config, Pflege über API/Frontend)

`enabled`, `dry_run`, Credentials (verschlüsselt), `flags` (live_release, live_touchgo, auto_create, join_towflights — alle default false), `daily_budget`.

**Startzustand nach Deployment:** `enabled=false` für alle Mandanten; SFG Werdenfels wird manuell aktiviert mit `dry_run=true`. Scharfschaltung (`dry_run=false`) erst nach bestandener Validierung — bewusst kein Automatismus.

---

## 8. Sicherheitskonzept

### 8.1 Bedrohungsmodell (STRIDE-Kurzfassung)

| Bedrohung | Szenario | Gegenmaßnahme |
|---|---|---|
| Spoofing | Gefälschte FLARM-Beacons/OGN-Daten manipulieren Abrechnungshöhen | Konfidenz-Gates, Plausibilitätsfenster, Paar-Korrelation (eine gefälschte Spur ohne passenden Schlepper schreibt keine Höhe); Audit macht Manipulation nachvollziehbar; Restrisiko dokumentiert — FLARM/OGN ist unauthentifiziert, die Preisformel-Fallbacks und die monatliche Abrechnungsprüfung sind die Kompensation |
| Tampering | Manipulation der Session-/Audit-Daten in der DB | DB nur im internen Docker-Netz; Audit append-only (kein UPDATE/DELETE im Code, DB-Rolle des Workers ohne DELETE-Grant auf `vf_sync_audit`) |
| Repudiation | „Das hat das System falsch eingetragen" | Vollständiges Audit mit pre_state, Payload, Konfidenz, Methode je Schreibvorgang (R-10) |
| Information Disclosure | VF-Credentials, Positionsdaten leaken | Kap. 8.2/8.5; Logs ohne Secrets (structlog-Processor filtert `password`, `appkey`, `accesstoken`) |
| DoS | OGN-Feed-Flut / API-Budget-Erschöpfung | Radius-/Flottenfilter (Bestand), Budget-Guard mit Degradation (Kap. 5.6), Backoff |
| Elevation of Privilege | Kompromittierter Worker schreibt beliebig in VF | Technischer VF-User mit Minimalrechten (nur Flugdatenerfassung, keine Mitglieder-/Finanzrechte); Schaden auf Flugdaten des Vereins begrenzt; 400/Fehlerserie → Alarm |

### 8.2 Secrets-Management

- VF-Passwort wird **als md5-Hash** benötigt (API-Vorgabe) → es wird ausschließlich der md5-Hash erfasst und gespeichert, nie das Klartextpasswort („store what you send").
- Speicherung je Mandant in `vf_sync_config` **verschlüsselt** (Fernet/AES-GCM) mit Schlüssel aus ENV (`VFSYNC_CRED_KEY`); der Schlüssel liegt nur auf dem Host (Docker-Secret/.env mit 600er-Rechten), nie in DB, Repo oder Image.
- Kein Secret in Code, Logs, Audit (`fields_sent` enthält nie Auth-Parameter), Testfixtures oder Fehlermeldungen. `.env` in `.gitignore`; CI-Check (gitleaks o. ä.) im Repo verankern.
- Accesstoken nur im Prozessspeicher, nie persistiert.
- Kompromittierungs-Playbook im Betriebshandbuch: VF-Passwort des technischen Users ändern + AppKey neu erzeugen = vollständige Rotation, < 10 min.

### 8.3 Rechte- und Zugriffskonzept

- **VF:** dedizierter technischer Benutzer je Verein, minimale Rollen (Flugdatenerfassung lesen/schreiben; explizit keine Mitgliederdaten-, Buchhaltungs- oder Adminrechte). Kein persönlicher Account, kein Admin-Account.
- **DB:** eigene Postgres-Rolle `vfsync` mit GRANT nur auf `vf_sync_*` (RW), `flight_status`/`flight_log`/`aircraft`/`airfields` (RO); kein DDL, kein DELETE auf Audit.
- **Redis:** bestehende Instanz im internen Netz; Worker subscribed nur, published nicht auf Bestandskanäle.
- **API-Endpunkte `/api/vfsync/*`:** nur für authentifizierte Tenant-Nutzer (bestehendes JWT), Credentials-Felder write-only (nie im GET zurückgeben, nur „gesetzt: ja/nein").
- **Container:** non-root User im Image, read-only Root-FS wo möglich, kein eingehender Port außer Health (nur internes Netz/hinter Nginx mit Auth).

### 8.4 Netzwerk

Ausgehend: `aprs.glidernet.org:14580` (Bestand, nur APRS-Worker), `www.vereinsflieger.de:443` (nur VF-Sync-Worker), Alert-Kanal. Eingehend: nichts Neues öffentlich. TLS-Verifikation für VF-Calls verpflichtend (kein `verify=False`, auch nicht in Tests — Mock nutzt lokalen Server).

### 8.5 Datenschutz (DSGVO)

- **Rechtsgrundlage/Zweck:** Vertrags-/Vereinszweck Flugbuchführung und Abrechnung; Verarbeitung nur für LFZ des Vereins bzw. mit OGN-DDB-Tracking-Freigabe; Vereinsbeschluss dokumentieren, Mitglieder informieren (Datenschutzhinweis-Ergänzung).
- **Datenminimierung:** VF-Sync speichert keine Rohspuren — nur verdichtete Sessions (Zeiten, Höhe, Zähler, Konfidenzen). Rohpositionsdaten verbleiben in den Bestands-Redis-Streams (TTL prüfen/setzen: max. 30 Tage). Für Regression dauerhaft benötigte Mitschnitte werden pseudonymisiert (fremde FLARM-IDs entfernt/gehasht).
- **Speicherfristen:** Sessions/Audit: Aufbewahrung wie Abrechnungsunterlagen des Vereins (konfigurierbar, Default 2 Jahre, dann Löschjob); offene Sessions 7 Tage → expired.
- **Betroffenenrechte:** Auskunft/Löschung über Audit-Query je Kennzeichen möglich (dokumentierte SQL im Betriebshandbuch).
- **Auftragsverarbeitung:** Bei Betrieb für fremde Vereine (Multi-Tenant) ist Achim/Silver7 Auftragsverarbeiter → AV-Vertrag je Verein erforderlich; Mandantentrennung logisch über `airfield_id` in allen Queries (Tests erzwingen den Filter).

### 8.6 VF-Nutzungsbedingungen (rechtliche Leitplanke)

Die VF-REST-API untersagt kommerzielle Nutzung und limitiert auf 500 Requests/Tag je AppKey. Konsequenzen im Design: Budget-Guard (Kap. 5.6); je Verein eigener AppKey + eigener technischer User (kein geteilter Key über Mandanten). **Vor** einer Bereitstellung des VF-Sync-Features für Dritte — erst recht als Bezahlfunktion des SaaS-Monitors — ist eine schriftliche Freigabe von Vereinsflieger.de einzuholen; bis dahin ist das Feature technisch auf explizit freigeschaltete Mandanten begrenzt (`enabled` default false, keine Selbstaktivierung im Frontend).

### 8.7 Supply Chain & Betrieb

- Dependencies gepinnt (bestehendes Muster in requirements.txt fortführen); Dependabot/Renovate aktivieren; Image-Rebuild bei CVE-Findings.
- CI: Tests + Linting + Secret-Scan verpflichtend vor Merge; Image-Build reproduzierbar (Multi-Stage, Bestand).
- Logs strukturiert (structlog, Bestand), keine personenbezogenen Daten über das Erforderliche hinaus; Log-Level via ENV.
- Backup: täglicher pg_dump inkl. `vf_sync_*` (Audit ist abrechnungsrelevant); Restore-Test dokumentieren.

---

## 9. Teststrategie

1. **Unit:** Mapping (starttype, Zeiten, Rundung — parametrisiert), Konfidenz-Gates, Budget-Degradationsstufen, Matcher-Entscheidungstabelle (eindeutig/mehrdeutig/leer/Startart-Konflikt).
2. **Writer-Invarianten (kritischste Suite):** gegen VF-Mock — Read-before-Write erzwungen (Mock zählt Reihenfolge), Nur-leere-Felder (Mock mit vorbefüllten Feldern → dürfen nie im PUT auftauchen), landingcount-nie-verringern, Dry-Run sendet nie PUT, Budget-Hard-Stop, 400-kein-Retry, Race-Test: Mock ändert Feld zwischen get und edit → Feld darf nicht gesendet werden (Mock validiert Payload).
3. **Tracking-Härtungen:** Replay-Tests über aufgezeichnete/synthetische Beacon-Sequenzen (bestehendes Test-Setup nutzen): T&G vs. Endlandung vs. tiefer Durchstart, Hüpfer-Entprellung, Paarungs-Ambiguität (2 Schlepps parallel), AGL-Konvertierung, Silence-Landung vs. Funkloch-im-Endanflug.
4. **Integration:** docker-compose-Testprofil mit Postgres+Redis+Mock-VF; Ende-zu-Ende: synthetischer Flugtag → Sessions → Audit-Soll-Abgleich (Golden-File).
5. **Recovery:** Worker-Kill zwischen takeoff und landing → Neustart → Session wird aus flight_log vervollständigt und korrekt gebündelt geschrieben.
6. **Abnahme (real):** Dry-Run-Validierungsphase gemäß Lastenheft (≥ 14 Betriebstage, Mindeststichproben, Write Precision 100 %). Nicht Teil der CI.

---

## 10. Arbeitspakete für die agentenbasierte Umsetzung

Jedes AP: Branch `feat/vfsync-apN`, Tests zuerst oder parallel, DoD = Tests grün + Lint sauber + kurzer PR-Text mit SPEC-DEVIATION/OPEN-QUESTION-Liste.

| AP | Inhalt | Abhängig von | Schätzung |
|---|---|---|---|
| **AP-0** | Projektvorbereitung im Repo: Paket `app/app/vfsync/` skeleton, ENV-Erweiterung, Migration Kap. 4.1 (SQL in `db/migrations/`), Compose-Service `vfsync` (gleiches Image, `python -m app.vfsync`, profile `vfsync`), CI-Erweiterung (Secret-Scan) | — | 0,5 PT |
| **AP-1** | `vf_client`: models, mapping (inkl. READ/WRITE_MAP, Zeitformat, Rundungskonstante), client mit Auth-Lifecycle & Fehlerklassen; Unit-Tests Mapping | AP-0 | 1 PT |
| **AP-2** | **VF-Mock**: lokaler FastAPI-Testserver, der die genutzten Endpunkte inkl. dokumentierter Antwortformate simuliert; konfigurierbare Szenarien (leerer Flug, vorbefüllte Felder, 400/401/5xx, Feldänderung zwischen get und edit); Fixture für pytest + Compose-Testprofil | AP-1 | 1 PT |
| **AP-3** | `session_store`, `event_consumer` (Subscribe `event:{slug}` für enabled-Tenants; Events: takeoff, launch_type_detected, landing, flight_restarted→vorerst landing-final), Recovery-Logik | AP-0 | 1 PT |
| **AP-4** | `matcher` + Entscheidungstabellen-Tests (R-06) | AP-2, AP-3 | 0,5 PT |
| **AP-5** | `writer` + `budget` + `confidence` + `audit` mit vollständiger Invarianten-Suite (Kap. 5.4, Kap. 9.2) — das Herzstück | AP-4 | 1,5 PT |
| **AP-6** | `scheduler` (Retry-Kadenz, Expiry, 21:00-Lauf) + Recovery-Integrationstest | AP-5 | 0,5 PT |
| **AP-7** | Härtung 6.3 AGL + 6.2 Paarung One-to-One/Konfidenz im LaunchDetector inkl. Replay-Tests | AP-0 | 1–1,5 PT |
| **AP-8** | Härtung 6.1 Touch & Go in StateMachine + FlightState + Migrationen + Event; Replay-Tests | AP-7 | 1–1,5 PT |
| **AP-9** | Härtung 6.5 Silence-Landung + 6.4 Rollen-Feld; Replay-Tests | AP-8 | 1 PT |
| **AP-10** | `/api/vfsync/*`: Tenant-Konfig (Credentials write-only, Fernet), Status-/Audit-Ansicht; minimale Frontend-Seite (Konfig + Session-/Audit-Tabelle + Budget-Anzeige) | AP-5 | 1–1,5 PT |
| **AP-11** | `health.py`, Alerting (ntfy/SMTP), Alarmregeln R-12; Betriebsdoku (Runbook: Rotation, Restore, Troubleshooting) | AP-6 | 0,5–1 PT |
| **AP-12** | **Mensch + Agent:** VF-API-Spike gegen Echtsystem (Kap. 11) — Ergebnisse in mapping/Matcher einpflegen (`TOWTIME_ROUNDING`, POWERED-starttype, ggf. Fallback-Leseweg) | AP-1 | 0,5 PT |
| **AP-13** | Ende-zu-Ende-Golden-File-Test (synthetischer Flugtag), Dry-Run-Deployment für SFG Werdenfels | alle | 0,5 PT |

**Summe: ~10–12 PT.** Kritischer Pfad: AP-0 → AP-1/2/3 → AP-4 → AP-5 → AP-6 → AP-13; Härtungen AP-7–9 parallelisierbar. AP-12 (Spike) so früh wie möglich einschieben — er ist Go/No-Go für das Matching-Konzept.

---

## 11. VF-API-Spike (Go/No-Go, vor bzw. parallel zu AP-4)

Am Echtsystem mit Wegwerf-Testflügen (anlegen → testen → löschen), manuell freigegeben, Budget beachten (~30 Requests):

1. Leer angelegter Flug (ohne departuretime) in `flight/list/today` enthalten? Falls nein: `flight/list/plane` / `flight/list/modified` prüfen; falls auch nein → Matching-Konzept eskalieren (OPEN-QUESTION, Stopp AP-4).
2. Teil-Edit: Werden nicht gesendete Felder unangetastet gelassen? (Erwartung: ja — verifizieren.)
3. `flighttime`: Berechnet VF sie neu, wenn departure/arrival per API gesetzt werden?
4. `towheight`-Bezug: AGL bestätigen (Testflug mit bekannter Höhe + Sichtprüfung Abrechnung/Anzeige; parallel Support-Anfrage).
5. `towtime`-Rundung: 5:31 min → 5 oder 6? → `TOWTIME_ROUNDING` setzen.
6. `starttype` für Motorflugzeug: Was schreibt die VF-Oberfläche selbst? `E` verifizieren; bis dahin POWERED-Flüge ohne starttype.
7. `landingcount`-Edit-Verhalten (Erhöhen ok? Default 1 bestätigt?).
8. Race-Verhalten: Feld manuell ändern zwischen get und edit → bestätigt Read-before-Write-Design.
9. Auffindbarkeit nach Mitternacht/`list/modified`-Semantik für den 21:00-/Folgetag-Retry.
10. 2FA am technischen User? (Falls ja: `auth_secret`-Handling klären oder 2FA für technischen User deaktivieren.)

Ergebnisse als `docs/vf-api-spike-results.md` ins Repo; Konstanten/Verhalten in `mapping.py`/`matcher.py` nachziehen.

---

## 12. Abnahme und Rollout

1. AP-0 bis AP-13 abgeschlossen, CI grün.
2. Dry-Run-Betrieb SFG Werdenfels über die Validierungsphase des Lastenhefts (≥ 14 Betriebstage, Mindeststichproben: ≥ 30 F-Schlepps, ≥ 2 Windentage, Eigenstarts, 1 T&G-Trainingstag, parallele Schlepps); wöchentlicher Abgleich Audit vs. Referenzprotokoll.
3. Abnahmekriterien des Lastenhefts (A1–A9), insbesondere Write Precision 100 %.
4. Vorstandsfreigabe → `dry_run=false` nur für SFG Werdenfels.
5. Hypercare 4 Wochen inkl. Begleitung der ersten Monatsabrechnung.
6. Danach Roadmap-Entscheidungen: `live_release`/`live_touchgo`, `auto_create`+`jointowflights`, VF-Freigabe für Fremdvereine (Kap. 8.6).

---

## Anhang A: Referenzierte Bestandsdateien (Andockpunkte)

| Datei | Relevanz |
|---|---|
| `app/app/tracking/flight_state_machine.py` | Härtungen 6.1, 6.5; Event-Emission (`_emit_event`) |
| `app/app/tracking/launch_detector.py` | Härtungen 6.2, 6.3; Quelle für release/tow-Daten |
| `app/app/tracking/flight_state.py` | Neue Felder: `landing_count`, `pairing_confidence`, `release_alt_agl_m`, `landing_method` |
| `app/app/tracking/redis_writer.py` | PubSub-Kanäle `event:{slug}`, `beacon:{slug}`; Streams |
| `app/app/tracking/state_synchronizer.py` | `flight_status`/`flight_log`-Persistenz (Recovery-Quelle) |
| `app/app/config.py` | ENV-Erweiterung (`VFSYNC_*`) |
| `db/migrations/` (+ `db/init.sql`) | Neue Migrationen: vf_sync_*, aircraft.role, landing_count-Spalten |
| `docker-compose.yml` | Neuer Service `vfsync` (gleiches Image) |
