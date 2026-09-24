"""AP-2: scenario hooks and response shapes of the VF mock server."""

import httpx
import pytest

from app.vfsync.vf_client.mock import MockVfStore, create_mock_app

API = "/interface/rest"


@pytest.fixture
def store() -> MockVfStore:
    return MockVfStore()


@pytest.fixture
async def http(store):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mock_app(store)),
                                 base_url="http://vf-mock") as c:
        yield c


async def _login(http: httpx.AsyncClient, store: MockVfStore) -> str:
    tok = (await http.get(f"{API}/auth/accesstoken")).json()["accesstoken"]
    r = await http.post(f"{API}/auth/signin", data={
        "accesstoken": tok, "username": store.username,
        "password": store.password_md5, "appkey": store.appkey,
    })
    assert r.status_code == 200
    return tok


def test_add_flight_defaults_and_ids(store):
    a = store.add_flight(callsign="D-1234")
    b = store.add_flight(callsign="D-5678", landingcount=3)
    assert (a, b) == (1001, 1002)
    rec = store.flight(a)
    assert rec["departuretime"] == "" and rec["arrivaltime"] == ""
    assert rec["towheight"] == "" and rec["towtime"] == ""
    assert rec["landingcount"] == "1"
    assert rec["flid"] == "1001"
    assert store.flight(b)["landingcount"] == "3"
    assert all(isinstance(v, str) for v in rec.values())


async def test_unauthenticated_requests_are_401(http, store):
    store.add_flight(callsign="D-1234")
    r = await http.post(f"{API}/flight/list/today")
    assert r.status_code == 401
    assert r.json() == {"error": "not signed in", "httpstatuscode": 401}
    store.require_signin(False)
    r = await http.post(f"{API}/flight/list/today")
    assert r.status_code == 200
    store.require_signin()
    r = await http.post(f"{API}/flight/list/today")
    assert r.status_code == 401


async def test_list_shape_numeric_keys_plus_httpstatuscode(http, store):
    store.add_flight(callsign="D-1234")
    store.add_flight(callsign="D-5678")
    tok = await _login(http, store)
    body = (await http.post(f"{API}/flight/list/today", data={"accesstoken": tok})).json()
    assert set(body) == {"0", "1", "httpstatuscode"}
    assert body["httpstatuscode"] == 200
    assert body["0"]["callsign"] == "D-1234"
    assert body["1"]["flid"] == "1002"


async def test_get_shape_single_object(http, store):
    flid = store.add_flight(callsign="D-1234", starttype="3")
    tok = await _login(http, store)
    body = (await http.post(f"{API}/flight/get/{flid}", data={"accesstoken": tok})).json()
    assert body["flid"] == str(flid)
    assert body["starttype"] == "3"
    assert body["httpstatuscode"] == 200
    r = await http.post(f"{API}/flight/get/4242", data={"accesstoken": tok})
    assert r.status_code == 404


async def test_edit_validation(http, store):
    flid = store.add_flight(callsign="D-1234")
    tok = await _login(http, store)
    url = f"{API}/flight/edit/{flid}"

    r = await http.put(url, data={"accesstoken": tok, "nosuchfield": "x"})
    assert r.status_code == 400 and "nosuchfield" in r.json()["error"]

    r = await http.put(url, data={"accesstoken": tok, "departuretime": "2026-09-24 10:15:00"})
    assert r.status_code == 400

    r = await http.put(url, data={"accesstoken": tok, "arrivaltime": "2026-13-40 10:15"})
    assert r.status_code == 400

    r = await http.put(url, data={"accesstoken": tok, "towheight": "abc"})
    assert r.status_code == 400

    r = await http.put(url, data={"accesstoken": tok, "starttype": "Q"})
    assert r.status_code == 400

    assert store.edits == []

    r = await http.put(url, data={"accesstoken": tok, "departuretime": "2026-09-24 10:15",
                                  "starttype": "F", "landingcount": "2"})
    assert r.status_code == 200
    assert r.json()["httpstatuscode"] == 200
    assert store.edits == [(flid, {"departuretime": "2026-09-24 10:15", "starttype": "F",
                                   "landingcount": "2"})]

    # decreasing landingcount is NOT rejected by the mock (writer's job)
    r = await http.put(url, data={"accesstoken": tok, "landingcount": "1"})
    assert r.status_code == 200
    assert store.flight(flid)["landingcount"] == "1"

    r = await http.put(f"{API}/flight/edit/4242", data={"accesstoken": tok, "towheight": "1"})
    assert r.status_code == 404


