# Testhandbuch OGN FlightMonitor + VF-Sync

Für: manuelle Tester (Abnahme / Regression). Vorkenntnisse: Browser, Grundbegriffe
Segelflugbetrieb, Docker-Compose-Befehle aus diesem Dokument kopieren können.

Stand: 2026-09-24 · Branch `feat/vfsync-core` (enthält `feat/vfsync-ap7-ap9`)

---

## 0. Wie dieses Dokument benutzt wird

- Jeder Testfall hat eine **ID** (Bereich-Nr.), **Vorbedingung/Schritte** und ein
  **erwartetes Ergebnis**. Ergebnis im Protokoll (Kap. 9) festhalten: `OK`,
  `FEHLER` (mit Beschreibung, Screenshot, Uhrzeit UTC), `N/A` (nicht testbar,
  mit Grund).
- Reihenfolge: Kap. 1 (Aufbau) → Kap. 2 (Smoke) → dann Bereiche in beliebiger
  Reihenfolge; Kap. 6 (Live-Flugbetrieb) braucht einen realen Flugtag.
- **Nie** gegen das echte Vereinsflieger-System testen. Alle VF-Tests laufen
  gegen den Mock (Kap. 1.3).
- Zeiten: Monitor und Flugbuch zeigen **UTC**, die VF-Sync-Seite Lokalzeit.

Begriffe: *FLARM-ID* (6-stellige Hex-Kennung eines Geräts), *Kennzeichen*
(z. B. D-1234), *Tenant/Mandant* (ein registrierter Verein), *Slug* (URL-Kürzel
des Flugplatzes, z. B. `ohlstadt`), *Session* (ein von VF-Sync verfolgter Flug),
*Audit* (Protokoll jedes VF-API-Zugriffs), *Dry-Run* (VF-Sync rechnet alles,
schreibt aber nichts).

---

## 1. Testumgebung aufbauen

### 1.1 Voraussetzungen

- Docker Desktop (oder Docker Engine ≥ 24) mit Compose v2
- Repository ausgecheckt, Branch `feat/vfsync-core`
- Ports frei: `HTTP_PORT` aus `.env` (Standard 80; Frontend **und** API laufen hinter Nginx), 8099 (VF-Mock), 5432, 6379. Im Folgenden steht `http://localhost:8080` – ersetze 8080 durch deinen `HTTP_PORT`.
- Für Live-Tests (Kap. 6): Internetzugang zu `aprs.glidernet.org:14580`

### 1.2 `.env` anlegen

```
cp .env.example .env
```
Mindestens setzen:

| Variable | Wert für Tests |
|---|---|
| `DB_PASSWORD` | beliebiges Passwort |
| `JWT_SECRET` | ≥ 64 zufällige Zeichen |
| `COMPOSE_PROFILES` | `vfsync,vfmock` |
| `VFSYNC_CRED_KEY` | Ausgabe von `docker compose run --rm --no-deps api python -m app.vfsync.tools generate-key` |
| `VFSYNC_ALLOW_INSECURE_BASE_URL` | `true` (**nur lokal**) |
| `VF_MOCK_PASSWORD` / `VF_MOCK_APPKEY` | Defaults lassen (`mock-password` / `mock-appkey`) |

### 1.3 Starten

```
docker compose up -d --build
docker compose ps            # alle Dienste "running"/"healthy"
```

| Dienst | URL |
|---|---|
| Frontend (Monitor, Dashboard) | http://localhost:8080/ |
| API (nur über Nginx erreichbar) | http://localhost:8080/health, http://localhost:8080/api/health/ogn |
| VF-Mock (Oberfläche) | http://localhost:8099/ |
| VF-Sync-Health | nur im Container-Netz: `docker compose exec vfsync python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8090/healthz').read())"` |

Logs: `docker compose logs -f worker` (Tracking), `docker compose logs -f vfsync`
(VF-Sync), `docker compose logs -f api`.

### 1.4 Testdaten

Flugplatz für alle Beispiele: **Ohlstadt-Pömetsried** – Lat `47.6389`, Lon
`11.2347`, Höhe `660` m, Slug `ohlstadt`. Beispielflotte:

| FLARM-ID | Kennzeichen | WB-Kz. | Typ | Kategorie/Rolle |
|---|---|---|---|---|
| `DDA5BA` | D-1234 | SF | Ventus ct | Segelflugzeug |
| `DDB111` | D-5678 | W2 | LS4 | Segelflugzeug |
| `DDC222` | D-ETOW | – | Robin DR400 | Schleppflugzeug |
| `DDD333` | D-KMSF | C | Arcus M | Motorsegler |

