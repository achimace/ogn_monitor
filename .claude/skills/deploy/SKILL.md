---
name: deploy
description: Geprüftes Produktiv-Deployment des OGN Monitors auf flight-monitor.de über scripts/deploy.sh (Pre-Flight-Checks, Review, Deploy, Post-Check).
disable-model-invocation: true
argument-hint: "[--no-push | --skip-migrate]"
---

# Deploy nach Produktion (flight-monitor.de)

Das eigentliche Deployment macht **ausschließlich** `./scripts/deploy.sh`.
Dieser Skill ergänzt nur Prüfungen davor und danach. Das Script nicht verändern.

## 1. Pre-Flight (alles muss grün sein, sonst abbrechen und berichten)
1. `git status --short` leer und Branch `master` (`git rev-parse --abbrev-ref HEAD`).
   Uncommittete Änderungen → Nutzer fragen, nicht selbst `--dirty` verwenden.
2. Was geht raus: `git fetch origin && git log origin/master..HEAD --oneline`
   und `git diff --stat origin/master...HEAD`. Leer → nichts zu deployen.
3. Tests/Build (nur wenn betroffen):
   - Backend: `docker compose run --rm --no-deps api pytest -q` (falls `app/tests/` existiert)
   - Frontend: `cd frontend && npm run build`
4. Migrationen: Für jede neue/geänderte Datei in `db/migrations/` Idempotenz lokal prüfen
   (lokaler postgres muss laufen) – Datei **zweimal** ausführen:
   `docker compose exec -T postgres psql -U ogn_monitor -d ogn_monitor -v ON_ERROR_STOP=1 -f - < db/migrations/<datei>`
5. Nginx: Enthält der Diff `nginx/nginx.conf` oder `nginx.prod.conf`? → **Stopp**, Nutzer
   warnen: auf dem Server ist `nginx.conf` lokal überschrieben, `git pull --ff-only`
   schlägt fehl. Vorgehen mit dem Nutzer abstimmen.
6. Agent `reviewer` auf `origin/master...HEAD` ansetzen. Bei BLOCKER abbrechen.

## 2. Freigabe
Kurze Zusammenfassung an den Nutzer (Commits, Migrationen, Risiken) und ausdrücklich
nach „deployen?“ fragen. Ohne Ja kein Deploy.

## 3. Deploy
`./scripts/deploy.sh $ARGUMENTS`
(führt git push, git pull auf dem Server, alle SQL-Migrationen, `docker compose up -d --build`
und Health-Check aus).

## 4. Post-Check
- Health: `curl -fsS https://flight-monitor.de/health`
- Container/Worker-Singleton und Logs (per SSH, Konfig aus `.deploy.env` wird vom Script genutzt;
  Host `flight-monitor.de`, Pfad `/opt/ogn_monitor`):
  `ssh root@flight-monitor.de "cd /opt/ogn_monitor && docker compose ps && docker compose logs --tail=30 worker api"`
  → genau **ein** worker-Container, APRS verbunden, keine Tracebacks.
- Ergebnis kurz berichten. Bei Fehler: Logs zeigen, **kein** eigenmächtiges Rollback,
  Nutzer entscheiden lassen (Rollback = `git revert` + erneutes `/deploy`).
