"""AP-1/AP-2: VfClient against the in-process mock (no network)."""

from datetime import datetime, timezone

import httpx
import pytest

from app.vfsync.vf_client import (
    VfAuthError,
    VfBadRequest,
    VfClient,
    VfError,
    VfForbidden,
    VfNetworkError,
    VfServerError,
)
from app.vfsync.vf_client.client import API_PREFIX, DEFAULT_BACKOFF_S
from app.vfsync.vf_client.mock import MockVfStore, mock_client

UTC = timezone.utc


class Counter:
    """on_request hook that counts calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


@pytest.fixture
def store() -> MockVfStore:
    s = MockVfStore()
    s.add_flight(callsign="D-1234", pilotname="Anna", starttype="3")
    s.add_flight(callsign="D-5678", pilotname="Ben", starttype="5",
                 departuretime="2026-09-24 08:00", arrivaltime="2026-09-24 08:20",
                 landingcount="2")
    return s


@pytest.fixture
def counter() -> Counter:
    return Counter()


@pytest.fixture
async def client(store, counter):
    c = mock_client(store, on_request=counter)
    yield c
    await c.close()


def _paths(store: MockVfStore, contains: str) -> list[tuple[str, str, dict]]:
    return [r for r in store.requests if contains in r[1]]


# ---------------------------------------------------------------- signin

async def test_signin_flow_and_token_only_in_memory(store, client, counter):
    await client.signin()
    assert client.signed_in
    assert store.signins == 1
    assert counter.calls == 2
    methods = [(m, p) for m, p, _ in store.requests]
    assert methods == [("GET", f"/{API_PREFIX}/auth/accesstoken"),
                       ("POST", f"/{API_PREFIX}/auth/signin")]
    # mock strips secrets, but the recorded payload proves username/cid shape
    signin_payload = store.requests[1][2]
    assert signin_payload == {"username": store.username}
    assert "accesstoken" not in signin_payload
    assert "password" not in signin_payload
    assert "appkey" not in signin_payload


async def test_signin_sends_cid_when_configured(counter):
    s = MockVfStore(cid=77)
    c = mock_client(s, on_request=counter)
    try:
        await c.signin()
        assert store_payload(s) == {"username": s.username, "cid": "77"}
    finally:
        await c.close()


def store_payload(s: MockVfStore) -> dict:
    return s.requests[1][2]


async def test_signin_403_raises_forbidden(store, client, counter):
    store.fail_next("auth/signin", 403)
    with pytest.raises(VfForbidden) as exc:
        await client.signin()
    assert exc.value.status == 403
    assert not client.signed_in
    assert counter.calls == 2       # failed call counted too


async def test_wrong_credentials_give_forbidden(store):
    c = mock_client(store, password_md5="0" * 32)
    try:
        with pytest.raises(VfForbidden):
            await c.list_today()
    finally:
        await c.close()


async def test_implicit_signin_before_first_call(store, client, counter):
    flights = await client.list_today()
    assert len(flights) == 2
    assert store.signins == 1
    assert counter.calls == 3       # accesstoken + signin + list


# ------------------------------------------------------------ list / get

async def test_list_today_parses_numeric_keys(store, client):
    flights = await client.list_today()
    by_callsign = {f.callsign: f for f in flights}
    assert set(by_callsign) == {"D-1234", "D-5678"}
    a = by_callsign["D-1234"]
    assert isinstance(a.flid, int)
    assert a.is_empty("departuretime") and a.is_empty("towheight")
    assert a.starttype == "3"
    b = by_callsign["D-5678"]
    assert b.departure_dt == datetime(2026, 9, 24, 8, 0, tzinfo=UTC)
    assert b.landingcount == 2


async def test_get_flight_single_object(store, client):
    flid = next(iter(store.flights))
    f = await client.get_flight(flid)
    assert f.flid == flid
    assert f.callsign == "D-1234"
    assert "httpstatuscode" not in f.raw()


async def test_get_unknown_flid_is_plain_vferror(client):
    with pytest.raises(VfError) as exc:
        await client.get_flight(999999)
    assert exc.value.status == 404
    assert not isinstance(exc.value, (VfBadRequest, VfAuthError, VfForbidden, VfServerError))


async def test_list_plane_and_modified(store, client):
    only = await client.list_plane("d1234")
    assert [f.callsign for f in only] == ["D-1234"]
    recent = await client.list_modified(datetime(2000, 1, 1, tzinfo=UTC))
    assert len(recent) == 2
    none = await client.list_modified(datetime(2999, 1, 1, tzinfo=UTC))
    assert none == []
    payload = _paths(store, "list/plane")[0][2]
    assert payload == {"callsign": "d1234"}
    payload = _paths(store, "list/modified")[0][2]
    assert payload == {"modified": "2000-01-01 00:00"}


# ------------------------------------------------------------------ edit

async def test_edit_sends_only_given_fields(store, client):
    flid = next(iter(store.flights))
    body = await client.edit_flight(flid, {"departuretime": "2026-09-24 10:15", "towheight": 450})
    assert body["httpstatuscode"] == 200
    assert store.edits == [(flid, {"departuretime": "2026-09-24 10:15", "towheight": "450"})]
    method, path, payload = _paths(store, "flight/edit")[0]
    assert method == "PUT"
    assert payload == {"departuretime": "2026-09-24 10:15", "towheight": "450"}
    assert "accesstoken" not in payload
    rec = store.flight(flid)
    assert rec["departuretime"] == "2026-09-24 10:15"
    assert rec["towheight"] == "450"
    assert rec["arrivaltime"] == ""          # untouched
    assert rec["landingcount"] == "1"        # untouched


async def test_edit_400_is_not_retried(store, client, counter):
    flid = next(iter(store.flights))
    await client.signin()
    counter.calls = 0
    with pytest.raises(VfBadRequest) as exc:
        await client.edit_flight(flid, {"bogusfield": "1"})
    assert exc.value.status == 400
    assert "bogusfield" in str(exc.value)
    assert "bogusfield" in (exc.value.body or "")
    assert counter.calls == 1
    assert len(_paths(store, "flight/edit")) == 1
    assert store.edits == []


async def test_edit_malformed_time_is_400(store, client):
    flid = next(iter(store.flights))
    with pytest.raises(VfBadRequest):
        await client.edit_flight(flid, {"arrivaltime": "24.09.2026 10:15"})
    assert store.edits == []


async def test_injected_400_is_not_retried(store, client, counter):
    flid = next(iter(store.flights))
    await client.signin()
    counter.calls = 0
    store.fail_next("flight/edit", 400)
    with pytest.raises(VfBadRequest):
        await client.edit_flight(flid, {"towheight": 300})
    assert counter.calls == 1


# ------------------------------------------------------------- 401 / auth

async def test_401_triggers_exactly_one_resignin(store, client, counter):
    flid = next(iter(store.flights))
    await client.signin()
    counter.calls = 0
    store.invalidate_token()
    f = await client.get_flight(flid)
    assert f.flid == flid
    assert store.signins == 2
    assert counter.calls == 4       # 401 + accesstoken + signin + retry
    assert store.order_of(flid) == ["get", "get"]


async def test_persistent_401_raises_auth_error(store, client, counter):
    flid = next(iter(store.flights))
    await client.signin()
    counter.calls = 0
    store.fail_next("flight/get", 401, times=5)
    with pytest.raises(VfAuthError):
        await client.get_flight(flid)
    assert store.signins == 2                                # exactly one re-signin
    assert len(_paths(store, "flight/get")) == 2            # original + one retry
    assert counter.calls == 4


# ------------------------------------------------------- 5xx / network

async def test_5xx_is_retried_three_times_then_server_error(store, client, counter):
    await client.signin()
    counter.calls = 0
    store.fail_next("flight/list/today", 503, times=10)
    with pytest.raises(VfServerError) as exc:
        await client.list_today()
    assert exc.value.status == 503
    assert len(_paths(store, "list/today")) == 3
    assert counter.calls == 3


async def test_5xx_recovers_within_retry_budget(store, client, counter):
    await client.signin()
    counter.calls = 0
    store.fail_next("flight/list/today", 500, times=2)
    flights = await client.list_today()
    assert len(flights) == 2
    assert counter.calls == 3


async def test_backoff_schedule_is_used(store):
    delays: list[float] = []

    async def fake_sleep(d: float) -> None:
        delays.append(d)

    c = mock_client(store, sleep=fake_sleep)
    try:
        store.fail_next("auth/accesstoken", 502, times=10)
        with pytest.raises(VfServerError):
            await c.signin()
    finally:
        await c.close()
    assert delays == list(DEFAULT_BACKOFF_S[:2])
    assert DEFAULT_BACKOFF_S == (0.5, 1.0, 2.0)


@pytest.mark.parametrize("exc_factory", [
    lambda req: httpx.ConnectError("refused", request=req),
    lambda req: httpx.ReadTimeout("slow", request=req),
])
async def test_network_errors_become_vfnetworkerror(exc_factory):
    counter = Counter()
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        raise exc_factory(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = VfClient("https://vf.invalid", "u", "0" * 32, "k", http=http,
                 on_request=counter, backoff_s=(0, 0, 0))
    try:
        with pytest.raises(VfNetworkError):
            await c.list_today()
    finally:
        await c.close()
    assert len(sent) == 3
    assert counter.calls == 3
    assert all(str(r.url).startswith("https://vf.invalid/interface/rest/") for r in sent)


# -------------------------------------------------------------- misc

async def test_on_request_exception_aborts_before_sending(store):
    class Budget(Exception):
        pass

    async def deny() -> None:
        raise Budget()

    c = mock_client(store, on_request=deny)
    try:
        with pytest.raises(Budget):
            await c.list_today()
    finally:
        await c.close()
    assert store.requests == []


async def test_mutate_after_get_visible_on_next_get(store, client):
    flid = next(iter(store.flights))
    store.mutate_after_get(flid, {"arrivaltime": "2026-09-24 11:00"})
    first = await client.get_flight(flid)
    assert first.is_empty("arrivaltime")
    second = await client.get_flight(flid)
    assert second.arrival_dt == datetime(2026, 9, 24, 11, 0, tzinfo=UTC)
    assert store.order_of(flid) == ["get", "get"]


def test_base_url_prefix_not_doubled():
    c1 = VfClient("https://www.vereinsflieger.de/", "u", "p", "k", http=httpx.AsyncClient())
    c2 = VfClient("https://www.vereinsflieger.de/interface/rest", "u", "p", "k",
                  http=httpx.AsyncClient())
    assert c1._url("flight/get/1") == "https://www.vereinsflieger.de/interface/rest/flight/get/1"
    assert c2._url("flight/get/1") == c1._url("flight/get/1")


def test_client_never_disables_tls_verification():
    import inspect
    sig = inspect.signature(VfClient.__init__)
    assert "verify" not in sig.parameters
    src = inspect.getsource(VfClient.__init__)
    assert "verify=" not in src