CSV-Datei `flotte.csv` (für A-05):
```
flarm_id,registration,competition_sign,aircraft_model,aircraft_type
DDA5BA,D-1234,SF,Ventus ct,glider
DDB111,D-5678,W2,LS4,glider
DDC222,D-ETOW,,Robin DR400,tow_plane
DDD333,D-KMSF,C,Arcus M,motor_glider
```

### 1.5 Flug-Simulator (für VF-Sync ohne echten Flugbetrieb)

Erzeugt die Ereignisse eines Flugs genau so, wie der Tracking-Worker sie
veröffentlicht, und schreibt dabei auch den Redis-Hot-State. Der Flug erscheint
im Tower-Monitor (auch nach Reload; nach der Landung unter GELANDET) und wird
von VF-Sync verarbeitet. Er landet **nicht** im Flugbuch (kein DB-Write).

```
docker compose run --rm --no-deps api python -m app.vfsync.simulate \
  --slug ohlstadt --registration D-1234 --type aerotow \
  --release-agl 450 --tow-minutes 7 --duration-min 45 --touch-go 0 --delay 3
```
Wichtige Optionen: `--type winch|self|powered`, `--touch-go N`,
`--landing-method silence`, `--confidence 0.5` (Paarungs-Konfidenz),
`--release-agl 2500` (unplausibel), `--only-takeoff` (Flug bleibt in der Luft),
`--flarm SIM002` (zweites Gerät).

---

## 2. Smoke-Test (nach jedem Neustart, 5 Minuten)

| ID | Prüfung | Erwartet |
|---|---|---|
| S-01 | `docker compose ps` | postgres, redis, worker, api, nginx, vfsync, vfmock laufen; keine Restarts-Schleife |
| S-02 | http://localhost:8080/health | HTTP 200, JSON mit `status` |
| S-03 | http://localhost:8080/api/health/ogn | JSON mit `connected: true` (bei Internet) und `beacons_per_minute` > 0 nach ~1 min |
| S-04 | http://localhost:8080/ | Login-Seite lädt, kein Konsolenfehler |
| S-05 | http://localhost:8099/ | „Vereinsflieger-Mock“ lädt, Login-Daten im Kopf sichtbar |
| S-06 | `docker compose logs vfsync --tail 20` | `vfsync_started` mit 6 Tasks; kein `vfsync_cred_key_missing` |
| S-07 | VF-Sync-Health (Kap. 1.3) | HTTP 200, `"healthy": true` |
| S-08 | `docker compose run --rm --no-deps api python -m app.tools.import_elevation --check 47.6386 11.2394` | `≈ 676 m` (Geländemodell importiert, DEPLOYMENT.md 9a); Worker-Log zeigt `terrain_cache_warmed` mit `cells_with_data > 0` |

---

## 3. Registrierung, Login, Mandant (R)

| ID | Testfall | Schritte | Erwartet |
|---|---|---|---|
| R-01 | Registrierung | `/register`: E-Mail, Flugplatz-Name „Segelflugverein Ohlstadt“, Passwort 2× , Haftungsausschluss **anhaken**, absenden | Konto angelegt, Weiterleitung ins Dashboard, eingeloggt |
| R-02 | Registrierung ohne Disclaimer | wie R-01, Haken **nicht** setzen | Fehlermeldung, kein Konto (CLAUDE-Regel: kein Zugang ohne Akzeptanz) |
| R-03 | Passwörter ungleich | Passwort ≠ Bestätigung | Fehlermeldung vor dem Absenden |
| R-04 | Doppelte E-Mail | R-01 mit bereits genutzter E-Mail | Fehler „E-Mail bereits registriert“ (o. ä.), kein zweites Konto |
| R-05 | Login korrekt | `/login` mit R-01-Daten | Dashboard |
| R-06 | Login falsch | falsches Passwort | Fehlermeldung, kein Login; kein Hinweis, ob E-Mail existiert |
| R-07 | Geschützte Seiten ohne Login | im ausgeloggten Zustand `/dashboard`, `/dashboard/vfsync` aufrufen | Weiterleitung `/login` |
| R-08 | Logout | Logout in der Navigation | Zurück zu `/login`, `/dashboard` nicht mehr erreichbar |
| R-09 | Sitzung nach Reload | Eingeloggt, F5 | Bleibt eingeloggt (Token im Browser) |
| R-10 | Token abgelaufen | Token in `localStorage` löschen/ändern, Seite nutzen | Saubere Rückkehr zum Login, keine Endlosschleife |
| R-11 | Mandant löschen (optional, am Ende) | eigenes Konto löschen (API `DELETE /api/tenants`), danach Login | Login schlägt fehl; Flugplätze/Flugzeuge des Mandanten sind weg |

---

## 4. Flugplatz-Konfiguration (F) – Seite „Flugplatz“

