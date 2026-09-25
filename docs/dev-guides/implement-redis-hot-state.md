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

### Flugspur pro Luftfahrzeug (Stream)
```
Key:    track:{airfield_slug}:{flarm_id}
Type:   STREAM, Entry-ID = Beacon-Zeit in ms ("{ts_ms}-*"), damit XRANGE nach Zeit geht
TTL:    settings.track_retention_s (86400 = 24h), gleitend - jeder Write erneuert
MAXLEN: ~ track_retention_s / track_min_interval_s (approximate, Speicher-Guard)

Felder: lat, lon, alt (MSL m), agl (m), speed (km/h), vs (m/s), track (deg)
        - alle als Strings wie im positions:-Stream

Ausduennung: der Worker schreibt nur, wenn seit dem letzten Punkt dieses
LFZ mindestens settings.track_min_interval_s (5 s) Beacon-Zeit vergangen ist
(FlightTracker._last_track_ts). Out-of-order-Beacons (ID <= letzter Eintrag)
werden verworfen (ResponseError "equal or smaller than the target stream top
item", debug-Log); jeder andere ResponseError wird weitergereicht.

Gelesen von GET /api/monitor/{slug}/flights/{flarm_id}/track?hours=24
(XRANGE track:... {since_ms}-0 +; Obergrenze von hours = track_retention_s/3600).

Einschraenkung: Weil die Beacon-Millisekunden die Stream-ID sind, hat die Spur
nach einem beacon_timeline_reset der State Machine (Empfaenger-Uhr lief vor)
eine Luecke: Beacons mit Zeit <= Top-ID des Streams werden als out-of-order
verworfen, bis die Beacon-Zeit die Top-ID wieder ueberholt hat.
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
