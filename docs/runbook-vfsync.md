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
5. Worker starten: `docker compose --profile vfsync up -d --build vfsync`
   (`deploy.sh` startet nur die Standarddienste; das Profil muss auf dem
   Server einmalig mit `COMPOSE_PROFILES=vfsync` in `.env` aktiviert werden).
6. Prüfen: `curl -s http://localhost:8090/healthz` im Container-Netz bzw.
   Web-UI → Status „Dry-Run“, Sessions erscheinen bei Flugbetrieb, Audit zeigt
   `dryrun_edit` mit dem Payload, der geschrieben worden wäre.

Scharfschalten erst nach der Validierungsphase (Konzept Kap. 12):
`python -m app.vfsync.tools enable --slug ohlstadt --live` bzw. `dry_run`
im UI abschalten.

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

## 4. Alarme (R-12)

| Alarm | Ursache | Maßnahme |
|---|---|---|
| `feed_dead` | > 30 min kein Event | APRS-Worker/Redis prüfen (`docker compose logs worker`) |
| `budget:*` / `budget_stop:*` | > 80 % / 100 % Budget | Abwarten (Folgetag), ggf. `daily_budget` prüfen; Ursache für hohe Call-Zahl im Audit |
| `pending:*` | Session > 24 h offen | Flug in VF anlegen (kein Match) oder Session im UI ansehen |
| `write_errors:*` | ≥ 3 Fehler in Folge | Audit `error` lesen; VF erreichbar? Credentials? |
| `login:*` | 403 beim Login | Passwort/AppKey/2FA des technischen Users prüfen, Rotation (Kap. 5) |

## 5. Credential-Rotation / Kompromittierung (< 10 min)

1. In VF: Passwort des technischen Users ändern, AppKey neu erzeugen.
2. `set-credentials` (Abschnitt 2, Schritt 3) erneut ausführen.
3. Worker neu starten: `docker compose --profile vfsync restart vfsync`.
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
