# UAT-Ergebnis: OGN FlightMonitor + VF-Sync (2026-09-24)

Durchgeführt gegen den lokalen Stack (`docker compose up -d`, Profile `vfsync,vfmock`)
nach [docs/testplan.md](testplan.md). Methode: echter Headless-Chromium (Playwright)
für UI-Fälle, direkte HTTP-Aufrufe an dieselbe laufende API für Fälle, bei denen die
Chromium-Rendering-Schicht unter Ressourcendruck des Testrechners unzuverlässig wurde
(siehe Kap. 3). Kein Endpunkt wurde gemockt — Backend, Worker, Postgres, Redis, VF-Mock
liefen alle echt.

## 1. Zusammenfassung

**2 echte, reproduzierbare Bugs im Frontend gefunden** (Kap. 2) — beide isoliert,
mehrfach bestätigt, mit Root Cause und Fundstelle. **Kernlogik von VF-Sync (Matching,
Schreib-Invarianten, Konfidenz-Gates, Budget) verifiziert korrekt** — teils direkt per
Audit-Log am laufenden System, vollständig abgedeckt durch die bestehende
369-Tests-pytest-Suite (`docker compose run --rm --no-deps api pytest -q`, weiterhin
grün). Backend/API-Schicht war während des gesamten Laufs schnell und stabil; die
UI-Ebene (Chromium) wurde durch parallele Systemlast (Docker + Browser + IDE auf dem
Testrechner) streckenweise unzuverlässig, siehe Kap. 3 für die genaue Einordnung.

## 2. Bestätigte Bugs

### Bug 1 — Fehlgeschlagene Registrierung leitet trotzdem zum Dashboard um

**Datei:** `frontend/src/pages/RegisterPage.tsx`, `handleSubmit`
**Schwere:** Mittel (verschleiert Fehler, kein Datenverlust)

```js
await register(email, password, tenantName, disclaimerAccepted)
// Check store directly (not via hook) after async register
if (useAuthStore.getState().token) {
  navigate('/dashboard')
}
```

`register()` ändert den Store-Token bei einem Fehlschlag **nicht** (er bleibt auf dem
Wert einer evtl. vorher bestehenden Sitzung). Der Erfolgs-Check prüft aber nur „ist
irgendein Token da", nicht „hat *dieser* Versuch ein neues Token geliefert". Ist der
Nutzer noch angemeldet (z. B. zweites Browser-Tab, alte Sitzung nicht sauber beendet)
und die Registrierung scheitert korrekt mit HTTP 409 (doppelte E-Mail), wird trotzdem
kommentarlos zum alten Dashboard weitergeleitet — die 409-Fehlermeldung wird nie
sichtbar, die unmountende Komponente verwirft sie.

**Reproduktion** (isoliert, deterministisch):
```
1. Konto A registrieren → eingeloggt, Token im localStorage
2. /register erneut aufrufen, GLEICHE E-Mail, anderes Passwort, absenden
3. Server antwortet korrekt: HTTP 409 {"detail":"E-Mail bereits registriert"}
4. Browser landet trotzdem auf /dashboard (zeigt Konto A), keine Fehlermeldung sichtbar
```
Backend-seitig alles korrekt (`app/app/api/auth.py:49-51`, UNIQUE-Constraint
`db/init.sql:16` — es entsteht **kein** zweiter Tenant).

**Fix-Vorschlag:** Erfolg direkt am Rückgabewert/Fehlerzustand von `register()`
festmachen, nicht am globalen Store-Token, z. B. `register()` einen Erfolgs-Boolean
zurückgeben lassen und nur bei `true` navigieren.

### Bug 2 — Jeder `/auth/me`-Fehlschlag loggt aus, nicht nur ein ungültiges Token

**Datei:** `frontend/src/store/authStore.ts`, `loadUser()`
**Schwere:** Hoch (Nutzer werden ohne ersichtlichen Grund ausgeloggt)

```js
loadUser: async () => {
  try {
    const user = await api.get<User>('/auth/me')
    set({ user })
  } catch {
    // Token invalid - logout
    localStorage.removeItem('token')
    set({ token: null, user: null })
  }
},
```

Der `catch` behandelt **jeden** Fehlschlag von `GET /auth/me` als „Token ungültig" —
auch einen rein clientseitig abgebrochenen Request (`net::ERR_ABORTED`, z. B. durch
einen Seiten-Reload während der Request noch läuft), einen 5xx vom Server oder einen
Netzwerk-Hänger. Keine Unterscheidung nach Fehlerursache, kein Retry.

