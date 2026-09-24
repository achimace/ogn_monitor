# VF-Sync – interne Schnittstellen (verbindlich für alle Arbeitspakete)

Spezifikation: `docs/konzept-vf-sync.md` (gewinnt bei Widersprüchen). Dieses
Dokument legt nur die **Schnittstellen zwischen den Modulen** fest, damit die
Pakete unabhängig implementiert werden können. Alle Zeiten sind UTC-`datetime`
(tz-aware). Keine Secrets in Logs (structlog-Processor in `app/app/vfsync/logging.py`
maskiert `password`, `password_md5`, `appkey`, `accesstoken`).

```
app/app/vfsync/
├── __main__.py           python -m app.vfsync  → main.run()
├── main.py               Wiring: Stores, Client-Factory, Consumer, Scheduler, Health
├── models.py             Domain-Dataclasses (TenantConfig, Session, AuditEntry, Enums)
├── stores.py             Protocols: ConfigStore, SessionStore, AuditStore, BudgetStore
├── stores_memory.py      In-Memory-Implementierungen (Tests, Golden-File)
├── stores_pg.py          asyncpg-Implementierungen (vf_sync_* Tabellen)
├── crypto.py             Fernet: encrypt/decrypt(bytes) mit settings.vfsync_cred_key
├── logging.py            structlog-Secret-Filter
├── event_consumer.py     Redis PubSub event:{slug} → Coordinator
├── coordinator.py        Glue: Events/Retry → Matcher → Writer; Recovery
├── matcher.py            reine Funktion match_session()
├── confidence.py         reine Funktion gate_fields()
├── budget.py             BudgetGuard + Stage
├── audit.py              AuditLog (append-only)
├── writer.py             VfWriter.write() – Invarianten Kap. 5.4
├── scheduler.py          Retry-Läufe, Expiry, 21:00-Lauf
├── health.py             HTTP /healthz + Redis-Hash vfsync:health
├── alerts.py             AlertSink (ntfy / SMTP / log) + Regeln R-12
└── vf_client/
    ├── models.py         VfFlight (pydantic, extra="allow")
    ├── mapping.py        StartType, WRITE_MAP/READ_MAP, Zeitformat, TOWTIME_ROUNDING
    ├── client.py         VfClient (httpx), Fehlerklassen, on_request-Hook
    └── mock.py           MockVfStore + create_mock_app() (FastAPI, in-process)
```

## models.py

```python
class SessionState(str, Enum):
    TRACKING = "tracking"; AWAITING_MATCH = "awaiting_match"; MATCHED = "matched"
    DEPARTURE_WRITTEN = "departure_written"; COMPLETED = "completed"
    REVIEW = "review"; EXPIRED = "expired"

@dataclass
class TenantConfig:
    airfield_id: UUID; slug: str; enabled: bool; dry_run: bool
    vf_base_url: str; vf_cid: int | None; vf_username: str | None
    vf_password_md5: str | None      # entschlüsselt, NIE loggen
    vf_appkey: str | None            # entschlüsselt, NIE loggen
    flags: dict[str, bool]           # live_release, live_touchgo, auto_create, join_towflights
    daily_budget: int = 450
    timezone: str = "Europe/Berlin"
    def flag(self, name) -> bool

@dataclass
class Session:                      # 1:1 zu vf_sync_sessions (Kap. 4.1)
    airfield_id: UUID; flarm_id: str; registration: str | None
    takeoff_ts: datetime | None; session_id: UUID = uuid4()
    landing_ts, landing_method, start_type_detected, tow_registration,
    release_ts, release_alt_agl_m: int | None, release_method, tow_time_min: int | None,
    landing_count: int = 1, conf_pairing, conf_landing, conf_touchgo: float | None,
    matched_flid: int | None, state: SessionState = TRACKING, review_reason: str | None,
    attempts: int = 0, last_attempt, created_at, updated_at
    def add_review_reason(self, reason: str)   # dedupliziert, ';'-getrennt
    @property is_airborne -> landing_ts is None

@dataclass
class AuditEntry:
    airfield_id: UUID; action: str   # get | edit | match | abstain | error | dryrun_edit | recovery
    session_id: UUID | None = None; flid: int | None = None
    fields_sent: dict | None = None; pre_state: dict | None = None
    http_status: int | None = None; detail: str = ""; ts: datetime = now(); id: int | None = None
```

