---
name: db-migration
description: Neue PostgreSQL-Schemaänderung für den OGN Monitor anlegen – als idempotente SQL-Migration in db/migrations/ plus Nachtrag in db/init.sql, lokal zweifach getestet. Nutzen bei jeder Tabellen-/Spalten-/Index-/Constraint-Änderung.
---

# Idempotente DB-Migration anlegen

Hintergrund: `scripts/deploy.sh` führt bei **jedem** Deploy **alle** `db/migrations/*.sql`
der Reihe nach mit `ON_ERROR_STOP=1` aus. Eine nicht-idempotente Migration bricht
also spätestens den nächsten Deploy. Alembic wird nicht ausgeführt.

## Schritte
1. Nächste Nummer: `ls db/migrations/` → `NNN_<kurzbeschreibung>.sql` (dreistellig, fortlaufend).
2. Kopfkommentar: Zweck, „Idempotent.“ – Stil wie `005_flight_log_dedup.sql`.
3. Nur idempotente Konstrukte:
   - `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`
   - `ALTER TABLE … ADD COLUMN IF NOT EXISTS`
   - Constraints/Umbenennungen/Datenkorrekturen in `DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '…') THEN … END IF; END $$;`
   - PostGIS-Spalten: vorher in `information_schema.columns` prüfen.
   - Keine `DROP`/`DELETE` ohne ausdrückliche Freigabe durch den Nutzer.
4. Dieselbe Änderung in `db/init.sql` einarbeiten (Neuinstallation muss dem Endstand entsprechen).
5. Lokal testen (postgres läuft) – **zweimal**:
   ```bash
   for i in 1 2; do docker compose exec -T postgres psql -U ogn_monitor -d ogn_monitor -v ON_ERROR_STOP=1 -f - < db/migrations/NNN_x.sql; done
   ```
6. Code anpassen (`app/app/db/queries.py`, Pydantic-Schemas, ggf. `state_synchronizer.py`).
   Neue Spalten mit Default, damit alter Code während des Rollouts weiterläuft.
7. Separater Commit: `DB: <was> (migration NNN)`.

Für VF-Sync: Rollen/Grants aus `docs/konzept-vf-sync.md` Kap. 8.3 ebenfalls idempotent
(`DO $$ … IF NOT EXISTS (SELECT 1 FROM pg_roles …)`), Passwörter nie in der SQL-Datei.