| ID | Testfall | Schritte | Erwartet |
|---|---|---|---|
| F-01 | Flugplatz anlegen | Name, Slug `ohlstadt`, Lat/Lon/Höhe aus 1.4, speichern | Erfolgsmeldung; Worker-Log innerhalb 5 min `airfield_configs_loaded` mit dem Slug |
| F-02 | Pflichtfelder | Name leer / Lat 95 / Höhe -5 / Slug mit Leerzeichen | Jeweils Validierungsfehler, nichts gespeichert |
| F-03 | Schwellwerte ändern | Startgeschwindigkeit 40 → 45, Alarm-Timeout, Signalverlust-Timeout ändern, speichern, neu laden | Werte bleiben erhalten |
| F-04 | Schleppflugzeuge | „FLARM-IDs (kommagetrennt)“ = `DDC222`, speichern | Wird gespeichert; Basis für Startart „F-Schlepp“ (Kap. 6) |
| F-05 | Winden-Schwelle | `winch_vs_threshold_ms` z. B. 15 setzen | Gespeichert; Windenstarts mit < 15 m/s werden dann **nicht** als Winde erkannt (Kap. 6, T-14) |
| F-06 | Strip-Felder | Optionale Felder (Distanz, Flugdauer, Startart, QDR…) an/abwählen, speichern | Tower-Monitor zeigt nur die gewählten Felder (T-05) |
| F-07 | „Gelandet sichtbar (Min.)“ | auf 2 setzen | Gelandete Flüge verschwinden ~2 min nach Landung aus dem Monitor (T-10) |
| F-08 | Heimatbereich als Polygon | Polygon-Editor: Fläche um die Piste zeichnen, speichern, neu laden | Polygon bleibt; Start/Landung werden nur innerhalb erkannt (Kap. 6) |
| F-09 | Polygon ungültig | < 3 Punkte oder Koordinaten außerhalb | Validierungsfehler |
| F-10 | Flugplatz inaktiv | „Flugplatz aktiv“ abwählen | Monitor `/monitor/ohlstadt` → 404/Hinweis, keine Beacons mehr verarbeitet |
| F-11 | Zweiter Flugplatz | weiteren Flugplatz anlegen | Umschalter auf Flugplatz-, Flugzeug- und VF-Sync-Seite erscheint |
| F-12 | Fremder Flugplatz | mit Konto B die `airfield_id` von Konto A per API aufrufen (`GET /api/airfields/{id}`) | HTTP 403 |

---

## 5. Flugzeuge (A) – Seite „Flugzeuge“

| ID | Testfall | Schritte | Erwartet |
|---|---|---|---|
| A-01 | Flugzeug anlegen | FLARM-ID `DDA5BA`, D-1234, SF, Ventus ct, Kategorie Segelflugzeug | Erscheint in der Liste |
| A-02 | FLARM-ID ungültig | `ZZZZ` (kein Hex) / leer | Validierungsfehler |
| A-03 | Doppelte FLARM-ID | A-01 erneut | Fehler oder Aktualisierung des vorhandenen Eintrags – kein Duplikat in der Liste |
| A-04 | Bearbeiten / löschen | Kennzeichen ändern, speichern; danach löschen | Änderung sichtbar; nach Löschen weg |
| A-05 | CSV-Import | `flotte.csv` aus 1.4 importieren | 4 Einträge, vorhandene aktualisiert (Upsert), Ergebnis-Meldung mit Zählern |
| A-06 | CSV ohne Kopfzeile / mit Semikolon / falsche Endung | Varianten importieren | Kopfzeile optional; `.txt` wird abgelehnt; unbrauchbare Zeilen gemeldet |
| A-07 | Rolle für VF-Sync (V1 per SQL) | `UPDATE tenant_aircraft SET role='towplane' WHERE flarm_id='DDC222';` | Worker-Log nach ≤ 1 h (oder Neustart) `aircraft_cache_reloaded`; Schlepper wird a priori „Motor“ (T-15) |
| A-08 | Auflösung im Monitor | Flugzeug aus A-01 fliegt (Kap. 6) | Monitor zeigt Kennzeichen/WB-Kz./Typ statt nur FLARM-ID |

---

## 6. Tower-Monitor (T) – `/monitor/<slug>` (öffentlich, ohne Login)

Vorbedingung: Flugplatz aktiv, Flotte eingetragen. Für T-01…T-06 reicht der
Simulator (1.5); **T-07 bis T-20 brauchen echten Flugbetrieb** am
konfigurierten Platz (Flugtag mit Winde und F-Schlepp einplanen; alternativ
einen Platz mit regelmäßigem Betrieb konfigurieren).

