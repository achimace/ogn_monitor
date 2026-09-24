---
name: implement-redis-hot-state
description: Arbeite mit Redis Hot State (Flight-Daten, PubSub, Streams, Caching)
triggers:
  - "redis"
  - "hot state"
  - "cache"
  - "pubsub"
---

# Redis Hot State Implementierung

## Kontext laden
1. Lies `feature.md` Section 3b (Redis Hot State Strategie)
2. Lies `app/app/redis_client.py` fuer Connection

## Redis-Schema

### Live-Flugstatus (Hash pro Flug)
```
Key:    flight:{airfield_slug}:{flarm_id}
Type:   HASH
TTL:    86400 (24h Sicherheits-Cleanup)

Felder: flarm_id, registration, aircraft_model, competition_sign,
        status, latitude, longitude, altitude_m, altitude_agl,
        speed_kmh, vertical_speed_ms, track_deg,
        distance_m, qdr_deg, bearing_text,
        takeoff_time, last_seen, max_altitude_m, max_distance_m,
        launch_type, tow_plane_reg, release_alt_m
```

### Flug-Index pro Flugplatz (Set)
```
Key:    flights:{airfield_slug}
Type:   SET
Values: FLARM-IDs aller aktiven Fluege
```

### Aktive Flugplaetze (Set)
```
Key:    active_airfields
Type:   SET
Values: Airfield-Slugs mit mindestens 1 aktivem Flug
```

### OGN Health (Hash)
```
Key:    ogn:health
Type:   HASH
Felder: connected, last_beacon_age_s, beacons_per_minute,
        reconnect_count, uptime_s, status
```

### PubSub Channels (Notifications)
```
beacon:{airfield_slug}   -> Bei jedem Beacon-Update (fuer API WebSocket)
event:{airfield_slug}    -> Bei Struktur-Events (flight_added, flight_removed, alarm)
```

## Worker schreibt, API liest

### Worker (Schreiben)
```python
# Bei jedem Beacon:
await redis.hset(f"flight:{slug}:{flarm_id}", mapping={...})
await redis.expire(f"flight:{slug}:{flarm_id}", 86400)
await redis.sadd(f"flights:{slug}", flarm_id)
await redis.publish(f"beacon:{slug}", json.dumps({"flarm_id": flarm_id, "type": "update"}))

# Bei neuem Flug:
await redis.publish(f"event:{slug}", json.dumps({"flarm_id": flarm_id, "type": "added"}))

# Bei Alarm:
await redis.publish(f"event:{slug}", json.dumps({"flarm_id": flarm_id, "type": "alarm", ...}))
```

### API (Lesen)
```python
# Alle Fluege eines Flugplatzes:
flarm_ids = await redis.smembers(f"flights:{slug}")
pipe = redis.pipeline()
for fid in flarm_ids:
    pipe.hgetall(f"flight:{slug}:{fid}")
flights = await pipe.execute()

# PubSub fuer WebSocket:
pubsub = redis.pubsub()
await pubsub.subscribe(f"beacon:{slug}", f"event:{slug}")
```

## WICHTIG: volatile-lru Policy
- Redis hat `maxmemory-policy volatile-lru`
- NUR Keys mit TTL werden bei Memory-Knappheit evicted
- Flight-Hashes haben TTL (86400) -> koennen evicted werden
- Deshalb: IMMER pruefen ob Key existiert bevor darauf zugegriffen wird!
- Index-Sets (flights:*) haben KEIN TTL -> bleiben immer erhalten
  -> Regelmaessig bereinigen: Verwaiste FLARM-IDs aus Sets entfernen

## Checkliste
- [ ] feature.md Section 3b gelesen
- [ ] Worker schreibt, API liest (nie umgekehrt!)
- [ ] TTL auf allen Flight-Hashes setzen
- [ ] PubSub fuer Notifications (nicht fuer Daten!)
- [ ] Pipeline fuer Bulk-Reads verwenden
- [ ] Null-Checks bei Redis-Reads (Key koennte evicted sein)
