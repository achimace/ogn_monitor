---
name: implement-aprs-client
description: Implementiere oder erweitere den APRS-IS Client (OGN-Verbindung)
triggers:
  - "aprs"
  - "ogn"
  - "beacon"
  - "aprs client"
  - "ogn verbindung"
---

# APRS-IS Client Implementierung

## Kontext laden
1. Lies `feature.md` Section 2 (OGN-Datenanbindung)
2. Lies `feature.md` Section 7b (OGN-Verbindungs-Resilience)
3. Lies `app/app/aprs/` fuer bestehenden Code

## APRS-IS Protokoll

### Verbindung
```
Server: aprs.glidernet.org:14580
Login:  user OGNMON pass -1 vers OGNMonitor 0.1
Filter: r/47.5/11.5/500
```

### Beacon-Format (OGN-spezifisch)
```
ICA000239>APRS,qAS,EDMO:/142345h4738.12N/01119.45E'/A=006890 !W72! id06000239
  +198fpm +0.3rot 14.2dB 0e -0.2kHz gps3x4
```

Felder:
- Position: Lat/Lon im APRS-Format (DDMM.MM)
- Altitude: Feet nach `/A=`
- FLARM-ID: nach `id06` (6 Hex-Zeichen)
- Climb rate: `+198fpm` (feet per minute)
- Turn rate: `+0.3rot` (halbe Umdrehungen pro 2 Min)
- Signal: `14.2dB`

### Filter-Syntax
```
r/LAT/LON/RADIUS_KM    Geo-Filter (max 9 pro Verbindung)
```

## Architektur-Regeln

### SINGLETON - Nur 1 Instanz!
- OGN erlaubt nur eine Verbindung pro Callsign
- Worker laeuft mit `deploy.replicas: 1` in docker-compose.yml
- Niemals parallel starten!

### Resilience (KRITISCH)
- Auto-Reconnect mit Exponential Backoff (1s -> 300s max)
- Beacon-Timeout: Keine Daten seit 120s -> Verbindung als tot betrachten
- Keepalive: Alle 60s `# keepalive` senden
- Health-Status in Redis: `HSET ogn:health connected true/false ...`

### Dynamische Filter (Multi-Tenant)
- Filter werden aus aktiven Flugplaetzen generiert
- `filter_builder.py` konsolidiert ueberlappende Regionen
- Bei Flugplatz-Aenderung: Filter-Update an APRS senden

## Implementation Pattern
```python
class APRSClient:
    async def run(self):
        delay = 1
        while not self.shutdown:
            try:
                await self._connect_and_stream()
                delay = 1  # Reset on success
            except (ConnectionError, asyncio.TimeoutError):
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def _connect_and_stream(self):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(server, port), timeout=10)
        await self._login(writer)
        while True:
            line = await asyncio.wait_for(
                reader.readline(), timeout=120)  # Beacon timeout
            if line.startswith(b"#"):
                continue  # Comment/keepalive
            await self._process_beacon(line.decode())
```

## Checkliste
- [ ] feature.md Section 2 + 7b gelesen
- [ ] Singleton-Constraint beachtet
- [ ] Auto-Reconnect mit Exponential Backoff
- [ ] Beacon-Timeout-Erkennung (120s)
- [ ] Health-Status in Redis geschrieben
- [ ] Filter dynamisch aus Airfield-Config generiert
- [ ] Beacons an flight_tracker weitergeleitet