| ID | Testfall | Schritte | Erwartet |
|---|---|---|---|
| T-01 | Seite und Verbindung | Monitor öffnen | Uhr (UTC) läuft, Verbindungsstatus „verbunden“, Sektionen NOTFALL/ALARM/KEIN SIGNAL/AUSSENLANDUNG/IM SCHLEPP/FLIEGEND/GELANDET |
| T-02 | Ansichten | Buttons Tabelle / Karte / Split | Karte zeigt Platz, Split beides; Auswahl überlebt Reload nicht zwingend (Info) |
| T-03 | Verbindungsabbruch | `docker compose stop api`, 20 s warten, `start` | Status „getrennt“, danach automatisch wieder „verbunden“, Flüge wieder da |
| T-04 | Simulierter Start | Simulator mit `--only-takeoff` | Flug erscheint sofort unter FLIEGEND (Startzeit UTC) |
| T-05 | Strip-Felder | F-06 verändern, Monitor neu laden | Felder folgen der Konfiguration |
| T-06 | Detail-Drawer | Zeile anklicken | Drawer mit Position, Höhe, Startart, Ausklinkhöhe (AGL), Landungszähler; „Ausblenden“ entfernt den Flug aus dem Monitor (nicht aus dem Flugbuch) |
| T-07 | Echter Start | Flugzeug startet | Innerhalb ~30 s unter FLIEGEND; Startzeit = Beginn des Startlaufs ± 1 min |
| T-08 | QDR/Distanz/Höhe | Flug entfernt sich | QDR (°, Himmelsrichtung), Distanz km, MSL/AGL aktualisieren sich alle paar Sekunden; Karte folgt |
| T-09 | Landung | Flugzeug landet und rollt aus | Wechsel nach GELANDET innerhalb ~15 s; Landezeit = Aufsetzzeit (nicht Stillstand) |
| T-10 | Sticky gelandet | nach T-09 warten | Flug bleibt „Gelandet sichtbar (Min.)“ lang sichtbar, dann weg; Flugbuch-Eintrag vorhanden |
| T-11 | Touch & Go | Landung mit Wiederstart < 90 s (Schulung) | **Kein** neuer Flug: bleibt FLIEGEND, Landungszähler +1 (Drawer), keine zweite Startzeit |
| T-12 | Neuer Flug nach Landung | dasselbe Flugzeug startet > 90 s nach Landung erneut | Alter Flug im Flugbuch, neuer Flug mit neuer Startzeit unter FLIEGEND |
| T-13 | Startart F-Schlepp | Schlepp mit konfiguriertem Schlepper (F-04/A-07) | Segler: „F-Schlepp“ + Schlepper-Kennzeichen; nach Ausklinken Ausklinkhöhe **AGL** (±50 m plausibel) und Schleppzeit; Schlepper selbst „Motor“ |
| T-14 | Startart Winde | Windenstart | „Winde“ innerhalb ~1 min; Ausklinkhöhe angezeigt; keine Schleppzeit |
| T-15 | Startart Eigenstart | Motorsegler (A-07 Rolle `motorglider_sl` oder Modell Arcus M) startet mit Motor | „Eigenstart“ nach ~1 min |
| T-16 | Parallel-Schlepps | zwei Schlepps gleichzeitig / dicht nacheinander | Jeder Segler bekommt seinen Schlepper; bei nicht trennbaren Paaren „F-Schlepp?“ **ohne** Höhe (nie eine falsche Höhe) |
| T-17 | Signalverlust | Flugzeug fliegt aus dem Empfangsbereich (> 5 min still) | KEIN SIGNAL (gelb) mit „Letzte Position“; bei Rückkehr wieder FLIEGEND |
| T-18 | Alarm | Signalverlust > Alarm-Timeout (F-03, zum Test 10 min setzen) | ALARM (rot), Banner mit Ton; „Quittieren“ stoppt Ton, Eintrag bleibt |
| T-19 | Funkstille nach Landung | FLARM direkt nach dem Aufsetzen ausschalten | Nach ~3 min GELANDET (Landung per Funkstille), **kein** Alarm; Drawer zeigt reduzierte Landekonfidenz |
| T-20 | Außenlandung | Flugzeug landet außerhalb (oder tief/langsam > 5 min entfernt) | AUSSENLANDUNG (orange) mit Position/Entfernung |
| T-21 | Überflug fremdes Flugzeug | Flugzeug fliegt über den Platz ohne dort gestartet zu sein | Erscheint **nicht** im Monitor |
| T-22 | Mehrere Browser | Monitor auf 2 Geräten | Beide zeigen dieselben Änderungen ohne Reload |
| T-23 | Alarm quittieren (eingeloggt) | Bei einem Flug unter ALARM/KEIN SIGNAL/AUSSENLANDUNG „Quittieren“ mit Kommentar („Pilot per Handy erreicht“) | 201; Flug bleibt in seiner Sektion, zeigt Status „quittiert“, Kommentar, Bearbeiter und Zeit (UTC); zweiter Browser sieht es ohne Reload; `GET /api/monitor/<slug>` liefert `alarmState`/`alarmComment`/`alarmSetBy`/`alarmSetAt` |
| T-24 | Alarm-Verlauf | Nach T-23 weitere Aktionen „Rückholung läuft“ und „Erledigt“; dann `GET /api/monitor/<slug>/flights/<flarmId>/actions` bzw. `…/actions?date=YYYY-MM-DD` und `GET /api/monitor/<slug>/actions` | Liste **neueste zuerst** mit `state`, `comment`, `alarmKind` (aus dem Flugstatus: alarm/emergency/outlanding/signal_lost, sonst other), `setBy`, `createdAt`, `flightTakeoffTs`; anderer Tag → leer; fremder Mandant → 403; ohne Login → 401; Verlauf bleibt auch nach Archivierung/„Ausblenden“ des Flugs erhalten |
| T-25 | Fehlalarm | Bei einem Alarm „Fehlalarm“ ohne Kommentar setzen | Flug bleibt sichtbar (kein Entfernen), `alarmState = false_alarm`, `alarmComment = ""`; Kommentar > 500 Zeichen oder unbekannter Status → 422; Eintrag im Verlauf (T-24) |
| T-26 | Landung an fremdem Platz | Vorbedingung: `import_airports` gelaufen (DEPLOYMENT 9b). Flugzeug landet auf einem bekannten Platz (z. B. Unterwössen), nicht zu Hause | **Keine** AUSSENLANDUNG: Wechsel nach GELANDET mit `landingType = foreign`, `landingAirfield = "Unterwoessen Airfield (EDPU)"`; Flugbuch-Eintrag Status `foreign`, Landeort gefüllt; startet es dort wieder, neuer Flug mit Startort = dieser Platz |
| T-27 | Rückkehr nach Außenlandung | Motorsegler landet außen (kein bekannter Platz, > 5 min), fliegt weiter und landet zu Hause | AUSSENLANDUNG wird als eigener Flug ins Flugbuch geschrieben (Landezeit = Beginn des Verdachts, Status `outlanding`); ein **neuer** Flug (Startort „Feld“, Startzeit = erster Luft-Beacon) erscheint unter FLIEGEND und landet normal zu Hause (`landingType = home`); kein Flug bleibt als Status 4 am Platz stehen |
| T-28 | Besucher | Fremdes Flugzeug startet an bekanntem Platz (< 300 km) und landet zu Hause; alternativ ein Flugzeug ohne Bodenkontakt in der 15-km-Zone | Beim Einflug in die 15-km-Zone erscheint es als Besucher (`isVisitor`, Startort + Startzeit bzw. „unbekannt“, Toast „Besucher aus …“); Landung → GELANDET (`landingType = home`), Flugbuch mit Startort; fliegt es ohne Landung wieder weg (> 18 km) oder verstummt es, verschwindet es **ohne** Alarm und ohne Flugbuch-Eintrag |

