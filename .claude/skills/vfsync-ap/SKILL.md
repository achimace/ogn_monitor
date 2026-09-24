---
name: vfsync-ap
description: Ein Arbeitspaket (AP-0 bis AP-13) der Vereinsflieger-Integration (VF-Sync) gemäß docs/konzept-vf-sync.md umsetzen – mit Branch, Tests, Review und PR-Notizen. Nutzen bei "VF-Sync", "Vereinsflieger", "AP-<n>".
argument-hint: "AP-<n>"
---

# VF-Sync Arbeitspaket $ARGUMENTS umsetzen

Spezifikation: `docs/konzept-vf-sync.md` (gewinnt bei Widersprüchen zum Code-Ist-Stand,
außer bei der Migrationstechnik – siehe CLAUDE.md: SQL-Migration statt Alembic).

## Ablauf
1. Kap. 0 (Regeln) und Kap. 10 (AP-Tabelle) lesen; Abhängigkeiten von $ARGUMENTS prüfen.
   Vorgänger nicht erledigt → stoppen und dem Nutzer sagen.
   Stand ermitteln: `git branch -a | grep vfsync`, `git log --oneline | grep -i vfsync`.
2. Nur die für das AP relevanten Kapitel lesen (Komponenten Kap. 4, Adapter Kap. 5,
   Härtungen Kap. 6, Sicherheit Kap. 8, Tests Kap. 9).
3. Branch: `git checkout -b feat/vfsync-<ap-kleingeschrieben>` von `master`.
4. Umsetzung an Agent `backend` delegieren (bei AP-10-Frontend zusätzlich `frontend`),
   mit klarer Aufgabe: Ziel, Dateien, Invarianten, erwartete Tests.
5. Tests zuerst oder parallel; `docker compose run --rm --no-deps api pytest -q` grün.
6. Agent `reviewer` über den Branch-Diff laufen lassen; BLOCKER beheben.
7. Commit(s) auf dem Branch. PR-/Abschlussnotiz mit Abschnitten
   **Umgesetzt**, **Tests**, **SPEC-DEVIATION**, **OPEN-QUESTION**.

## Nicht verhandelbar
- **Nie gegen die echte Vereinsflieger-API entwickeln oder testen** – nur gegen den Mock (AP-2).
  Ausnahme: AP-12-Spike, nur nach ausdrücklicher Freigabe durch den Nutzer.
- Schreib-Invarianten Kap. 5.4 sind testgesichert; Tests nie lockern.
- `dry_run` default an, `enabled` default aus; keine Selbstaktivierung.
- Keine Secrets in Code/Tests/Fixtures/Logs; VF-Credentials nur verschlüsselt (Fernet, `VFSYNC_CRED_KEY`).
- Bestandscode nur an den in Kap. 6 benannten Härtungspunkten ändern.
- Neuer Compose-Service `vfsync` (gleiches Image, `python -m app.vfsync`) – APRS-Worker bleibt unberührt Singleton.
- Kein Deploy aus diesem Skill; Merge nach `master` und `/deploy` entscheidet der Nutzer.