## stores.py (Protocols)

```python
class ConfigStore:
    async def load_enabled(self) -> list[TenantConfig]        # nur enabled=true, Credentials entschlüsselt
    async def load(self, airfield_id) -> TenantConfig | None
class SessionStore:
    async def upsert(self, s: Session) -> Session               # Schlüssel (airfield_id, flarm_id, takeoff_ts); gibt gespeicherte Session (mit session_id) zurück
    async def save(self, s: Session) -> None                    # UPDATE by session_id, setzt updated_at
    async def get(self, session_id) -> Session | None
    async def find_open_for_aircraft(self, airfield_id, flarm_id) -> Session | None   # jüngste Session mit state ∉ {completed, expired}
    async def list_open(self, airfield_id=None, states=None, airborne_only=False) -> list[Session]
    async def expire_older_than(self, days: int, now) -> int    # offene Sessions → EXPIRED, Anzahl
    async def count_today(self, airfield_id, day: date) -> int  # Flugbewegungen (Sessions mit takeoff am Tag)
class AuditStore:
    async def append(self, e: AuditEntry) -> AuditEntry
    async def list(self, airfield_id, session_id=None, limit=200) -> list[AuditEntry]
class BudgetStore:
    async def used(self, airfield_id, day: date) -> int
    async def increment(self, airfield_id, day: date, n: int = 1) -> int   # neuer Stand
```

`stores_memory.py`: `InMemoryConfigStore(tenants)`, `InMemorySessionStore`,
`InMemoryAuditStore`, `InMemoryBudgetStore`. `stores_pg.py`: `Pg…Store(pool)`.

## vf_client

```python
# mapping.py
class StartType(str, Enum): AEROTOW="aerotow"; WINCH="winch"; SELF="self"; POWERED="powered"; UNKNOWN="unknown"
WRITE_MAP = {AEROTOW: "F", WINCH: "W", SELF: "E"}          # POWERED bewusst nicht (Kap. 11)
READ_MAP  = {1: {SELF, POWERED}, 3: {AEROTOW}, 5: {WINCH}}   # 7 bungee, 9 vehicle → leer
VF_TIME_FMT = "%Y-%m-%d %H:%M"
TOWTIME_ROUNDING = "half_up"                                 # Spike-Ergebnis einpflegen (AP-12)
def to_vf_time(dt: datetime) -> str          # UTC, Sekunden abschneiden
def from_vf_time(s: str | None) -> datetime | None   # "" / None / "0000-00-00 00:00" → None
def tow_time_minutes(seconds: int) -> int
def is_starttype_compatible(detected: StartType, vf_starttype: int | str | None) -> bool  # leer → True
def normalize_callsign(s: str | None) -> str # "D-1234 " → "D1234"

# models.py
class VfFlight(BaseModel, extra="allow"):
    flid: int; callsign: str = ""; pilotname: str = ""; attendantname: str = ""
    departuretime: str = ""; arrivaltime: str = ""; starttype: int | str | None = None
    towheight: int | None = None; towtime: int | None = None; landingcount: int = 1
    towcallsign: str = ""; towflid: int | None = None; flighttime: str = ""; comment: str = ""
    @property departure_dt / arrival_dt -> datetime | None
    def is_empty(self, field: str) -> bool    # "" / None / 0 / "0000-00-00 00:00"

# client.py
class VfError(Exception): status: int | None
class VfBadRequest(VfError)        # 400 → kein Retry
class VfAuthError(VfError)         # 401
class VfForbidden(VfError)         # 403 (Login) → Mandant pausieren
class VfServerError(VfError)       # 5xx
class VfNetworkError(VfError)      # Timeout / Verbindungsfehler
class VfClient:
    def __init__(self, base_url, username, password_md5, appkey, cid=None, *,
                 http: httpx.AsyncClient | None = None, on_request: Callable[[], Awaitable[None]] | None = None,
                 timeout_s=15.0)
    async def close()
    async def signin() -> None                     # accesstoken (GET) + signin (POST); Token nur im Speicher
    async def list_today() -> list[VfFlight]
    async def get_flight(flid: int) -> VfFlight
    async def edit_flight(flid: int, fields: dict[str, str | int]) -> dict   # PUT, nur die übergebenen Felder
    async def list_plane(callsign: str) -> list[VfFlight]
    async def list_modified(since: datetime) -> list[VfFlight]
```
Jeder HTTP-Call (auch fehlgeschlagen, auch Auth) ruft `on_request()` **vorher**
auf (Budget). 401 → genau ein `signin()` + Wiederholung, dann `VfAuthError`.
5xx/Netz → bis zu 3 Versuche mit exponentiellem Backoff (0.5/1/2 s), dann Exception.
TLS-Verifikation nie abschalten.