---

## 7. Flugbuch (L) – Seite „Flugbuch“

| ID | Testfall | Schritte | Erwartet |
|---|---|---|---|
| L-01 | Standard heute | Seite öffnen | Datum = heute, Einträge des Tages (nach Landungen aus Kap. 6) |
| L-02 | Tages-Stepper | ‹ › und Datumsfeld | Wechsel funktioniert, Liste aktualisiert |
| L-03 | Spalten | Eintrag prüfen | Kennzeichen, Start, Landung (UTC), Dauer, Max-Höhe, Max-Distanz, Startart (Winde/F-Schlepp/Eigen/Motor), Status |
| L-04 | Paginierung | > 25 Flüge an einem Tag (oder per_page in URL) | Seitenwechsel korrekt, Gesamtzahl stimmt |
| L-05 | CSV-Export | Export für einen Tag | Datei `fluglog-<datum>.csv` mit Datum, Kennzeichen, WB-Kz., Typ, Start, Landung, Dauer, Höhen, Startart, Schlepper, Ausklinkhöhe; Umlaute korrekt |
| L-06 | T&G im Flugbuch | nach T-11 | Ein Eintrag, Landungszähler 2 (in DB `landing_count`; API/CSV zeigen Startart) |
| L-07 | Statistik | `GET /api/flight-log/stats?days=7` (Browser mit Token oder curl) | Anzahl Flüge, Winde/F-Schlepp/Eigen je Tag; „F-Schlepp?“ zählt als F-Schlepp |
| L-08 | Mandantentrennung | Konto B öffnet Flugbuch | Sieht **nur** eigene Flüge |

---

## 8. VF-Sync und Vereinsflieger-Mock (V)

### 8.1 Vorbereitung (einmalig)

