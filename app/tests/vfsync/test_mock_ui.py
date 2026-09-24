"""Browser UI of the VF mock: create/delete flights, observe VF-Sync writes."""

import httpx

from app.vfsync.vf_client.mock import MockVfStore, create_server_app, mock_client


async def _ui(store: MockVfStore) -> httpx.AsyncClient:
    app = create_server_app(store)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ui")


async def test_flight_created_in_ui_is_visible_to_the_api_client():
    store = MockVfStore()
    async with await _ui(store) as ui:
        r = await ui.post("/ui/flights", json={"callsign": "D-1234", "pilotname": "Max",
                                                "starttype": "F"})
        assert r.status_code == 201
        flid = r.json()["flid"]
        page = await ui.get("/")
        assert page.status_code == 200 and "Vereinsflieger-Mock" in page.text

    client = mock_client(store)
    flights = await client.list_today()
    assert [f.flid for f in flights] == [flid]
    assert flights[0].callsign == "D-1234" and flights[0].starttype == "F"
    assert flights[0].is_empty("departuretime")


async def test_state_shows_requests_and_edits_without_credentials():
    store = MockVfStore()
    flid = store.add_flight(callsign="D-1234")
    client = mock_client(store)
    await client.edit_flight(flid, {"departuretime": "2026-09-24 10:00"})

    async with await _ui(store) as ui:
        s = (await ui.get("/ui/state")).json()
    assert s["flights"][0]["departuretime"] == "2026-09-24 10:00"
    assert s["edits"][-1]["flid"] == flid
    assert s["edits"][-1]["fields"] == {"departuretime": "2026-09-24 10:00"}
    methods = [q["method"] for q in s["requests"]]
    assert "PUT" in methods and "GET" in methods
    for q in s["requests"]:
        assert not ({"accesstoken", "password", "appkey"} & set(q["payload"]))
    assert s["signins"] == 1
    assert all(q["ts"] for q in s["requests"])


async def test_validation_delete_and_reset():
    store = MockVfStore()
    async with await _ui(store) as ui:
        assert (await ui.post("/ui/flights", json={"callsign": " "})).status_code == 422
        flid = (await ui.post("/ui/flights", json={"callsign": "D-1"})).json()["flid"]
        assert (await ui.delete(f"/ui/flights/{flid}")).status_code == 200
        assert (await ui.delete(f"/ui/flights/{flid}")).status_code == 404
        await ui.post("/ui/flights", json={"callsign": "D-2"})
        assert (await ui.post("/ui/reset")).json() == {"ok": True}
        s = (await ui.get("/ui/state")).json()
        assert s["flights"] == [] and s["requests"] == [] and s["edits"] == []
