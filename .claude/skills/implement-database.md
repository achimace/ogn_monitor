---
name: implement-database
description: Arbeite mit PostgreSQL (Queries, Migrations, Schema-Aenderungen)
triggers:
  - "datenbank"
  - "database"
  - "migration"
  - "sql"
  - "query"
  - "schema"
---

# Datenbank-Implementierung

## Kontext laden
1. Lies `db/init.sql` fuer das aktuelle Schema
2. Lies `feature.md` Section 3 fuer das geplante Schema
3. Lies `app/app/db/connection.py` fuer den Connection Pool

## Regeln (KRITISCH!)

### PostgreSQL ist COLD STORAGE
- Live-Flugdaten gehoeren in Redis, NICHT in PostgreSQL!
- PG-Writes nur bei:
  - Statuswechsel (Takeoff, Landing, Alarm)
  - Periodischer Bulk-Sync (alle 30s aus Redis)
  - Mandanten/Airfield CRUD
  - Flight-Log Archivierung
  - Aircraft Registry Sync (taeglich)

### NIEMALS String-Konkatenation in SQL!
```python
# FALSCH - SQL Injection!
await conn.execute(f"SELECT * FROM airfields WHERE slug = '{slug}'")

# RICHTIG - Parametrisierte Query
await conn.execute("SELECT * FROM airfields WHERE slug = $1", slug)
```

### Tenant-Isolation in JEDER Query
```python
# IMMER tenant_id filtern:
await conn.fetch(
    "SELECT * FROM airfields WHERE tenant_id = $1 AND id = $2",
    tenant_id, airfield_id
)
```

## asyncpg Pattern
```python
from app.db.connection import get_db

async def get_airfield(tenant_id: str, airfield_id: str):
    pool = get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM airfields WHERE tenant_id = $1 AND id = $2",
            tenant_id, airfield_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Airfield not found")
        return dict(row)
```

## Alembic Migrationen
```bash
# Neue Migration erstellen
cd app && alembic revision --autogenerate -m "add_column_xyz"

# Migration ausfuehren
cd app && alembic upgrade head
```

## Checkliste
- [ ] Parametrisierte Queries ($1, $2, ...)
- [ ] Tenant-Isolation (WHERE tenant_id = $1)
- [ ] Connection Pool verwenden (get_db())
- [ ] async with pool.acquire() as conn
- [ ] Fehlerbehandlung (Not Found -> 404)
- [ ] Alembic Migration bei Schema-Aenderungen
- [ ] Kein Live-Flugdaten in PG (gehoert in Redis!)