| ID | Schritt | Erwartet |
|---|---|---|
| V-00a | Mandant auf den Mock zeigen: `VF_PASSWORD=mock-password VF_APPKEY=mock-appkey docker compose run --rm --no-deps -e VF_PASSWORD -e VF_APPKEY api python -m app.vfsync.tools set-credentials --slug ohlstadt --username mock-user --base-url http://vfmock:8099` | „credentials stored for ohlstadt (encrypted)“ |
| V-00b | `… tools show --slug ohlstadt` | `enabled False`, `dry_run True`, `has_password True`, `has_appkey True`; **keine** Klartext-Werte |
| V-00c | `… tools enable --slug ohlstadt` | `enabled, dry_run=True`; `docker compose logs vfsync` zeigt innerhalb weniger Sekunden `vfsync_config_reload_triggered` und `vfsync_config_changed added=['ohlstadt']` |
| V-00d | Seite `/dashboard/vfsync` | Badge „Freigeschaltet“, amber „Dry-Run“, Budget 0/450, Worker-Health mit Heartbeat |

### 8.2 Konfigurations-Seite

| ID | Testfall | Schritte | Erwartet |
|---|---|---|---|
| V-01 | Ohne Konfiguration | mit frischem Mandanten (ohne V-00) Seite öffnen | Defaults (nicht freigeschaltet, Dry-Run, 450), kein Fehler |
| V-02 | `enabled` nicht änderbar | Checkbox „Freigeschaltet“ | Deaktiviert, Hinweis „Freischaltung erfolgt durch den Betreiber“ |
| V-03 | Credentials speichern | Benutzer, Passwort, AppKey eingeben, speichern | Erfolg; Felder leer; „gesetzt: ja“; Antwort/Netzwerk-Tab enthält **nie** Passwort/AppKey; DB: `SELECT vf_password_enc FROM vf_sync_config` = Bytes, kein Klartext |
| V-04 | Passwort wird gehasht | Netzwerk-Tab beim Speichern | Body enthält `vf_password_md5` (32 Hex), nicht das Passwort |
| V-05 | Base-URL Regeln | `http://www.vereinsflieger.de` / `https://evil.example` / `https://www.vereinsflieger.de/x` | Jeweils Fehlermeldung (https-Pflicht, Host nicht freigegeben, kein Pfad); `https://www.vereinsflieger.de/` wird akzeptiert |
| V-06 | Flags & Budget | Flags an/aus, Budget 300 speichern; Budget 501 | Gespeichert; 501 → lesbare Validierungsmeldung |
| V-07 | Dry-Run umschalten | Dry-Run aus, speichern | Badge rot „Scharf – schreibt in Vereinsflieger“ |
| V-08 | Fremder Flugplatz | Konto B ruft `GET /api/vfsync/config/<id von A>` auf | 403 |

### 8.3 Worker und Health

| ID | Testfall | Schritte | Erwartet |
|---|---|---|---|
| V-10 | Ohne Schlüssel | `VFSYNC_CRED_KEY=` leer, `docker compose up -d vfsync` | Log `vfsync_cred_key_missing`, Container beendet sich; danach Schlüssel wieder setzen |
| V-11 | Falscher Schlüssel | anderen `generate-key`-Wert eintragen, Neustart | Log `vfsync_config_decrypt_failed`, Mandant nicht aktiv; Health-Snapshot `config_errors`; UI zeigt Worker ohne Mandant; Rotation (Runbook Kap. 5) behebt es |
| V-12 | Health-Hash | `docker compose exec redis redis-cli HGETALL vfsync:health` | `status ok`, `tenants ohlstadt`, `budget_used:ohlstadt`, `stage:ohlstadt normal`, `updated_at` aktuell |
| V-13 | Health 503 | `docker compose stop worker` (APRS), > 30 min warten | `/healthz` 503 (`aprs_connected false`), Alarm `feed_dead`; nach `start` wieder 200 |
| V-14 | Kein Flugbetrieb ist kein Fehler | APRS-Worker läuft, stundenlang keine Flüge | `/healthz` bleibt 200, kein `feed_dead` |
| V-15 | Neustart-Recovery | Simulator `--only-takeoff`; `docker compose restart vfsync`; dann restlichen Flug simulieren | Session überlebt; Landung wird geschrieben (Audit `recovery` nur, wenn zwischenzeitlich gelandet) |

### 8.4 Dry-Run

| ID | Testfall | Schritte | Erwartet |
|---|---|---|---|
| V-20 | Dry-Run schreibt nichts | Dry-Run an (V-00c); im Mock Flug D-1234 anlegen; Simulator F-Schlepp | Mock: Felder bleiben leer, „Schreibzugriffe“ leer, Request-Log zeigt nur signin/list/get; VF-Sync-Seite: Session `completed`, Audit `dryrun_edit` mit exaktem Payload (departuretime, arrivaltime, towheight 450, towtime 7) |

