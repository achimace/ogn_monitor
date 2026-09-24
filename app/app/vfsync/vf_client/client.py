"""Async HTTP client for the Vereinsflieger REST API (httpx).

Behaviour (docs/dev-guides/vfsync-internals.md, Kap. 5.1/5.2):

* Every HTTP call – including auth calls, retries and calls that end up
  failing – awaits ``on_request()`` *before* sending (budget accounting).
* Access token lives only in memory. Credentials are never logged; log
  events use the keys masked by ``app.vfsync.logging``.
* 401 → exactly one re-signin + one retry, then ``VfAuthError``.
* 403 on signin → ``VfForbidden`` (credentials / 2FA, tenant must pause).
* 400 → ``VfBadRequest`` immediately, no retry, response text attached.
* 5xx / transport errors / timeouts → up to ``max_attempts`` attempts with
  backoff ``backoff_s`` (default 0.5/1/2 s), then ``VfServerError`` /
  ``VfNetworkError``. Backoff and the sleep function are injectable so tests
  run without waiting.
* TLS verification is never disabled (no ``verify`` knob on purpose).

ASSUMPTIONS (verify in AP-12): parameter names ``callsign`` for
``flight/list/plane`` and ``modified`` (``YYYY-mm-dd HH:MM``) for
``flight/list/modified``; VF wraps errors as
``{"error": "...", "httpstatuscode": N}`` and may report an error status in
the body while the HTTP status is 200 – the client honours the body status
when present.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

import httpx
import structlog

from app.vfsync.vf_client.mapping import to_vf_time
from app.vfsync.vf_client.models import VfFlight

__all__ = [
    "VfError",
    "VfBadRequest",
    "VfAuthError",
    "VfForbidden",
    "VfServerError",
    "VfNetworkError",
    "VfClient",
    "DEFAULT_BACKOFF_S",
    "MAX_ATTEMPTS",
    "API_PREFIX",
]

log = structlog.get_logger("vfsync.client")

#: Path prefix of the VF REST interface below the tenant's base URL.
API_PREFIX = "interface/rest"
#: Backoff between attempts on 5xx / network errors (seconds).
DEFAULT_BACKOFF_S: tuple[float, ...] = (0.5, 1.0, 2.0)
#: Total attempts per HTTP call on 5xx / network errors.
MAX_ATTEMPTS = 3

OnRequest = Callable[[], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]


class VfError(Exception):
    """Base class for all VF client errors.

    Attributes:
        status: HTTP status (or VF body ``httpstatuscode``), if any.
        body: Response text (truncated), if any. Never contains credentials
            because VF error bodies only echo the error message.
    """

    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class VfBadRequest(VfError):
    """HTTP 400 – payload rejected; never retried."""


class VfAuthError(VfError):
    """HTTP 401 after the single re-signin."""


class VfForbidden(VfError):
    """HTTP 403 – credentials / 2FA problem; tenant must be paused."""


class VfServerError(VfError):
    """HTTP 5xx after all retries."""


class VfNetworkError(VfError):
    """Timeout or transport error after all retries."""


def _truncate(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _list_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the numerically keyed entries of a VF list response, in order."""
    items: list[tuple[int, dict[str, Any]]] = []
    for key, value in body.items():
        if isinstance(key, str) and key.isdigit() and isinstance(value, dict):
            items.append((int(key), value))
    items.sort(key=lambda kv: kv[0])
    return [v for _, v in items]


def _single_object(body: dict[str, Any]) -> dict[str, Any]:
    """Strip the transport-level ``httpstatuscode`` from a single object."""
    return {k: v for k, v in body.items() if k != "httpstatuscode"}


