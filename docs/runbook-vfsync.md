# Runbook VF-Sync (Vereinsflieger-Integration)

Betriebshandbuch für den Worker `vfsync` (Konzept: `docs/konzept-vf-sync.md`,
Schnittstellen: `docs/dev-guides/vfsync-internals.md`).

## 1. Komponenten

| Was | Wo |
|---|---|
| Worker | Compose-Service `vfsync` (Profil `vfsync`), `python -m app.vfsync` |
| Konfiguration je Mandant | Tabelle `vf_sync_config` (Credentials Fernet-verschlüsselt) |
| Sessions / Audit / Budget | `vf_sync_sessions`, `vf_sync_audit` (append-only), `vf_sync_budget` |
| Health | `GET http://vfsync:8090/healthz` (intern), Redis-Hash `vfsync:health` |
| Web-UI | `/dashboard/vfsync` (Status, Konfiguration, Sessions, Audit) |
| Alarme | ntfy (`VFSYNC_ALERT_NTFY_URL`) und/oder E-Mail (`VFSYNC_ALERT_EMAIL_TO` + SMTP_*) |

## 2. Erstinbetriebnahme

1. Schlüssel erzeugen und in `.env` eintragen (Rechte 600, nie committen):
   ```
   docker compose run --rm --no-deps api python -m app.vfsync.tools generate-key
   # → VFSYNC_CRED_KEY=... in .env
   ```
2. Technischen VF-Benutzer anlegen (Minimalrechte: Flugdatenerfassung
   lesen/schreiben, keine Mitglieder-/Finanz-/Adminrechte) und AppKey erzeugen.
3. Credentials verschlüsselt speichern (Secrets per ENV, nicht als Argument):
   ```
   VF_PASSWORD='…' VF_APPKEY='…' docker compose run --rm --no-deps \
     -e VF_PASSWORD -e VF_APPKEY api \
     python -m app.vfsync.tools set-credentials --slug ohlstadt --username <techuser> [--cid <Vereinsnr>]
   ```
4. Mandant im **Dry-Run** freischalten (Konzept 7.2 – keine Selbstaktivierung im UI):
   ```
   docker compose run --rm --no-deps api python -m app.vfsync.tools enable --slug ohlstadt
   ```
   Ein laufender Worker übernimmt `set-credentials`, `enable`, `disable` und
   Konfigurationsänderungen aus dem UI **sofort**: nach jedem erfolgreichen
   Schreibvorgang wird der Slug auf dem Redis-Kanal `vfsync:config`
   veröffentlicht, der Worker lädt daraufhin seine Mandanten neu
   (Log `vfsync_config_reload_triggered`, dann `vfsync_config_changed`).
   Fällt das Signal aus (Redis nicht erreichbar), greift der periodische
   Reload (`VFSYNC_CONFIG_RELOAD_S`, Standard 5 min); ein Neustart des
   Workers ist nur der Fallback, nicht der Normalweg.
5. Profil dauerhaft aktivieren: `COMPOSE_PROFILES=vfsync` in `.env` (siehe
   `.env.example`). **Ohne diesen Eintrag** startet `deploy.sh`
   (`docker compose up -d --build`) den Worker nicht und baut einen bereits
   laufenden `vfsync`-Container beim nächsten Deploy nicht neu – er liefe
   dann mit altem Code weiter. Danach: `docker compose up -d --build vfsync`.
   `VFSYNC_CRED_KEY` wird auch dem `api`-Container gereicht (Credentials im
   UI speichern, Operator-CLI).
6. Prüfen: `curl -s http://localhost:8090/healthz` im Container-Netz bzw.
   Web-UI → Status „Dry-Run“, Sessions erscheinen bei Flugbetrieb, Audit zeigt
   `dryrun_edit` mit dem Payload, der geschrieben worden wäre.

Scharfschalten erst nach der Validierungsphase (Konzept Kap. 12):
`python -m app.vfsync.tools enable --slug ohlstadt --live` bzw. `dry_run`
im UI abschalten. Sessions, die im Dry-Run als `completed` gebucht wurden,
werden nach dem Umschalten **nicht** nachgeschrieben (sie gelten als
erledigt) – der Stichtag der Scharfschaltung ist damit auch der erste Tag
mit echten Einträgen.

## 2a. Lokal testen mit dem Vereinsflieger-Mock (kein echter VF-Zugriff)

