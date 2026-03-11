---
name: implement-websocket
description: Implementiere WebSocket-Server oder Frontend WebSocket-Client (Delta-Protokoll)
triggers:
  - "websocket"
  - "echtzeit"
  - "realtime"
  - "delta"
  - "push"
---

# WebSocket Implementierung (Delta-Protokoll)

## Kontext laden
1. Lies `feature.md` Section "WebSocket Echtzeit-Protokoll (Delta-basiert)"
2. Lies `frontend/src/types/flight.ts` fuer TypeScript-Typen

## Protokoll-Ueberblick

### Server -> Client Messages
1. **`full_state`** - Einmal bei Connect/Reconnect (alle Fluege)
2. **`flight_update`** - Delta nur mit geaenderten Feldern (alle 1-5s pro Flug)
3. **`flight_added`** - Neuer Flug erkannt (komplettes Flight-Objekt)
4. **`flight_removed`** - Flug archiviert (Landung/Timeout)
5. **`alarm`** - Alarm-Event (hoechste Prioritaet)
6. **`ping`** - Heartbeat alle 15s

### Client -> Server Messages
1. **`pong`** - Antwort auf ping (Pflicht!)

## Backend (FastAPI WebSocket)

### Endpoint: `/ws/monitor/{slug}`
```python
@router.websocket("/ws/monitor/{slug}")
async def ws_monitor(websocket: WebSocket, slug: str):
    await websocket.accept()

    # 1. Initial full_state aus Redis
    flights = await load_all_flights_from_redis(slug)
    await websocket.send_json({
        "type": "full_state",
        "flights": flights,
        "stats": calc_stats(flights)
    })

    # 2. Subscribe Redis PubSub
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"beacon:{slug}", f"event:{slug}")

    # 3. Forward-Loop
    try:
        async for message in pubsub.listen():
            # Read updated data from Redis, compute delta, send
            ...
    finally:
        await pubsub.unsubscribe()
```

### Delta-Berechnung
```python
def compute_delta(old: dict, new: dict) -> dict:
    """Nur geaenderte Felder zurueckgeben."""
    delta = {}
    for key, value in new.items():
        if old.get(key) != value:
            delta[key] = value
    return delta
```

## Frontend (React useWebSocket Hook)

### Delta-Merge Pattern
```typescript
function useMonitorWebSocket(slug: string) {
  const flights = useRef<Map<string, Flight>>(new Map())

  const onMessage = (msg: WsMessage) => {
    switch (msg.type) {
      case 'full_state':
        // Replace all flights
        flights.current = new Map(
          msg.flights.map(f => [f.flarmId, f])
        )
        break
      case 'flight_update':
        // Merge delta into existing flight
        const existing = flights.current.get(msg.flarmId)
        if (existing) {
          flights.current.set(msg.flarmId, { ...existing, ...msg.d })
        }
        break
      case 'flight_added':
        flights.current.set(msg.flight.flarmId, msg.flight)
        break
      case 'flight_removed':
        flights.current.delete(msg.flarmId)
        break
    }
  }
}
```

### Auto-Reconnect
- Bei Disconnect: Sofort reconnect-Versuch
- Exponential Backoff: 1s, 2s, 4s, 8s, max 30s
- Bei Reconnect: Server sendet automatisch `full_state`
- ConnectionStatus-Komponente zeigt Status an

### Heartbeat
- Server sendet `ping` alle 15s
- Client antwortet `pong`
- Kein pong nach 30s -> Server trennt Verbindung
- Client-seitig: Kein ping seit 20s -> Reconnect starten

## Checkliste
- [ ] feature.md WebSocket-Protokoll gelesen
- [ ] Delta-Berechnung auf Server-Seite
- [ ] Delta-Merge auf Client-Seite
- [ ] full_state bei Connect/Reconnect
- [ ] Heartbeat (Ping/Pong)
- [ ] Auto-Reconnect mit Exponential Backoff
- [ ] ConnectionStatus-Anzeige im Frontend