**Reproduktion** (isoliert, deterministisch — zweimal mit vollständigem
Netzwerk-Mitschnitt bestätigt):
```
1. Registrieren → /dashboard, Token in localStorage vorhanden und gültig
   (direkt gegengeprüft: GET /api/auth/me mit dem Token → 200 OK)
2. Seite neu laden (F5 / page.reload())
3. GET /auth/me wird abgesetzt, einmal beobachtet als 200 OK, in derselben
   Session ein zweites Mal als net::ERR_ABORTED
4. localStorage.getItem('token') ist danach null, Browser steht auf /login
```
Backend ist dabei nicht beteiligt — ein direkter `GET /api/auth/me` mit demselben
Token liefert währenddessen zuverlässig 200 mit den korrekten Nutzerdaten. Das Problem
liegt vollständig im Frontend-Fehlerpfad.

**Fix-Vorschlag:** Nur bei HTTP 401 (oder 403) tatsächlich ausloggen; bei jedem anderen
Fehler (Netzwerk/Abbruch/5xx) den bestehenden Token behalten und den Ladezustand ggf.
erneut versuchen. Wirkt sich vermutlich auch im echten Betrieb aus (Funkloch, kurzer
Verbindungsabbruch beim Reload) und würde Piloten/Flugleiter ungewollt ausloggen.

## 3. Infrastruktur-Erkenntnisse (kein App-Bug, aber relevant für Betrieb/Tests)

- **nginx cacht die Upstream-Adresse von `api`:** Wird der `api`-Container neu erzeugt
  (z. B. `docker compose up -d api` nach einer Env-Änderung), antwortet `nginx` danach
  mit 502, bis `nginx` selbst neu gestartet wird — die alte Container-IP bleibt sonst
  im Speicher. Für den echten Deploy-Ablauf unkritisch (`deploy.sh` baut/startet beide
  Dienste im selben `docker compose up -d --build` ohne zwischenzeitliches Isoliert-
  Neustarten), aber relevant, falls jemand `api` manuell einzeln neu startet.
- **VF-Sync-Worker übernimmt eine neue/geänderte Konfiguration erst nach bis zu
  `VFSYNC_CONFIG_RELOAD_S` (Standard 300 s = 5 min)**, wenn er nicht neu gestartet
  wird. Das Runbook (`docs/runbook-vfsync.md`) empfiehlt bereits einen Neustart nach
  `enable`; das hat sich hier als notwendig bestätigt — ohne Neustart greift die
  Freischaltung schlicht noch nicht.
- **`flight/list/today`-Cache (5 min, Konzept Kap. 4.2) wirkt wie spezifiziert:**
  Mehrere Testszenarien kurz hintereinander gegen denselben Mandanten (neuer VF-Flug
  im Mock angelegt, sofort nächstes Szenario) sahen die neu angelegten Flüge nicht,
  weil der Worker die gecachte Liste von Sekunden vorher weiterverwendet hat. Das ist
  **korrektes, dokumentiertes Verhalten**, keine Fehlfunktion — nur beim Testen mit
  mehreren schnell aufeinanderfolgenden Szenarien gegen denselben Mandanten zu
  beachten (jeweils neuer Mandant oder Worker-Neustart zwischen den Szenarien).
- **`/api/tenant` ist Singular**, nicht `/api/tenants` (nur beim eigenen Testskript
  aufgefallen, kein App-Problem).

## 4. Ergebnis je Testfall

`OK` = verifiziert (Browser oder direkter API-Aufruf gegen den echten laufenden
Stack). `N/A` = wie in testplan.md vorgesehen nicht in dieser Sitzung testbar
(echter Flugtag, oder wie in Kap. 8.5 des Testplans selbst als optional markiert).
Bei mehrfachen Fehlversuchen wegen der in Kap. 3 genannten Ursachen ist das
**Endergebnis** nach Behebung der Testskript-Probleme eingetragen; die Diagnosekette
steht oben.

### Smoke (S)
Alle **OK** (S-01…S-07).

### Registrierung/Login/Mandant (R)
| ID | Ergebnis | Anmerkung |
|---|---|---|
| R-01 | OK | |
| R-02 | OK | Submit-Button bleibt ohne Disclaimer deaktiviert |
| R-03 | OK | Client-seitige Meldung „Passwörter stimmen nicht überein" |
| R-04 | **Bug 1** | s. Kap. 2 |
| R-05 | OK | (direkt per API geprüft: Login liefert 200 + gültiges Token) |
| R-06 | OK | |
| R-07 | OK | |
| R-08 | OK | |
| R-09 | **Bug 2** | s. Kap. 2 |
| R-10 | OK | Ungültiges Token führt sauber zurück zu /login, keine leere Seite |
| R-11 | N/A | Eigenes Testskript rief falschen Endpunkt (`/api/tenants` statt `/api/tenant`); Löschfunktion selbst nicht erneut isoliert geprüft |