Der Mock (`app/app/vfsync/vf_client/mock.py`) bildet die genutzten
VF-Endpunkte nach und hat eine Web-Oberfläche, in der du Flüge wie ein
Pilot anlegst und live siehst, was VF-Sync schreibt (Request-Log mit
Payloads, `flight/edit`-Log, Feldwerte je Flug).

1. In `.env`: `COMPOSE_PROFILES=vfsync,vfmock`, `VFSYNC_ALLOW_INSECURE_BASE_URL=true`
   (nur lokal! erlaubt `http://vfmock:8099` als `vf_base_url`).
2. Starten: `docker compose up -d --build` → UI unter `http://localhost:8099/`.
3. Mandant auf den Mock zeigen lassen (Passwort/AppKey = `VF_MOCK_PASSWORD`,
   `VF_MOCK_APPKEY` aus `.env`, Default `mock-password` / `mock-appkey`):
   ```
   VF_PASSWORD=mock-password VF_APPKEY=mock-appkey docker compose run --rm --no-deps \
     -e VF_PASSWORD -e VF_APPKEY api python -m app.vfsync.tools set-credentials \
     --slug ohlstadt --username mock-user --base-url http://vfmock:8099
   docker compose run --rm --no-deps api python -m app.vfsync.tools enable --slug ohlstadt --live
   ```
   (`--live`, sonst schreibt der Worker nur `dryrun_edit`-Audit-Einträge – auch
   das ist im UI unter `/dashboard/vfsync` sichtbar.)
4. Im Mock-UI einen Flug anlegen (Kennzeichen wie im Monitor, z. B. `D-1234`,
   Startart leer oder passend).
5. Flug auslösen – entweder echter Flugbetrieb oder ein simulierter Flug, der
   dieselben Events wie der APRS-Worker veröffentlicht:
   ```
   docker compose run --rm --no-deps api python -m app.vfsync.simulate \
     --slug ohlstadt --registration D-1234 --type aerotow --release-agl 450 \
     --tow-minutes 7 --duration-min 45 --touch-go 1 --delay 3
   ```
   Im Mock-UI erscheinen nacheinander `departuretime` (sofort nach `takeoff`)
   und mit `landing_final` das Bündel `arrivaltime`, `towheight`, `towtime`,
   `landingcount`. Mit `--only-takeoff` bleibt der Flug in der Luft (Retry-
   Verhalten testen), mit `--type winch` gibt es keine Höhe.
6. Szenarien: zwei leere Flüge desselben Kennzeichens → `awaiting_match`
   mit `ambiguous_match`; Startart `W` im Mock bei simuliertem `--type aerotow`
   → `starttype_conflict`; Felder vorab im Mock füllen → werden nie
   überschrieben.

Der Mock ist **nicht** für den Server gedacht: Profil `vfmock` und
`VFSYNC_ALLOW_INSECURE_BASE_URL` dort nie setzen.

## 3. Betrieb

- **Budget:** 450 Requests/Tag je Mandant (VF-Limit 500). Stufen: ≥ 60 % keine
  Live-Startzeit, ≥ 90 % nur F-Schlepps, 100 % Stopp. Tagesstand im UI und
  in `vf_sync_budget`.
- **Retry:** 15 min (offene Flüge ohne Match), stündlich (alle offenen),
  21:00 lokal Abschlusslauf, 03:00 Expiry (> 7 Tage → `expired`).
- **Review-Sessions** (`state = review`): manuell im VF prüfen; Grund steht
  in `review_reason` (z. B. `ambiguous_match`, `starttype_conflict`,
  `towheight_low_pairing_confidence`). Der Worker schreibt an diesen
  Sessions nichts mehr.
- **Manuelle Eingaben in VF gewinnen immer** – der Worker füllt nur leere
  Felder und erhöht `landingcount` nie nach unten.
- **Ignorierte Events:** Der Worker verarbeitet nur Events mit
  `takeoff_time`. Ein `takeoff` ohne Startzeit (Log
  `vfsync_takeoff_without_time`) oder ein `landing_final` /
  `touch_and_go` / `launch_type_detected` / `landing_retracted` ohne
  Startzeit (Log `vfsync_event_without_takeoff_time`) wird verworfen – es
  wird weder eine Session mit der aktuellen Uhrzeit angelegt noch an eine
  andere offene Session desselben Flugzeugs angehängt. Besucher (Flugzeuge,
  die nicht am Platz gestartet sind, `is_visitor = "1"`) werden nie in das
  Vereinsflieger dieses Vereins gebucht (Log `vfsync_visitor_ignored`); sie
  erscheinen nur im Tower-Monitor.

