"""In-process mock of the Vereinsflieger REST endpoints used by VF-Sync.

Provides ``MockVfStore`` (state + scenario hooks), ``create_mock_app``
(FastAPI app mimicking the VF response shapes) and ``mock_client`` (a
``VfClient`` wired to the app via ``httpx.ASGITransport`` – no network).

Run ``python -m app.vfsync.vf_client.mock`` to serve it with uvicorn
(default port 8099, ``VF_MOCK_PORT``) for the compose test profile.

Response shapes (assumed, verify in AP-12): lists are JSON objects with
numeric string keys plus ``"httpstatuscode": 200``; single objects contain
the fields directly plus ``httpstatuscode``; all field values are strings;
errors are ``{"error": "...", "httpstatuscode": N}`` with matching HTTP
status.

The mock validates edit payloads (unknown fields, malformed times / ints →
400). It deliberately does **not** reject a ``landingcount`` lower than the
current value – that invariant belongs to the writer (tests check
``store.edits``).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.vfsync.vf_client.client import VfClient
from app.vfsync.vf_client.mapping import VF_TIME_FMT, normalize_callsign

__all__ = ["MockVfStore", "create_mock_app", "mock_client", "DEFAULT_MOCK_PORT"]

DEFAULT_MOCK_PORT = 8099
_API = "/interface/rest"

#: Fields a flight record carries; edit payloads may only contain these.
FLIGHT_FIELDS: tuple[str, ...] = (
    "callsign", "pilotname", "attendantname", "departuretime", "arrivaltime",
    "starttype", "towheight", "towtime", "landingcount", "towcallsign",
    "towflid", "flighttime", "comment", "departurelocation", "arrivallocation",
)
_TIME_FIELDS = ("departuretime", "arrivaltime")
_INT_FIELDS = ("towheight", "towtime", "landingcount", "towflid")
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
_STARTTYPE_RE = re.compile(r"^(?:[EWFS]|\d+)$", re.IGNORECASE)
_SECRET_FORM_KEYS = frozenset({"accesstoken", "password", "appkey"})


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()  # noqa: S324 - VF login format


class MockVfStore:
    """State and scenario hooks of the VF mock.

    Attributes:
        flights: flid → record (all values strings, like VF).
        requests: ``(method, path, payload)`` for every request, payload
            without ``accesstoken`` / ``password`` / ``appkey``.
        edits: ``(flid, fields)`` for every successful ``flight/edit``.
        signins: Number of successful signins.
        username / password_md5 / appkey: Credentials the mock accepts.
    """

    def __init__(self, username: str = "mock-user", password_md5: str | None = None,
                 appkey: str = "mock-appkey", cid: int | None = None) -> None:
        self.username = username
        self.password_md5 = password_md5 or _md5("mock-password")
        self.appkey = appkey
        self.cid = cid
        self.flights: dict[int, dict[str, str]] = {}
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.edits: list[tuple[int, dict[str, str]]] = []
        self.signins = 0
        self.modified: dict[int, datetime] = {}
        self._next_flid = 1001
        self._issued: set[str] = set()
        self._valid: set[str] = set()
        self._check_token = True
        self._failures: list[dict[str, Any]] = []
        self._mutations: dict[int, list[dict[str, Any]]] = {}

    # ---------------------------------------------------------------- data

    def add_flight(self, **fields: Any) -> int:
        """Add a flight; returns its flid.

        Defaults: empty times / tow fields, ``landingcount`` 1. ``flid`` may
        be given explicitly; otherwise ids are assigned from 1001 upwards.
        """
        flid = int(fields.pop("flid", 0)) or self._next_flid
        self._next_flid = max(self._next_flid, flid + 1)
        record: dict[str, str] = {name: "" for name in FLIGHT_FIELDS}
        record["landingcount"] = "1"
        for key, value in fields.items():
            record[key] = "" if value is None else str(value)
        record["flid"] = str(flid)
        self.flights[flid] = record
        self.modified[flid] = datetime.now(timezone.utc)
        return flid

    def flight(self, flid: int) -> dict[str, str]:
        """Current record of ``flid`` (KeyError if unknown)."""
        return self.flights[flid]

    # ------------------------------------------------------------ scenarios

    def fail_next(self, path_contains: str, status: int, times: int = 1) -> None:
        """Answer the next ``times`` requests whose path contains
        ``path_contains`` with ``status`` (before auth / handler logic)."""
        self._failures.append({"path": path_contains, "status": int(status), "times": int(times)})

    def mutate_after_get(self, flid: int, changes: dict[str, Any]) -> None:
        """Apply ``changes`` to ``flid`` right after the NEXT ``flight/get``
        of that flight is served (race between get and edit)."""
        self._mutations.setdefault(int(flid), []).append(
            {k: ("" if v is None else str(v)) for k, v in changes.items()})

    def require_signin(self, required: bool = True) -> None:
        """Enable (default) or disable access-token validation."""
        self._check_token = required

    def invalidate_token(self) -> None:
        """Invalidate all signed-in tokens → next call answers 401."""
        self._valid.clear()

    def order_of(self, flid: int) -> list[str]:
        """Sequence of ``get`` / ``edit`` requests for ``flid``."""
        out: list[str] = []
        for _method, path, _payload in self.requests:
            if path.endswith(f"/flight/get/{flid}"):
                out.append("get")
            elif path.endswith(f"/flight/edit/{flid}"):
                out.append("edit")
        return out

    def reset_log(self) -> None:
        """Clear request / edit logs (keeps flights and tokens)."""
        self.requests.clear()
        self.edits.clear()

    # ------------------------------------------------------------- internal

    def _issue_token(self) -> str:
        token = secrets.token_hex(16)
        self._issued.add(token)
        return token

    def _token_ok(self, token: str | None) -> bool:
        if not self._check_token:
            return True
        return bool(token) and token in self._valid

    def _pop_failure(self, path: str) -> int | None:
        for entry in self._failures:
            if entry["path"] in path and entry["times"] > 0:
                entry["times"] -= 1
                return entry["status"]
        return None

    def _record(self, method: str, path: str, form: dict[str, str]) -> None:
        payload = {k: v for k, v in form.items() if k not in _SECRET_FORM_KEYS}
        self.requests.append((method, path, payload))


# --------------------------------------------------------------------- app


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message, "httpstatuscode": status}, status_code=status)


def _list_response(records: list[dict[str, str]]) -> dict[str, Any]:
    body: dict[str, Any] = {str(i): dict(rec) for i, rec in enumerate(records)}
    body["httpstatuscode"] = 200
    return body


def _validate_edit(form: dict[str, str]) -> str | None:
    """Return an error message for an invalid edit payload, else None."""
    for key, value in form.items():
        if key not in FLIGHT_FIELDS:
            return f"unknown field '{key}'"
        if key in _TIME_FIELDS and value != "" and not _TIME_RE.match(value):
            return f"invalid time format for '{key}' (expected {VF_TIME_FMT})"
        if key in _TIME_FIELDS and value != "":
            try:
                datetime.strptime(value, VF_TIME_FMT)
            except ValueError:
                return f"invalid time value for '{key}'"
        if key in _INT_FIELDS and value != "" and not value.lstrip("-").isdigit():
            return f"invalid integer for '{key}'"
        if key == "starttype" and value != "" and not _STARTTYPE_RE.match(value):
            return f"invalid starttype '{value}'"
    return None


def create_mock_app(store: MockVfStore) -> FastAPI:
    """Build the FastAPI app serving the VF endpoints on top of ``store``."""
    app = FastAPI(title="Vereinsflieger mock", docs_url=None, redoc_url=None)

    async def _prepare(request: Request, *, auth: bool = True
                       ) -> tuple[dict[str, str], JSONResponse | None]:
        """Parse the form, log the request, apply failures and auth."""
        form: dict[str, str] = {}
        if request.method in ("POST", "PUT"):
            form = {k: str(v) for k, v in (await request.form()).items()}
        path = request.url.path
        store._record(request.method, path, form)
        status = store._pop_failure(path)
        if status is not None:
            return form, _error(status, f"injected failure {status}")
        if auth and not store._token_ok(form.get("accesstoken")):
            return form, _error(401, "not signed in")
        return form, None

    @app.get(f"{_API}/auth/accesstoken")
    async def accesstoken(request: Request) -> Any:
        _form, err = await _prepare(request, auth=False)
        if err:
            return err
        return {"accesstoken": store._issue_token(), "httpstatuscode": 200}

    @app.post(f"{_API}/auth/signin")
    async def signin(request: Request) -> Any:
        form, err = await _prepare(request, auth=False)
        if err:
            return err
        token = form.get("accesstoken", "")
        if token not in store._issued:
            return _error(401, "unknown accesstoken")
        creds_ok = (form.get("username") == store.username
                    and form.get("password") == store.password_md5
                    and form.get("appkey") == store.appkey)
        if store.cid is not None and form.get("cid") != str(store.cid):
            creds_ok = False
        if not creds_ok:
            return _error(403, "login failed")
        store._valid.add(token)
        store.signins += 1
        return {"httpstatuscode": 200}

    @app.post(f"{_API}/flight/list/today")
    async def list_today(request: Request) -> Any:
        _form, err = await _prepare(request)
        if err:
            return err
        return _list_response(list(store.flights.values()))

    @app.post(f"{_API}/flight/list/plane")
    async def list_plane(request: Request) -> Any:
        form, err = await _prepare(request)
        if err:
            return err
        wanted = normalize_callsign(form.get("callsign"))
        records = [r for r in store.flights.values() if normalize_callsign(r.get("callsign")) == wanted]
        return _list_response(records)

    @app.post(f"{_API}/flight/list/modified")
    async def list_modified(request: Request) -> Any:
        form, err = await _prepare(request)
        if err:
            return err
        raw = form.get("modified", "")
        try:
            since = datetime.strptime(raw, VF_TIME_FMT).replace(tzinfo=timezone.utc)
        except ValueError:
            return _error(400, "invalid 'modified' (expected YYYY-mm-dd HH:MM)")
        records = [r for flid, r in store.flights.items()
                   if store.modified.get(flid, since) >= since]
        return _list_response(records)

    @app.post(f"{_API}/flight/get/{{flid}}")
    async def get_flight(flid: int, request: Request) -> Any:
        _form, err = await _prepare(request)
        if err:
            return err
        record = store.flights.get(flid)
        if record is None:
            return _error(404, f"flight {flid} not found")
        body: dict[str, Any] = dict(record)
        body["httpstatuscode"] = 200
        pending = store._mutations.get(flid)
        if pending:
            record.update(pending.pop(0))
            store.modified[flid] = datetime.now(timezone.utc)
        return body

    @app.put(f"{_API}/flight/edit/{{flid}}")
    async def edit_flight(flid: int, request: Request) -> Any:
        form, err = await _prepare(request)
        if err:
            return err
        record = store.flights.get(flid)
        if record is None:
            return _error(404, f"flight {flid} not found")
        fields = {k: v for k, v in form.items() if k != "accesstoken"}
        problem = _validate_edit(fields)
        if problem:
            return _error(400, problem)
        record.update(fields)
        store.modified[flid] = datetime.now(timezone.utc)
        store.edits.append((flid, fields))
        return {"flid": str(flid), "httpstatuscode": 200}

    return app


async def _no_sleep(_delay: float) -> None:
    return None


def mock_client(store: MockVfStore, **kw: Any) -> VfClient:
    """``VfClient`` talking to ``create_mock_app(store)`` via ASGITransport.

    Credentials default to the store's; retry sleeps are skipped unless a
    ``sleep`` coroutine is given. Any ``VfClient`` kwarg may be overridden.
    """
    app = create_mock_app(store)
    transport = httpx.ASGITransport(app=app)
    http = kw.pop("http", None) or httpx.AsyncClient(transport=transport, base_url="http://vf-mock")
    kw.setdefault("sleep", _no_sleep)
    kw.setdefault("cid", store.cid)
    return VfClient(
        kw.pop("base_url", "http://vf-mock"),
        kw.pop("username", store.username),
        kw.pop("password_md5", store.password_md5),
        kw.pop("appkey", store.appkey),
        http=http,
        **kw,
    )


def _seeded_store() -> MockVfStore:
    store = MockVfStore(
        username=os.environ.get("VF_MOCK_USERNAME", "mock-user"),
        password_md5=os.environ.get("VF_MOCK_PASSWORD_MD5") or None,
        appkey=os.environ.get("VF_MOCK_APPKEY", "mock-appkey"),
    )
    store.add_flight(callsign="D-1234", pilotname="Test Pilot", starttype="3")
    store.add_flight(callsign="D-5678", pilotname="Winch Pilot", starttype="5",
                     departuretime="2026-09-24 08:00", arrivaltime="2026-09-24 08:20")
    store.add_flight(callsign="D-EKPO", pilotname="Tow Pilot", starttype="")
    return store


if __name__ == "__main__":  # pragma: no cover - manual / compose test profile
    import uvicorn

    port = int(os.environ.get("VF_MOCK_PORT", DEFAULT_MOCK_PORT))
    uvicorn.run(create_mock_app(_seeded_store()), host="0.0.0.0", port=port)  # noqa: S104
