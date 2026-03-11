---
name: implement-api-endpoint
description: Implementiere einen neuen REST API Endpoint (FastAPI)
triggers:
  - "neuer endpoint"
  - "api endpoint"
  - "REST route"
  - "CRUD"
---

# API Endpoint Implementierung

## Kontext laden
1. Lies `feature.md` Abschnitt "API-Endpoints" fuer die Spezifikation
2. Lies `feature.md` Abschnitt "Datenbank-Schema" fuer die relevanten Tabellen
3. Pruefe bestehende Endpoints in `app/api/` fuer Konsistenz

## Implementierungsschritte

### 1. Pydantic Models (Request/Response)
- Erstelle in der jeweiligen `app/api/<bereich>.py` Datei
- Request-Model mit Validation (Pydantic v2)
- Response-Model (nur Felder die der Client braucht)
- Beispiel:
```python
from pydantic import BaseModel, Field

class AirfieldCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    elevation_m: int = Field(..., ge=0, le=5000)
```

### 2. Router erstellen
```python
from fastapi import APIRouter, Depends, HTTPException, status

router = APIRouter(prefix="/api/airfields", tags=["airfields"])
```

### 3. Database Query
- IMMER parametrisierte Queries (NIEMALS String-Konkatenation!)
- asyncpg mit `$1, $2, $3` Parametern
- Connection aus Pool holen: `async with get_db().acquire() as conn:`

### 4. Error Handling
- HTTPException mit passenden Status Codes
- 400: Validation Error
- 401: Not Authenticated
- 403: Forbidden (falscher Mandant)
- 404: Not Found
- 409: Conflict (Duplicate)

### 5. Tenant-Isolation
- JEDE Query muss `WHERE tenant_id = $1` oder `WHERE airfield_id IN (SELECT id FROM airfields WHERE tenant_id = $1)` enthalten
- Tenant-ID kommt aus dem JWT Token via `get_current_user()` Dependency

### 6. Router registrieren
In `app/main.py`:
```python
from app.api.airfields import router as airfields_router
app.include_router(airfields_router)
```

## Checkliste
- [ ] Pydantic Models fuer Request und Response
- [ ] Parametrisierte SQL Queries
- [ ] Tenant-Isolation in allen Queries
- [ ] Error Handling mit passenden HTTP Status Codes
- [ ] Router in main.py registriert
- [ ] Docstring mit Beschreibung