## 4. Alarme (R-12)

| Alarm | Ursache | Maßnahme |
|---|---|---|
| `feed_dead` | APRS-Worker > 30 min ohne OGN-Verbindung (`ogn:health`) | APRS-Worker/Netz prüfen (`docker compose logs worker`) |
| `config_error:*` | Credentials nicht entschlüsselbar | `VFSYNC_CRED_KEY` prüfen, ggf. Rotation (Kap. 5) |
| `budget:*` / `budget_stop:*` | > 80 % / 100 % Budget | Abwarten (Folgetag), ggf. `daily_budget` prüfen; Ursache für hohe Call-Zahl im Audit |
| `pending:*` | Session > 24 h offen | Flug in VF anlegen (kein Match) oder Session im UI ansehen |
| `write_errors:*` | ≥ 3 Fehler in Folge | Audit `error` lesen; VF erreichbar? Credentials? |
| `login:*` | 403 beim Login | Passwort/AppKey/2FA des technischen Users prüfen, Rotation (Kap. 5) |

## 5. Credential-Rotation / Kompromittierung (< 10 min)

1. In VF: Passwort des technischen Users ändern, AppKey neu erzeugen.
2. `set-credentials` (Abschnitt 2, Schritt 3) erneut ausführen.
3. Der Worker übernimmt die neuen Credentials sofort (Redis-Signal
   `vfsync:config`, Log `vfsync_config_reload_triggered`) und hebt eine
   Login-Pause des Mandanten auf. Nur falls das Signal nicht ankommt
   (Redis-Störung) und nicht bis zum periodischen Reload gewartet werden
   soll: `docker compose --profile vfsync restart vfsync`.
4. Bei Verdacht auf Schlüsselverlust zusätzlich `VFSYNC_CRED_KEY` neu erzeugen,
   dann Schritt 2 für **alle** Mandanten wiederholen (alte Tokens sind mit dem
   neuen Schlüssel nicht lesbar).

## 6. Backup / Restore

`vf_sync_audit` ist abrechnungsrelevant. Der tägliche `pg_dump` (siehe
`DEPLOYMENT.md`) enthält alle `vf_sync_*`-Tabellen. Restore-Test:
`scripts/pull-db.sh` in eine lokale Instanz, dann
`SELECT count(*) FROM vf_sync_audit;` und Stichprobe im UI.

## 7. Troubleshooting

| Symptom | Prüfen |
|---|---|
| Worker beendet sich sofort | `VFSYNC_ENABLED=true`? `VFSYNC_CRED_KEY` gesetzt? Logs `vfsync_cred_key_missing` |
| Keine Sessions trotz Flugbetrieb | Mandant `enabled`? Slug korrekt? `redis-cli PSUBSCRIBE 'event:*'` zeigt Events? |
| `awaiting_match` bleibt | Flug in VF noch nicht angelegt oder Kennzeichen weicht ab (`callsign` vs. `registration`) |
| `starttype_conflict` | Pilot hat in VF eine andere Startart eingetragen als erkannt – nie automatisch korrigieren |
| `InvalidToken` im Log | Falscher `VFSYNC_CRED_KEY` für die gespeicherten Credentials → Rotation |
| 400 von VF | Audit `error` mit Payload; Feldnamen/Format gegen Spike-Ergebnisse (`docs/vf-api-spike-results.md`) prüfen |

## 8. Datenschutz-Abfragen (DSGVO)

Auskunft je Kennzeichen:
```sql
SELECT * FROM vf_sync_sessions WHERE registration = 'D-1234' ORDER BY takeoff_ts;
SELECT a.* FROM vf_sync_audit a JOIN vf_sync_sessions s USING (session_id)
 WHERE s.registration = 'D-1234' ORDER BY a.ts;
```
Löschung nach Ablauf der Aufbewahrungsfrist (Default 2 Jahre): Sessions
löschen, Audit-Zeilen behalten `session_id = NULL` (FK `ON DELETE SET NULL`);
das Audit selbst wird nur per Löschjob nach Frist entfernt, nie im Betrieb.
