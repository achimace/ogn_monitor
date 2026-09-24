---
name: implement-flight-logic
description: Implementiere Flight Tracking Logik (State Machine, Profile Analysis, Launch Detection)
triggers:
  - "flight logic"
  - "state machine"
  - "flugzustand"
  - "tracking"
  - "startart"
  - "flugprofil"
---

# Flight Logic Implementierung

## Kontext laden (PFLICHT!)
1. Lies `feature.md` komplett - dort stehen ALLE Details zu:
   - Flight State Machine (Section 4)
   - Flugprofil-Analyse (Section 4a)
   - Startart-Erkennung (Section 4b)
2. Lies `app/app/tracking/` fuer bestehenden Code
3. Lies `app/app/config.py` fuer Schwellwerte

## Architektur-Regeln (KRITISCH!)

### Flight Logic gehoert in den WORKER, nicht in die API!
- Alle Dateien in `app/tracking/` werden NUR vom Worker ausgefuehrt
- Der Worker hat In-Memory State (Python Dicts) - kein DB-Query pro Beacon!
- Ergebnisse gehen nach Redis (HSET + PUBLISH), nicht nach PostgreSQL

### Datenfluss
```
APRS Beacon -> beacon_parser -> flight_tracker -> state_machine -> Redis
                                     |                |
                                     v                v
                              launch_detector   profile_analyzer
```

## Flight States (numerisch, siehe init.sql)
```
0 = GROUND        (am Boden, kein aktiver Flug)
1 = TAKEOFF       (Start erkannt, erste Beacons)
2 = FLYING        (normaler Flug)
3 = LANDING       (Landung am Heimatplatz)
4 = OUTLANDING    (Aussenlandung bestaetigt)
5 = ALARM         (kein Signal, Zeitlimit ueberschritten)
6 = TOWING        (im F-Schlepp)
7 = OUTLANDING_PENDING  (Verdacht auf Aussenlandung)
8 = EMERGENCY     (abnormales Flugprofil erkannt)
9 = DIVERTED      (auf anderem Flugplatz gelandet)
10 = SIGNAL_LOST  (Signal verloren, Ursache unklar)
```

## Geo-Berechnungen
- IMMER `geo_calc.py` verwenden
- QDR: Azimut vom Heimatplatz zum Flugzeug (0-360 Grad)
- Distanz: Haversine-Formel
- Himmelsrichtung: 16-Punkt-Kompass (N, NNO, NO, ONO, ...)
- ACHTUNG: Geo-Berechnungen sind CPU-bound!

## Redis Hot State Schema
```python
await redis.hset(f"flight:{airfield_slug}:{flarm_id}", mapping={
    "flarm_id": flarm_id,
    "registration": registration,
    "status": str(status_code),
    "latitude": str(lat),
    "longitude": str(lon),
    "altitude_m": str(alt),
    "qdr_deg": str(qdr),
    "bearing_text": bearing,
    "distance_m": str(dist),
    # ... alle weiteren Felder
})
await redis.publish(f"beacon:{airfield_slug}", json.dumps({
    "flarm_id": flarm_id,
    "type": "update"  # oder "added", "removed", "alarm"
}))
```

## F-Schlepp Paar-Erkennung (3 Kriterien!)
1. Distanz < 150m (Seil-Laenge)
2. Hoehendifferenz < 80m
3. **Kurs-Abweichung < 20 Grad** (verhindert False Positives bei parallelen Starts!)

## Checkliste
- [ ] feature.md gelesen fuer die relevante Section
- [ ] Code lebt in app/tracking/ (nicht in app/api/)
- [ ] In-Memory State, keine DB-Queries im Hot Path
- [ ] Redis HSET + PUBLISH nach jedem State Update
- [ ] Alle Schwellwerte aus config.py oder airfield-Config
- [ ] Type Hints ueberall
- [ ] Docstrings mit Erklaerung der Fluglogik