Antwortformate (VF REST): Listen sind JSON-Objekte mit numerischen Keys
`{"0": {...}, "1": {...}, "httpstatuscode": 200}`; Einzelobjekte enthalten die
Felder direkt plus `httpstatuscode`. Der Client normalisiert beides.
Auth: `GET auth/accesstoken` → `{"accesstoken": "..."}`; `POST auth/signin`
form-encoded `accesstoken, username, password (md5), appkey, cid?`.
Alle weiteren Calls form-encoded mit `accesstoken`.

## mock.py

```python
class MockVfStore:
    flights: dict[int, dict]; requests: list[tuple[str, str, dict]]   # (method, path, payload ohne accesstoken)
    edits: list[tuple[int, dict]]
    def add_flight(self, **fields) -> int              # flid vergeben, Defaults: leere Zeiten, landingcount 1
    def fail_next(self, path_contains: str, status: int, times: int = 1)
    def mutate_after_get(self, flid: int, changes: dict) # nach dem NÄCHSTEN flight/get anwenden (Race-Test)
    def require_signin(self) / invalidate_token()      # 401-Szenario
    def order_of(self, flid) -> list[str]              # z. B. ["get", "edit"]
def create_mock_app(store: MockVfStore) -> FastAPI
def mock_client(store, **kw) -> VfClient               # httpx.ASGITransport, kein Netz
```
Der Mock validiert Edit-Payloads: unbekannte Felder oder Zeiten im falschen
Format → 400. Er lehnt `landingcount` < aktuellem Wert **nicht** ab (das muss
der Writer verhindern – Tests prüfen `store.edits`).
`python -m app.vfsync.vf_client.mock` startet ihn als uvicorn-Server (Compose-Testprofil).

## matcher.py / confidence.py / budget.py (reine Logik)

```python
WRITABLE_FIELDS = ("departuretime", "arrivaltime", "towheight", "towtime", "landingcount")

@dataclass
class MatchDecision:
    kind: Literal["matched", "awaiting_match", "ambiguous", "starttype_conflict"]
    flid: int | None = None; reason: str = ""
def match_session(session, flights: list[VfFlight], target_fields: set[str], window_min: int = 30) -> MatchDecision

def session_fields(session) -> dict[str, str | int]     # Session → VF-Felder (nur vorhandene Werte; towheight/towtime nur bei aerotow)
def gate_fields(session, fields: set[str]) -> tuple[set[str], list[str]]   # erlaubte Felder, review_reasons

class Stage(Enum): NORMAL; NO_LIVE_DEPARTURE; AEROTOW_ONLY; HARD_STOP
class BudgetGuard:
    def __init__(self, store: BudgetStore, clock=utcnow)
    async def used(self, tenant) -> int
    async def stage(self, tenant, movements_today: int) -> Stage
    async def reserve(self, tenant, n: int = 2) -> bool      # False bei HARD_STOP / Rest < n
    async def on_request(self, tenant) -> None               # Hook für VfClient
```