class VfClient:
    """Vereinsflieger REST client for one tenant.

    Args:
        base_url: Tenant base URL, e.g. ``https://www.vereinsflieger.de``
            (``interface/rest`` is appended unless already present).
        username: VF login name of the technical user.
        password_md5: MD5 hex digest of the password (VF signin format).
        appkey: VF application key.
        cid: Optional club id for multi-club accounts.
        http: Optional pre-built ``httpx.AsyncClient`` (tests: ASGITransport).
            If omitted the client creates and owns one.
        on_request: Awaited before *every* HTTP request (budget hook).
        timeout_s: Request timeout for the owned client.
        backoff_s: Sleep durations between attempts (index = attempt - 1).
        sleep: Sleep coroutine (patchable in tests).
        max_attempts: Total attempts on 5xx / network errors.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password_md5: str,
        appkey: str,
        cid: int | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        on_request: OnRequest | None = None,
        timeout_s: float = 15.0,
        backoff_s: Sequence[float] | None = None,
        sleep: Sleep | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        base = base_url.rstrip("/")
        if not base.endswith(API_PREFIX):
            base = f"{base}/{API_PREFIX}"
        self._base = base
        self._username = username
        self._password_md5 = password_md5
        self._appkey = appkey
        self._cid = cid
        self._on_request = on_request
        self._backoff_s: tuple[float, ...] = tuple(backoff_s) if backoff_s is not None else DEFAULT_BACKOFF_S
        self._sleep: Sleep = sleep or asyncio.sleep
        self._max_attempts = max(1, int(max_attempts))
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout_s)
        self._token: str | None = None
        self._log = log.bind(vf_base=base, vf_user=username)

    # ------------------------------------------------------------------ utils

    @property
    def signed_in(self) -> bool:
        """True while an access token is held in memory."""
        return self._token is not None

    def _url(self, path: str) -> str:
        return f"{self._base}/{path.lstrip('/')}"

    async def close(self) -> None:
        """Close the owned HTTP client and forget the access token."""
        self._token = None
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "VfClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------- transport

    async def _send(self, method: str, path: str,
                    data: dict[str, Any] | None) -> httpx.Response:
        """Send one logical request with retries on 5xx / network errors.

        Calls ``on_request()`` before each attempt. Returns any response with
        status < 500; raises ``VfServerError`` / ``VfNetworkError`` after the
        last failed attempt.
        """
        url = self._url(path)
        last_exc: VfError | None = None
        for attempt in range(1, self._max_attempts + 1):
            if self._on_request is not None:
                await self._on_request()
            try:
                resp = await self._http.request(method, url, data=data)
            except httpx.TimeoutException as exc:
                last_exc = VfNetworkError(f"timeout {method} {path}: {exc}")
                self._log.warning("vf_request_timeout", method=method, path=path,
                                  attempt=attempt, error=str(exc))
            except httpx.TransportError as exc:
                last_exc = VfNetworkError(f"network error {method} {path}: {exc}")
                self._log.warning("vf_request_network_error", method=method, path=path,
                                  attempt=attempt, error=str(exc))
            else:
                if resp.status_code < 500:
                    self._log.debug("vf_request", method=method, path=path,
                                    status=resp.status_code, attempt=attempt)
                    return resp
                last_exc = VfServerError(
                    f"{resp.status_code} {method} {path}: {_truncate(resp.text)}",
                    status=resp.status_code, body=_truncate(resp.text),
                )
                self._log.warning("vf_request_server_error", method=method, path=path,
                                  status=resp.status_code, attempt=attempt)
            if attempt < self._max_attempts:
                idx = min(attempt - 1, len(self._backoff_s) - 1)
                delay = self._backoff_s[idx] if self._backoff_s else 0.0
                await self._sleep(delay)
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _raise_for_status(status: int, method: str, path: str, text: str) -> None:
        body = _truncate(text)
        msg = f"{status} {method} {path}: {body}"
        if status == 400:
            raise VfBadRequest(msg, status=status, body=body)
        if status == 401:
            raise VfAuthError(msg, status=status, body=body)
        if status == 403:
            raise VfForbidden(msg, status=status, body=body)
        if status >= 500:
            raise VfServerError(msg, status=status, body=body)
        if status >= 400:
            raise VfError(msg, status=status, body=body)

    def _parse(self, resp: httpx.Response, method: str, path: str) -> dict[str, Any]:
        """Map HTTP / body status to exceptions and return the JSON body."""
        self._raise_for_status(resp.status_code, method, path, resp.text)
        try:
            body = resp.json()
        except ValueError as exc:
            raise VfError(f"invalid JSON from {method} {path}: {_truncate(resp.text)}",
                          status=resp.status_code, body=_truncate(resp.text)) from exc
        if not isinstance(body, dict):
            raise VfError(f"unexpected JSON shape from {method} {path}",
                          status=resp.status_code, body=_truncate(resp.text))
        inner = body.get("httpstatuscode")
        if inner is not None:
            try:
                inner_status = int(inner)
            except (TypeError, ValueError):
                inner_status = 200
            if inner_status != 200:
                self._raise_for_status(inner_status, method, path, resp.text)
        return body

    async def _call(self, method: str, path: str,
                    data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Authenticated call with exactly one re-signin on 401."""
        if self._token is None:
            await self.signin()
        for reauth in (False, True):
            payload: dict[str, Any] = {"accesstoken": self._token}
            if data:
                payload.update(data)
            resp = await self._send(method, path, payload)
            if resp.status_code == 401 and not reauth:
                self._log.info("vf_reauth", method=method, path=path)
                self._token = None
                await self.signin()
                continue
            return self._parse(resp, method, path)
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ auth

    async def signin(self) -> None:
        """Fetch an access token and sign in; token is kept in memory only.

        Raises:
            VfForbidden: Login rejected (403).
            VfAuthError: Login rejected (401) or no token in the response.
            VfServerError / VfNetworkError: After all retries.
        """
        self._token = None
        resp = await self._send("GET", "auth/accesstoken", None)
        body = self._parse(resp, "GET", "auth/accesstoken")
        token = body.get("accesstoken")
        if not token or not isinstance(token, str):
            raise VfAuthError("no accesstoken in auth/accesstoken response",
                              status=resp.status_code)

        form: dict[str, Any] = {
            "accesstoken": token,
            "username": self._username,
            "password": self._password_md5,
            "appkey": self._appkey,
        }
        if self._cid is not None:
            form["cid"] = str(self._cid)
        resp = await self._send("POST", "auth/signin", form)
        if resp.status_code == 403:
            self._log.error("vf_signin_forbidden", status=403)
            raise VfForbidden(f"403 POST auth/signin: {_truncate(resp.text)}",
                              status=403, body=_truncate(resp.text))
        self._parse(resp, "POST", "auth/signin")
        self._token = token
        self._log.info("vf_signin_ok")

    # --------------------------------------------------------------- flights

    async def list_today(self) -> list[VfFlight]:
        """``POST flight/list/today`` → flights of the current VF day."""
        body = await self._call("POST", "flight/list/today")
        return [VfFlight.model_validate(item) for item in _list_items(body)]

    async def get_flight(self, flid: int) -> VfFlight:
        """``POST flight/get/{flid}`` → one flight (read-before-write)."""
        body = await self._call("POST", f"flight/get/{int(flid)}")
        return VfFlight.model_validate(_single_object(body))

    async def edit_flight(self, flid: int, fields: dict[str, str | int]) -> dict[str, Any]:
        """``PUT flight/edit/{flid}`` with exactly the given fields.

        Args:
            flid: VF flight id.
            fields: Field → value; ints are sent as decimal strings. Nothing
                else is added (partial edit, Kap. 11 item 2).

        Returns:
            Parsed response body.
        """
        form = {k: (str(v) if isinstance(v, int) else v) for k, v in fields.items()}
        body = await self._call("PUT", f"flight/edit/{int(flid)}", form)
        self._log.info("vf_flight_edited", flid=int(flid), fields=sorted(form))
        return body

    async def list_plane(self, callsign: str) -> list[VfFlight]:
        """``POST flight/list/plane`` → flights of one aircraft (fallback read)."""
        body = await self._call("POST", "flight/list/plane", {"callsign": callsign})
        return [VfFlight.model_validate(item) for item in _list_items(body)]

    async def list_modified(self, since: datetime) -> list[VfFlight]:
        """``POST flight/list/modified`` → flights modified since ``since`` (UTC)."""
        body = await self._call("POST", "flight/list/modified", {"modified": to_vf_time(since)})
        return [VfFlight.model_validate(item) for item in _list_items(body)]