### 8.5 Scharfer Betrieb gegen den Mock (`… tools enable --slug ohlstadt --live`)

Vor jedem Szenario im Mock „Alles zurücksetzen“ und die VF-Sync-Sessions des
Tages im Blick behalten (`/dashboard/vfsync`, Datum heute). Kennzeichen im
Mock **exakt** wie beim Simulator (`--registration`).

| ID | Testfall | Schritte | Erwartet (Mock-UI / VF-Sync-Seite) |
|---|---|---|---|
| V-30 | Live-Startzeit | Flug D-1234 (Startart leer); Simulator `--only-takeoff` | Innerhalb Sekunden `departuretime` gesetzt (Minute, UTC); Session `departure_written`; Request-Log: accesstoken, signin, list/today, get, edit |
| V-31 | Lande-Bündel F-Schlepp | wie V-30 mit vollem Simulator-Lauf `--type aerotow --release-agl 450 --tow-minutes 7` | Ein einziger weiterer `edit` mit `arrivaltime`, `towheight=450`, `towtime=7`; Session `completed`; Startart im Mock bleibt leer (wird nie geschrieben) |
| V-32 | Winde ohne Höhe | Flug D-5678; `--registration D-5678 --type winch` | `departuretime`/`arrivaltime` gesetzt, `towheight`/`towtime` **leer** |
| V-33 | Touch & Go | `--touch-go 1` | `landingcount` = 2 im Lande-Bündel; kein Zwischenschreiben beim T&G |
| V-34 | Manuelle Eingaben gewinnen | Flug mit `Startzeit` vorab `2026-09-24 09:58` und Landungen 1; Simulator | `departuretime` bleibt `09:58`; nur leere Felder werden gefüllt; Audit `get` zeigt `pre_state` |
| V-35 | landingcount nie verringern | Flug mit Landungen 3; Simulator `--touch-go 1` (=2) | `landingcount` bleibt 3; Session `review` mit Grund `landingcount_vf_higher` |
| V-36 | Kein VF-Flug | Simulator ohne Mock-Flug | Session `awaiting_match` (Grund `no_candidate`), keine Edits; danach Flug im Mock anlegen → spätestens nach 15 min (Flug in der Luft) bzw. 1 h (gelandet) geschrieben |
| V-37 | Zwei leere Flüge gleiches Kennzeichen | Mock: 2× D-1234; Simulator `--only-takeoff` | `awaiting_match` mit `ambiguous_match:<flid>,<flid>`, nichts geschrieben; einen Flug im Mock mit anderer Startzeit füllen → nächster Retry matcht den anderen |
| V-38 | Startart-Konflikt | Mock-Flug mit Startart `W`; Simulator `--type aerotow` | Nichts geschrieben; Session `review`/`awaiting_match` mit `starttype_conflict`; Mock-Startart unverändert |
| V-39 | Startart kompatibel | Mock-Flug mit Startart `F`; Simulator `--type aerotow` | Normal geschrieben (V-31) |
| V-40 | Niedrige Paarungs-Konfidenz | `--confidence 0.5` | `arrivaltime` geschrieben, **keine** `towheight`/`towtime`; Session `review` mit `towheight_low_pairing_confidence` |
| V-41 | Unplausible Höhe | `--release-agl 2500` | `towtime` ja, `towheight` nein; Grund `towheight_implausible` |
| V-42 | Silence-Landung | `--landing-method silence` | `arrivaltime` geschrieben (Konfidenz 0.85 ≥ 0.8); Session `landing_method silence` |
| V-43 | Zweiter Flug, nur ein VF-Flug | V-31 komplett; dann zweiten Simulator-Lauf mit `--flarm SIM002` gleiches Kennzeichen | Zweite Session `awaiting_match`; der bereits abgeschlossene VF-Flug wird **nicht** überschrieben (Write Precision) |
| V-44 | VF-Flug gelöscht | nach V-30 den Flug im Mock löschen, Rest simulieren | Audit `error` (404) einmal, Session `awaiting_match` mit `vf_flight_deleted`; neuen Flug anlegen → wird beim Retry gematcht |
| V-45 | Idempotenz | nach V-31 `docker compose restart vfsync` | Keine zusätzlichen `edit`-Zeilen im Mock; Session bleibt `completed` |
| V-46 | Budget-Stufen | Budget im UI auf 6 setzen; Simulator-Läufe | Ab 60 % keine Live-Startzeit (Session bleibt `tracking`, Bündel nach Landung kommt); bei 100 % Stufe `hard_stop`, Session gequeued, Alarm im Log; Budget-Balken im UI |
| V-47 | Falsche Credentials | AppKey im UI auf „falsch“ setzen (Worker übernimmt Änderung sofort), Simulator | Request-Log: signin 403; Log `vfsync_tenant_paused`; keine weiteren Calls; richtigen Key speichern → nach Reload wieder aktiv |
| V-48 | 400 vom VF | Mock-Flug anlegen, dann in DB-Session `release_alt_agl_m` bleibt – alternativ nur per Entwickler (fail_next); dokumentieren als N/A wenn nicht testbar | Session `review` mit `vf_400`, kein Retry |
| V-49 | Audit-Ansicht | Session anklicken | Einträge get/edit/match/abstain mit Zeit, flid, gesendeten Feldern, HTTP-Status; `pre_state` ohne Zugangsdaten |
| V-50 | Sessions-Filter | Datum wechseln, Status-Filter `review` | Liste filtert; Paginierung bei > 50 |
| V-51 | Budget-Tageswechsel | Budget-Zähler nach lokal 00:00 | Zähler beginnt bei 0 (Tag = Europe/Berlin) |
| V-52 | Scheduler-Läufe | `docker compose logs vfsync | grep vfsync_budget_day_closed` am Folgetag 03:00 | Vortagsbudget im Log; Sessions > 7 Tage → `expired` |
| V-53 | Alarm-Kanal (optional) | `VFSYNC_ALERT_NTFY_URL` auf ein ntfy-Topic setzen, V-46/V-47 wiederholen | Push-Nachricht; gleiche Bedingung wird 6 h lang nicht wiederholt |

