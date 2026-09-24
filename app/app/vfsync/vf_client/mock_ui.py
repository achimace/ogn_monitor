"""Browser UI for the Vereinsflieger mock (manual testing).

Mounted on the mock server (``python -m app.vfsync.vf_client.mock``):

- ``/``              single-page UI (plain HTML/JS, auto refresh every 3 s)
- ``/ui/state``      JSON: flights, request log, edits, accepted credentials
- ``/ui/flights``    POST  create a flight like a pilot would in VF
- ``/ui/flights/{flid}``  DELETE
- ``/ui/reset``      POST  clear flights and logs

The UI never touches the VF REST endpoints, so what the request log shows
is exactly what VF-Sync sent.
"""

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.vfsync.vf_client.mock import FLIGHT_FIELDS, MockVfStore

STARTTYPES = {"": "(leer)", "F": "F-Schlepp (F)", "W": "Winde (W)", "E": "Eigenstart (E)",
              "3": "F-Schlepp (3)", "5": "Winde (5)", "1": "Eigen/Motor (1)"}


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def attach_ui(app: FastAPI, store: MockVfStore) -> None:
    """Add the UI routes to a mock app."""

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return PAGE

    @app.get("/ui/state")
    async def state() -> dict[str, Any]:
        flights = []
        for flid, rec in sorted(store.flights.items()):
            flights.append({"flid": flid, **{f: rec.get(f, "") for f in FLIGHT_FIELDS},
                            "modified": _iso(store.modified.get(flid))})
        requests = [
            {"i": i, "ts": _iso(ts), "method": m, "path": p, "payload": payload}
            for i, ((m, p, payload), ts) in enumerate(zip(store.requests, store.request_times))
        ]
        edits = [
            {"ts": _iso(ts), "flid": flid, "fields": fields}
            for (flid, fields), ts in zip(store.edits, store.edit_times)
        ]
        return {
            "flights": flights,
            "requests": requests[-200:],
            "edits": edits[-100:],
            "signins": store.signins,
            "credentials": {"username": store.username, "appkey": store.appkey,
                            "cid": store.cid, "password_hint": "Klartext siehe VF_MOCK_PASSWORD"},
            "starttypes": STARTTYPES,
            "now": _iso(datetime.now(timezone.utc)),
        }

    @app.post("/ui/flights")
    async def create_flight(request: Request) -> JSONResponse:
        body = await request.json()
        callsign = str(body.get("callsign", "")).strip()
        if not callsign:
            return JSONResponse({"error": "Kennzeichen fehlt"}, status_code=422)
        fields = {
            "callsign": callsign,
            "pilotname": str(body.get("pilotname", "")).strip(),
            "attendantname": str(body.get("attendantname", "")).strip(),
            "starttype": str(body.get("starttype", "")).strip(),
            "comment": str(body.get("comment", "")).strip(),
        }
        for opt in ("departuretime", "arrivaltime", "towheight", "towtime", "landingcount"):
            if body.get(opt) not in (None, ""):
                fields[opt] = str(body[opt]).strip()
        flid = store.add_flight(**fields)
        return JSONResponse({**store.flight(flid), "flid": flid}, status_code=201)

    @app.delete("/ui/flights/{flid}")
    async def delete_flight(flid: int) -> JSONResponse:
        if flid not in store.flights:
            return JSONResponse({"error": "unbekannter Flug"}, status_code=404)
        del store.flights[flid]
        store.modified.pop(flid, None)
        return JSONResponse({"deleted": flid})

    @app.post("/ui/reset")
    async def reset() -> dict[str, Any]:
        store.flights.clear()
        store.modified.clear()
        store.reset_log()
        store.request_times.clear()
        store.edit_times.clear()
        return {"ok": True}


PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Vereinsflieger-Mock</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: dark; --bg:#0f172a; --card:#1e293b; --line:#334155; --txt:#e2e8f0;
          --muted:#94a3b8; --ok:#22c55e; --warn:#f59e0b; --err:#ef4444; --acc:#38bdf8; }
  body { margin:0; font: 14px/1.4 system-ui, sans-serif; background:var(--bg); color:var(--txt); }
  header { padding:12px 20px; border-bottom:1px solid var(--line); display:flex; gap:16px;
           align-items:center; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; }
  header .muted { color:var(--muted); font-size:12px; }
  main { padding:16px 20px; display:grid; gap:16px; grid-template-columns: 1fr; }
  @media (min-width:1100px) { main { grid-template-columns: 3fr 2fr; } }
  section { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 14px; }
  h2 { font-size:14px; margin:0 0 10px; color:var(--acc); text-transform:uppercase; letter-spacing:.04em; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:5px 6px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--muted); font-weight:600; }
  td.empty { color:#475569; }
  td.new { background:#14532d55; }
  form { display:flex; gap:8px; flex-wrap:wrap; align-items:end; margin-bottom:12px; }
  label { display:flex; flex-direction:column; font-size:12px; color:var(--muted); gap:3px; }
  input, select { background:#0b1220; color:var(--txt); border:1px solid var(--line); border-radius:6px; padding:6px 8px; }
  button { background:var(--acc); color:#0b1220; border:0; border-radius:6px; padding:7px 12px; font-weight:600; cursor:pointer; }
  button.ghost { background:transparent; color:var(--muted); border:1px solid var(--line); }
  button.danger { background:transparent; color:var(--err); border:1px solid var(--err); }
  code { font-family: ui-monospace, monospace; font-size:12px; }
  .log { max-height:520px; overflow:auto; }
  .m-PUT { color:var(--warn); font-weight:700; } .m-POST { color:var(--acc); } .m-GET { color:var(--muted); }
  .pill { display:inline-block; padding:1px 7px; border-radius:999px; font-size:11px; border:1px solid var(--line); }
  #msg { color:var(--err); min-height:18px; }
</style>
</head>
<body>
<header>
  <h1>Vereinsflieger-Mock</h1>
  <span class="muted">Login-Daten für VF-Sync: Benutzer <code id="c-user"></code>,
    AppKey <code id="c-key"></code>, Passwort: <code>VF_MOCK_PASSWORD</code> (Default <code>mock-password</code>)</span>
  <span class="muted">Signins: <b id="c-signins">0</b> · <span id="c-now"></span></span>
  <button class="ghost" onclick="reset()">Alles zurücksetzen</button>
</header>
<main>
  <section>
    <h2>Flüge (wie vom Piloten in VF angelegt)</h2>
    <form onsubmit="return createFlight(event)">
      <label>Kennzeichen<input id="f-callsign" placeholder="D-1234" required></label>
      <label>Pilot<input id="f-pilot" placeholder="Max Muster"></label>
      <label>Begleiter<input id="f-attendant"></label>
      <label>Startart<select id="f-starttype"></select></label>
      <label>Startzeit (optional)<input id="f-dep" placeholder="YYYY-MM-DD HH:MM"></label>
      <label>Landungen<input id="f-lc" placeholder="1" size="3"></label>
      <button type="submit">Flug anlegen</button>
    </form>
    <div id="msg"></div>
    <table>
      <thead><tr><th>flid</th><th>Kennz.</th><th>Pilot</th><th>Startart</th><th>Start</th><th>Landung</th>
        <th>Schlepphöhe</th><th>Schleppzeit</th><th>Landungen</th><th>geändert</th><th></th></tr></thead>
      <tbody id="flights"></tbody>
    </table>
  </section>
  <section>
    <h2>Schreibzugriffe von VF-Sync (flight/edit)</h2>
    <div class="log"><table><thead><tr><th>Zeit</th><th>flid</th><th>gesendete Felder</th></tr></thead>
      <tbody id="edits"></tbody></table></div>
  </section>
  <section style="grid-column: 1 / -1">
    <h2>Request-Log (alle API-Aufrufe, ohne Zugangsdaten)</h2>
    <div class="log"><table><thead><tr><th>#</th><th>Zeit</th><th>Methode</th><th>Pfad</th><th>Payload</th></tr></thead>
      <tbody id="requests"></tbody></table></div>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
let seenEdits = 0;
const fmt = (iso) => iso ? new Date(iso).toLocaleTimeString('de-DE') : '';
const cell = (v, isNew) => `<td class="${v === '' ? 'empty' : ''}${isNew ? ' new' : ''}">${v === '' ? '–' : esc(v)}</td>`;
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const kv = (o) => Object.entries(o || {}).map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join(' ');

async function refresh() {
  const r = await fetch('/ui/state'); const s = await r.json();
  $('c-user').textContent = s.credentials.username; $('c-key').textContent = s.credentials.appkey;
  $('c-signins').textContent = s.signins; $('c-now').textContent = 'Stand ' + fmt(s.now);
  const sel = $('f-starttype');
  if (!sel.options.length) for (const [v, label] of Object.entries(s.starttypes)) sel.add(new Option(label, v));
  const recent = new Set(s.edits.slice(-3).map(e => e.flid));
  $('flights').innerHTML = s.flights.map(f => `<tr>
    <td>${f.flid}</td>${cell(f.callsign)}${cell(f.pilotname)}${cell(f.starttype)}
    ${cell(f.departuretime, recent.has(f.flid))}${cell(f.arrivaltime, recent.has(f.flid))}
    ${cell(f.towheight, recent.has(f.flid))}${cell(f.towtime, recent.has(f.flid))}${cell(f.landingcount)}
    <td>${fmt(f.modified)}</td>
    <td><button class="danger" onclick="del(${f.flid})">löschen</button></td></tr>`).join('')
    || '<tr><td colspan="11" class="empty">Noch keine Flüge – oben anlegen.</td></tr>';
  $('edits').innerHTML = s.edits.slice().reverse().map(e =>
    `<tr><td>${fmt(e.ts)}</td><td>${e.flid}</td><td>${kv(e.fields)}</td></tr>`).join('')
    || '<tr><td colspan="3" class="empty">Noch nichts geschrieben.</td></tr>';
  $('requests').innerHTML = s.requests.slice().reverse().map(q =>
    `<tr><td>${q.i + 1}</td><td>${fmt(q.ts)}</td><td class="m-${q.method}">${q.method}</td>
     <td><code>${esc(q.path.replace('/interface/rest/', ''))}</code></td><td>${kv(q.payload)}</td></tr>`).join('')
    || '<tr><td colspan="5" class="empty">Noch keine Aufrufe.</td></tr>';
}
async function createFlight(ev) {
  ev.preventDefault(); $('msg').textContent = '';
  const body = { callsign: $('f-callsign').value, pilotname: $('f-pilot').value,
                 attendantname: $('f-attendant').value, starttype: $('f-starttype').value,
                 departuretime: $('f-dep').value, landingcount: $('f-lc').value };
  const r = await fetch('/ui/flights', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
  if (!r.ok) { $('msg').textContent = (await r.json()).error || r.statusText; return false; }
  $('f-callsign').value = ''; $('f-dep').value = ''; $('f-lc').value = '';
  await refresh(); return false;
}
async function del(flid) { await fetch('/ui/flights/' + flid, { method: 'DELETE' }); refresh(); }
async function reset() { if (confirm('Alle Flüge und Logs löschen?')) { await fetch('/ui/reset', { method: 'POST' }); refresh(); } }
refresh(); setInterval(refresh, 3000);
</script>
</body>
</html>
"""