async def test_request_log_strips_secrets(http, store):
    flid = store.add_flight(callsign="D-1234")
    tok = await _login(http, store)
    await http.put(f"{API}/flight/edit/{flid}", data={"accesstoken": tok, "towheight": "450"})
    for _method, _path, payload in store.requests:
        assert "accesstoken" not in payload
        assert "password" not in payload
        assert "appkey" not in payload
    assert store.requests[1][2] == {"username": store.username}
    assert store.requests[-1] == ("PUT", f"{API}/flight/edit/{flid}", {"towheight": "450"})


async def test_fail_next_counts_down_and_matches_path(http, store):
    store.add_flight(callsign="D-1234")
    tok = await _login(http, store)
    store.fail_next("flight/list/today", 503, times=2)
    for _ in range(2):
        r = await http.post(f"{API}/flight/list/today", data={"accesstoken": tok})
        assert r.status_code == 503
        assert r.json()["httpstatuscode"] == 503
    r = await http.post(f"{API}/flight/list/today", data={"accesstoken": tok})
    assert r.status_code == 200
    # other paths unaffected while a failure is pending
    store.fail_next("flight/get", 500)
    r = await http.post(f"{API}/flight/list/today", data={"accesstoken": tok})
    assert r.status_code == 200
    r = await http.post(f"{API}/flight/get/1001", data={"accesstoken": tok})
    assert r.status_code == 500


async def test_invalidate_token_forces_401_until_resignin(http, store):
    store.add_flight(callsign="D-1234")
    tok = await _login(http, store)
    store.invalidate_token()
    r = await http.post(f"{API}/flight/list/today", data={"accesstoken": tok})
    assert r.status_code == 401
    tok2 = await _login(http, store)
    r = await http.post(f"{API}/flight/list/today", data={"accesstoken": tok2})
    assert r.status_code == 200
    assert store.signins == 2


async def test_signin_rejects_unknown_token_and_bad_credentials(http, store):
    r = await http.post(f"{API}/auth/signin", data={
        "accesstoken": "made-up", "username": store.username,
        "password": store.password_md5, "appkey": store.appkey})
    assert r.status_code == 401
    tok = (await http.get(f"{API}/auth/accesstoken")).json()["accesstoken"]
    r = await http.post(f"{API}/auth/signin", data={
        "accesstoken": tok, "username": store.username,
        "password": "wrong", "appkey": store.appkey})
    assert r.status_code == 403
    assert store.signins == 0


async def test_mutate_after_get_applies_once(http, store):
    flid = store.add_flight(callsign="D-1234")
    tok = await _login(http, store)
    store.mutate_after_get(flid, {"towheight": 600})
    first = (await http.post(f"{API}/flight/get/{flid}", data={"accesstoken": tok})).json()
    assert first["towheight"] == ""
    assert store.flight(flid)["towheight"] == "600"
    second = (await http.post(f"{API}/flight/get/{flid}", data={"accesstoken": tok})).json()
    assert second["towheight"] == "600"
    third = (await http.post(f"{API}/flight/get/{flid}", data={"accesstoken": tok})).json()
    assert third["towheight"] == "600"


async def test_order_of(http, store):
    flid = store.add_flight(callsign="D-1234")
    other = store.add_flight(callsign="D-5678")
    tok = await _login(http, store)
    await http.post(f"{API}/flight/get/{flid}", data={"accesstoken": tok})
    await http.post(f"{API}/flight/get/{other}", data={"accesstoken": tok})
    await http.put(f"{API}/flight/edit/{flid}", data={"accesstoken": tok, "towheight": "1"})
    await http.post(f"{API}/flight/get/{flid}", data={"accesstoken": tok})
    assert store.order_of(flid) == ["get", "edit", "get"]
    assert store.order_of(other) == ["get"]
    store.reset_log()
    assert store.order_of(flid) == []


async def test_list_plane_and_modified_filters(http, store):
    a = store.add_flight(callsign="D-1234")
    store.add_flight(callsign="D-5678")
    tok = await _login(http, store)
    body = (await http.post(f"{API}/flight/list/plane",
                            data={"accesstoken": tok, "callsign": "d1234"})).json()
    assert set(body) == {"0", "httpstatuscode"} and body["0"]["flid"] == str(a)
    body = (await http.post(f"{API}/flight/list/modified",
                            data={"accesstoken": tok, "modified": "2999-01-01 00:00"})).json()
    assert set(body) == {"httpstatuscode"}
    r = await http.post(f"{API}/flight/list/modified", data={"accesstoken": tok, "modified": "x"})
    assert r.status_code == 400