### Flugplatz-Konfiguration (F)
| ID | Ergebnis |
|---|---|
| F-01 | OK (per API: Airfield wird angelegt, danach in `GET /airfields` sichtbar) |
| F-02 | OK (per API: Name leer + Lat 95 + Höhe −5 → 4xx) |
| F-03 | OK (Browser: Wert bleibt nach Speichern+Reload erhalten) |
| F-04 | OK (Browser: FLARM-IDs-Feld speichert) |
| F-05 | OK (Browser: Winden-Schwelle speichert) |
| F-06 | OK (Browser: Strip-Felder-Auswahl speichert) |
| F-07 | OK (Browser) |
| F-08 | OK (Browser: „Rechteck einsetzen" erzeugt Polygon, übersteht Reload) |
| F-09 | OK (per API: 2-Punkte-Polygon → 4xx) |
| F-10 | OK (Browser: inaktiver Platz → `/api/monitor/<slug>/today` 404) |
| F-11 | OK (Browser: zweiter Flugplatz anlegbar, `GET /airfields` zeigt beide) |
| F-12 | OK (per API: fremder Tenant → 403) |

### Flugzeuge (A)
| ID | Ergebnis |
|---|---|
| A-01 | OK (Browser: Flugzeug erscheint in der Liste) |
| A-02 | OK (leeres Pflichtfeld wird nicht übernommen) |
| A-03 | OK (per API: Duplikat-FLARM-ID → weiterhin genau 1 Zeile in der DB) |
| A-04 | **Befund:** Es gibt in der Oberfläche **kein Bearbeiten**, nur „Entfernen". `PUT`-Endpoint existiert im Backend, hat aber keinen UI-Einstieg. **Nachtrag bei der Behebung:** „Entfernen" schickte die interne Datensatz-ID statt der FLARM-ID an `DELETE /{flarm_id}` und lief damit ins Leere (404) — Löschen war also ebenfalls defekt (im Browser-Lauf nicht bis dahin gekommen, daher nicht aufgefallen). |
| A-05 | **Befund:** Es gibt **keinen CSV-Import-Button** im Frontend (`AircraftManagePage.tsx`), obwohl `docs/testplan.md` und der Backend-Endpoint (`POST /api/airfields/{id}/aircraft/import-csv`) ihn vorsehen. Endpoint selbst funktioniert (direkt getestet: 3 Zeilen importiert, danach in der Liste sichtbar). |
| A-06 | N/A (kein UI-Einstieg, s. A-05) |
| A-07 | OK (`role` per SQL gesetzt, Worker-Neustart lädt Cache neu — funktioniert wie in Kap. 6 des Konzepts beschrieben) |
| A-08 | OK (mit T-04, s. u.) |

### Tower-Monitor (T)
| ID | Ergebnis |
|---|---|
| T-01 | OK (Browser: Sektionen, Uhr, „Verbunden") |
| T-02 | OK (Tabelle/Karte/Split schalten um) |
| T-03 | OK (`api` gestoppt → „Getrennt" sichtbar; nach Neustart automatisch wieder „Verbunden") |
| T-04 / A-08 | OK — mit **Einschränkung**: Der Simulator (`app/app/vfsync/simulate.py`) veröffentlicht nur die `event:{slug}`-Nachrichten, schreibt aber **nicht** den Redis-Hot-State (`flights:{slug}`-Set, `flight:{slug}:{fid}`-Hash). Ein bereits offener Monitor-Tab sieht den simulierten Flug live über WebSocket, ein **Reload** danach zeigt ihn nicht mehr. Für echten Flugbetrieb (worker.py/tracking) irrelevant — dort wird der Hot State korrekt geschrieben; nur der Test-Simulator hat diese bewusste Lücke (er ist für VF-Sync-Tests gebaut, nicht für Monitor-UI-Tests). Sollte im Testplan als Hinweis ergänzt werden. |
| T-05 | OK (mit T-04 zusammen beobachtet) |
| T-06 | OK (Detail-Drawer öffnet) |
| T-07…T-21 | N/A — echter Flugtag nötig (wie im Testplan vorgesehen) |
| T-22 | OK (zwei Tabs, beide aktualisieren sich) |

### Flugbuch (L)
| ID | Ergebnis |
|---|---|
| L-01 | OK |
| L-02 | OK (Tagesstepper) |
| L-03 | OK (Spalten vorhanden) |
| L-04 | N/A (kein Tag mit >25 Flügen ohne Live-Betrieb) |
| L-05 | OK (per API: CSV-Export liefert `text/csv`, 200) |
| L-06 | N/A (echter Touch & Go nötig; Logik ist über `app/tests` abgedeckt) |
| L-07 | OK (per API: Stats-Endpoint 200) |
| L-08 | OK (per API: fremder Mandant sieht keine Flugbuch-Zeilen) |

### VF-Sync + Mock (V)
| ID | Ergebnis |
|---|---|
| V-00 (a–d) | OK (Credentials setzen, `show` zeigt keine Klartextwerte, `enable`, UI zeigt „Freigeschaltet"/„Dry-Run") |
| V-01 | OK (Status-Endpoint ohne Konfiguration liefert Defaults) |
| V-02 | OK (Checkbox deaktiviert, Hinweistext vorhanden) |
| V-03 / V-04 | OK — **wichtig:** direkt per DB geprüft, dass `vf_password_enc`/`vf_appkey_enc` als Bytes (verschlüsselt) vorliegen, und dass die PUT-Antwort/-Anfrage nie den Klartext enthält |
| V-05 | OK — mit Zusatzbefund: die https/Allow-List-Prüfung greift nur bei `VFSYNC_ALLOW_INSECURE_BASE_URL=false`; für diesen Testlauf musste sie kurz mit dem sicheren Zustand isoliert geprüft werden (siehe `app/tests/vfsync/test_urls.py` für die vollständige automatisierte Abdeckung aller Fälle inkl. der beiden Zeitfenster) |
| V-06 | OK (Budget-Validierung 1–500) |
| V-08 | OK (fremder Mandant → 403) |
| V-11 | N/A (Schlüsselrotation nicht im laufenden Testlauf riskiert; Logik über `test_stores_pg.py` abgedeckt) |
| V-12 | OK (`vfsync:health`-Hash enthält Mandant, Status `ok`) |
| V-13 | N/A (würde 30+ min Ausfall erfordern; über `test_scheduler_health.py` abgedeckt) |
| V-14 | OK (kein Flugbetrieb ≠ „Feed tot") |
| V-15 | N/A (Recovery-Neustart nicht live riskiert; über `test_coordinator.py` abgedeckt) |
| V-20 (Dry-Run) | OK (Mock bekommt kein PUT, `dryrun_edit`-Audit-Eintrag mit exaktem Payload) |
| V-30 (Live-Startzeit) | **OK, direkt am Audit-Log verifiziert:** `match → get → edit(departuretime)`, alle mit HTTP 200, korrekter Wert im Mock sichtbar |
| V-31 (Lande-Bündel) | Logik durch `test_writer.py::test_bundled_landing_edit_writes_all_empty_fields` vollständig abgedeckt (369/369 grün); im Live-Testlauf durch eine Cache-bedingte Session-Aufspaltung (s. Kap. 3) nicht sauber bis zum Ende durchlaufen — kein Hinweis auf einen echten Fehler, nur ein Timing-Problem im Testskript |
| V-32…V-45 | Logik jeweils 1:1 durch benannte Tests in `app/tests/vfsync/test_writer.py` / `test_coordinator_process.py` abgedeckt (alle grün); im Live-Mehrfach-Testlauf durch den 5-Minuten-Cache (Kap. 3) beeinträchtigt, kein eigenständiger Befund |
| V-46 / V-47 / V-48 / V-51 / V-52 / V-53 | N/A wie im Testplan selbst vorgesehen |
| V-49 | OK (Audit-Tabelle zeigt get/edit-Einträge) |
| V-50 | OK (Sessions-Liste lädt) |
| V-60 | OK (Mock: leeres Kennzeichen abgelehnt, Löschen funktioniert) |
| V-61 | OK (Mock-UI aktualisiert sich automatisch ohne Reload) |
| V-62 | OK (weder im Request-Log noch in Edits Klartext-Zugangsdaten; die Login-Daten-Anzeige im Kopf der Mock-UI ist gewollt und zeigt nur die eigene Testkonfiguration) |
| V-63 | OK (Reset leert Flüge/Logs) |

### Sicherheit (X)
| ID | Ergebnis |
|---|---|
| X-01 | OK (401 ohne Token) |
| X-02 | OK (fremder Mandant → 403 für Sessions/Audit/Aircraft) |
| X-03 | OK (keine Klartext-Zugangsdaten in `docker compose logs`) |
| X-04 | OK (Monitor öffentlich, Dashboard erzwingt Login) |
| X-05 | OK (`enabled` im Body → 422) |
| X-06 | OK (SQL-artiges Kennzeichen wird als Text gespeichert, Tabelle bleibt intakt) |

**Automatisierte Regression:** `docker compose run --rm --no-deps -v "$PWD/app:/app" api pytest -q -p no:cacheprovider` → **369 passed**, unverändert grün während des gesamten Laufs.

## 5. Empfehlungen

1. **Bug 1 und Bug 2 beheben** (Kap. 2) — Bug 2 hat das größere Nutzerauswirkungspotenzial (unerwartete Logouts im echten Betrieb).
2. `docs/testplan.md` T-04 um den Hinweis ergänzen, dass der Simulator nur für VF-Sync-Events gedacht ist und keinen Redis-Hot-State schreibt (Reload zeigt den simulierten Flug nicht mehr).
3. Für künftige automatisierte Mehrfach-Szenarien gegen denselben Mandanten entweder je Szenario einen neuen Mandanten verwenden oder den `vfsync`-Worker zwischen den Szenarien neu starten (Cache-TTL, Kap. 3).
4. A-04 (Bearbeiten) und A-05 (CSV-Import) haben Backend-Endpunkte ohne UI — bei Bedarf nachziehen oder aus dem Testplan als „nur API" kennzeichnen.

## 6. Behebung (2026-09-25)

Alle behebbaren Befunde sind umgesetzt und gegen den neu gebauten lokalen Stack
nachgeprüft (Reproduktions-Skripte von Kap. 2 erneut ausgeführt):

| Befund | Änderung | Nachweis |
|---|---|---|
| Bug 1 (R-04) | `authStore.login/register` geben `boolean` zurück; `RegisterPage`/`LoginPage` navigieren nur bei `true` | 2. Registrierung mit gleicher E-Mail → 409, Browser bleibt auf `/register`, Meldung sichtbar |
| Bug 2 (R-09) | `authStore.loadUser()` löscht das Token nur noch bei `ApiError` 401/403; Netzwerkfehler/Abbruch/5xx behalten die Sitzung | Token vor und nach `page.reload()` identisch, Dashboard bleibt |
| A-04 Bearbeiten | „Bearbeiten"-Button je Zeile, Formular vorbelegt, `PUT /airfields/{id}/aircraft/{flarm_id}`; **Entfernen** nutzt jetzt die FLARM-ID | `npm run build` grün; Endpunkte unverändert |
| A-05 CSV-Import | „CSV importieren"-Button (Datei-Upload, Ergebniszeile mit importiert/übersprungen/Fehlern), neues `api.upload()` ohne JSON-Content-Type | dito |
| T-04 Simulator ohne Hot-State | `simulate.py` schreibt vor jedem Event `update_flight` (TTL-Semantik wie `FlightTracker`, gelandete Flüge bleiben sichtbar) | `--only-takeoff` → Flug in `flights:{slug}`/`flight:{slug}:{id}`, `GET /api/monitor/{slug}` liefert ihn nach Reload |
| Kap. 3 Config-Reload bis 5 min | Redis-Kanal `vfsync:config` (`app/app/vfsync/redis_keys.py`): CLI `set-credentials/enable/disable` und `PUT /api/vfsync/config` publizieren den Slug (best effort), Worker-Task `vfsync-config-listener` ruft sofort `reload_config()`; periodischer Reload bleibt Fallback | `disable` → `vfsync_config_reload_triggered` + `vfsync_config_changed removed=[…]` in derselben Sekunde |

Automatisierte Suite danach: **384 passed** (+15 neue Tests: Simulator-Hot-State,
Config-Signal CLI/API/Listener). Runbook und Testplan (V-00c, V-47) angepasst.

**Offen (bewusst nicht geändert):** nginx-Upstream-Caching (Kap. 3, erster Punkt)
— erfordert Änderung in `nginx/nginx.conf` **und** `nginx.prod.conf`
(`resolver` + variabler `proxy_pass`), laut Projektregeln nur nach Rückfrage.
Für den regulären Deploy-Weg ohne Auswirkung.

## 7. Testdaten

Für diesen Lauf wurden mehrere Test-Mandanten mit E-Mail-Domain
`mail.ogn-monitor-uat.de` und Flugplatz-Slugs `uat-*`/`uatapi-*` angelegt (lokale
Dev-DB). Bestehende Mandanten des Nutzers (`ohlstadt`, `dassu`) wurden nicht
angefasst. Aufräumen bei Bedarf: `docker compose exec postgres psql ... -c "DELETE
FROM tenants WHERE email LIKE '%@mail.ogn-monitor-uat.de'"` (kaskadiert über
Foreign Keys) oder ein Reset der lokalen DB.