### 8.6 Mock-Oberfläche selbst

| ID | Testfall | Schritte | Erwartet |
|---|---|---|---|
| V-60 | Flug anlegen/löschen | Formular ausfüllen, anlegen; löschen | Zeile erscheint/verschwindet; leeres Kennzeichen → Fehlermeldung |
| V-61 | Auto-Refresh | Simulator laufen lassen, nicht neu laden | Felder erscheinen ohne Reload (≤ 3 s), frisch geschriebene grün |
| V-62 | Request-Log | beliebige Aktion | Kein `accesstoken`/`password`/`appkey` im Log |
| V-63 | Zurücksetzen | „Alles zurücksetzen“ | Flüge und Logs leer; VF-Sync-Sessions bleiben (getrenntes System) |

---

## 9. Sicherheit und Mandantentrennung (X)

| ID | Testfall | Schritte | Erwartet |
|---|---|---|---|
| X-01 | API ohne Token | `curl http://localhost:8080/api/airfields` | 401 |
| X-02 | Fremde Ressourcen | Konto B: `GET /api/vfsync/sessions?airfield_id=<A>`, `/api/vfsync/audit?airfield_id=<A>`, `/api/aircraft?airfield_id=<A>` | 403 |
| X-03 | Credentials in Logs | `docker compose logs api vfsync | grep -i -E "password|appkey|accesstoken"` nach V-03/V-30 | Nur maskierte (`***`) oder keine Treffer |
| X-04 | Monitor öffentlich, Rest nicht | `/monitor/ohlstadt` ohne Login; `/dashboard` ohne Login | Monitor lädt; Dashboard leitet um |
| X-05 | Selbstaktivierung | `PUT /api/vfsync/config/<id>` mit `{"enabled": true}` | 422; `enabled` bleibt wie vorher |
| X-06 | SQL/Eingaben | Kennzeichen `D-1234'; DROP TABLE` anlegen | Wird als Text gespeichert, keine Fehler 500 |

---

## 10. Automatisierte Tests (Regression, vor jedem Testzyklus)

```
docker compose run --rm --no-deps -v "$PWD/app:/app" api pytest -q -p no:cacheprovider
```
Erwartet: alle Tests grün (Stand: 369 passed; `test_stores_pg`/`tests/api`
brauchen die laufende Postgres, sonst werden sie übersprungen). Bei Fehlern: Ausgabe ins Protokoll, Test-ID
nennen. CI (GitHub Actions) führt dieselbe Suite plus Secret-Scan aus.

---

## 11. Abnahmeprotokoll (Vorlage)

| ID | Ergebnis | Tester | Datum/Zeit (UTC) | Bemerkung / Ticket |
|---|---|---|---|---|
| S-01 | | | | |
| … | | | | |

Abnahmekriterien (Konzept Kap. 12): alle S-, R-, F-, A-, L-, X-Fälle OK;
T-07…T-20 an mindestens einem Flugtag mit Winde **und** F-Schlepp OK;
V-30…V-45 OK mit **Write Precision 100 %** (kein Wert am falschen Flug, kein
überschriebener manueller Wert). Bekannte, akzeptierte Einschränkungen:
Außenlandungs-Erkennung nutzt die Platzhöhe (im Gebirge ungenau); Flüge
während einer VF-Sync-Downtime bekommen keine Session; AP-12 (echte VF-API)
noch nicht durchgeführt – VF-Feldnamen/Formate sind Annahmen.