## writer.py

```python
@dataclass
class WriteResult:
    status: Literal["written", "dryrun", "skipped", "review", "deferred", "error"]
    sent: dict; reasons: list[str]; http_status: int | None = None

class VfWriter:
    def __init__(self, client_for: Callable[[TenantConfig], VfClient], sessions, audit: AuditLog, budget: BudgetGuard)
    async def write(self, session, tenant, fields: set[str] | None = None) -> WriteResult
```
Reihenfolge exakt Kap. 5.4: Budget → Konfidenz → `get_flight` (Audit `get`) →
Startart-Konflikt (Abbruch, `review`) → Feld-Filter (nur leere; landingcount
nur erhöhen, VF > Session → `review`) → leer? `skipped` → Dry-Run: Audit
`dryrun_edit` mit exaktem Payload, Session-State fortschreiben wie bei Erfolg →
sonst `edit_flight` + Audit `edit`. `VfBadRequest` → Audit `error`, Session
`review`, kein Retry. Andere Fehler → `error`, `attempts++`, Scheduler.
State-Fortschreibung: departuretime geschrieben → `departure_written`;
Lande-Bündel geschrieben (arrivaltime gesetzt oder bereits in VF) → `completed`.

## coordinator.py

```python
class SyncCoordinator:
    def __init__(self, stores, writer, matcher=match_session, budget, audit, alerts, clock)
    async def on_event(self, tenant, event: dict)     # dispatch nach event["type"]
    # takeoff → Session upsert (tracking) → match → ggf. departuretime (Stage NORMAL)
    # launch_type_detected → Session-Felder (start_type, tow_reg, release_ts, release_alt_agl_m, release_method, tow_time_min, conf_pairing)
    # touch_and_go → landing_count, conf_touchgo
    # landing_final → landing_ts, landing_method, conf_landing, landing_count → Lande-Bündel
    # landing_retracted → landing_ts = None
    # landing / flight_restarted / sticky_landed_expired / andere → ignorieren
    async def process(self, session, tenant) -> WriteResult | None   # Match + Write, von Scheduler/Recovery genutzt
    async def recover(self, tenant) -> int            # offene Sessions gegen flight_status/flight_log abgleichen
```
Event-Payload: `event["data"]` = `FlightState.to_redis_dict()` (alle Werte
Strings; Zeiten ISO `…Z`; `landing_count`, `release_alt_agl_m`,
`tow_duration_s`, `pairing_confidence`, `landing_confidence`,
`touch_go_confidence`, `landing_method`, `launch_type`, `tow_plane_reg`).

**SPEC-DEVIATION (Kap. 3.2/AP-3):** das Lande-Bündel wird auf `landing_final`
geschrieben, nicht auf `landing` – nach `landing` kann noch ein Touch & Go folgen.

## Health / Alerts

`vfsync:health` (Redis-Hash): `status`, `last_event_ts`, `open_sessions`,
`budget_used:{slug}`, `stage:{slug}`, `updated_at`. HTTP `GET /healthz`
liefert dasselbe als JSON (200 / 503 bei Feed tot > 30 min).
`AlertSink.send(level, title, message)`; Kanäle aus ENV; Regeln in
`alerts.evaluate()` (Feed tot > 30 min, Budget > 80 %, Session > 24 h pending,
≥ 3 Schreibfehler in Folge, Login 403).

## ENV (config.py)

`vfsync_enabled`, `vfsync_cred_key` (Fernet, base64), `vfsync_health_port`
(8090), `vfsync_alert_ntfy_url`, `vfsync_alert_email_to`, `vfsync_timezone`
(Europe/Berlin), `vfsync_config_reload_s` (300), `vfsync_list_cache_s` (300).

## Tests

`app/tests/vfsync/…`, alles ohne Netz/DB (In-Memory-Stores + Mock via
ASGITransport). PG-Stores: `test_stores_pg.py` läuft nur, wenn
`VFSYNC_TEST_DATABASE_URL` gesetzt ist (sonst skip).
