---
name: implement-frontend-component
description: Implementiere React-Komponente (Tower Monitor, Karte, Alarm, etc.)
triggers:
  - "komponente"
  - "component"
  - "frontend"
  - "react"
  - "seite"
  - "page"
  - "monitor"
  - "karte"
  - "map"
---

# Frontend Komponenten Implementierung

## Kontext laden
1. Lies `feature.md` Section 5 (Monitor-Darstellung)
2. Lies `frontend/src/types/flight.ts` fuer Typen
3. Lies `frontend/tailwind.config.ts` fuer Tower-Farben

## Design-Prinzipien

### Tower-Optimiert
- **Dark Mode ist Default** (Klasse `dark` auf `<html>`)
- **QDR, Distanz, Hoehe sind PRIMAER** - groesste Schrift, prominenteste Position
- **Ablesbar aus 3 Metern Entfernung** auf dem Tower-Monitor
- **Farb-Schema:**
  - `text-tower-qdr` (Cyan #06b6d4) fuer Peilung/Distanz
  - `text-tower-altitude` (Gelb #eab308) fuer Hoehe
  - `text-tower-fly` (Blau #5697d6) fuer fliegende Flugzeuge
  - `text-tower-ldg` (Gruen #0cb300) fuer gelandete
  - `text-tower-alarm` (Rot #dc3545) fuer Alarm
  - `text-tower-emergency` (Neon-Rot #ff0040) fuer Emergency

### Tower-Schriftgroessen
```
text-tower-xl   = 2rem    (primaere Daten: QDR, Hoehe)
text-tower-2xl  = 2.5rem  (Alarm-Anzeige)
text-tower-3xl  = 3rem    (Emergency-Popup)
```

## Tech Stack
- React 19 + TypeScript (strict mode)
- TailwindCSS (kein CSS-in-JS!)
- Zustand fuer globalen State
- Custom Hooks fuer Logik
- Functional Components (keine Class Components)

## Zustand Store Pattern
```typescript
// store/monitorStore.ts
import { create } from 'zustand'

interface MonitorState {
  flights: Map<string, Flight>
  stats: FlightStats
  connectionStatus: 'connected' | 'disconnected' | 'reconnecting'
  updateFlight: (flarmId: string, delta: Partial<Flight>) => void
  setFullState: (flights: Flight[], stats: FlightStats) => void
}
```

## Component Patterns

### Flight Table Row
```tsx
function FlightRow({ flight }: { flight: Flight }) {
  return (
    <tr className={cn(
      "border-b border-tower-border",
      flight.status === 'alarm' && "bg-red-900/30 animate-pulse",
      flight.status === 'emergency' && "bg-red-900/50 animate-pulse",
    )}>
      <td className="text-tower-xl font-mono text-tower-qdr font-bold">
        {flight.qdrDeg}&deg; {flight.bearingText}
      </td>
      <td className="text-tower-xl font-mono text-tower-altitude font-bold">
        {flight.altitudeM}m
      </td>
      {/* ... */}
    </tr>
  )
}
```

## Disclaimer-Footer (PFLICHT!)
Jede Monitor-Seite muss den Disclaimer-Footer enthalten:
```tsx
<footer className="text-center text-gray-600 text-xs py-2">
  Assistenzsystem - ersetzt nicht die Pflichten des Flugleiters
</footer>
```

## Checkliste
- [ ] TypeScript strict mode
- [ ] TailwindCSS Klassen (keine inline styles)
- [ ] Tower-Farben aus tailwind.config.ts verwenden
- [ ] Responsive (Desktop primaer, Tablet sekundaer)
- [ ] Accessibility (aria-labels fuer interaktive Elemente)
- [ ] Disclaimer-Footer auf Monitor-Seiten
- [ ] Dark Mode als Default
