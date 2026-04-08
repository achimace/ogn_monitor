/**
 * Public Tower Monitor page.
 *
 * Dark mode, large display, designed for tower controller readability.
 * Connects via WebSocket for real-time delta updates.
 *
 * Views: Table | Map | Split (table left, map right)
 */
import { useParams } from 'react-router-dom'
import { useWebSocket } from '../hooks/useWebSocket'
import { useTodayPoll } from '../hooks/useTodayPoll'
import { useMonitorStore } from '../store/monitorStore'
import FlightTable from '../components/FlightTable'
import MapView from '../components/MapView'
import ConnectionStatus from '../components/ConnectionStatus'
import AlarmBanner from '../components/AlarmBanner'
import FlightDetailDrawer from '../components/FlightDetailDrawer'
import type { Flight } from '../types/flight'
import { useEffect, useState } from 'react'

type ViewMode = 'table' | 'map' | 'split'

export default function MonitorPage() {
  const { slug } = useParams<{ slug: string }>()
  const getCombinedFlights = useMonitorStore((s) => s.getCombinedFlights)
  const dayStats = useMonitorStore((s) => s.dayStats)
  // Subscribe to flights + archivedToday so combined list is reactive
  useMonitorStore((s) => s.flights)
  useMonitorStore((s) => s.archivedToday)
  const setSlug = useMonitorStore((s) => s.setSlug)
  const [clock, setClock] = useState(utcNow())
  const [view, setView] = useState<ViewMode>('table')
  const [selectedFlight, setSelectedFlight] = useState<Flight | null>(null)

  // Set slug and connect WebSocket
  useEffect(() => {
    if (slug) setSlug(slug)
  }, [slug, setSlug])

  useWebSocket(slug || null)
  useTodayPoll(slug || null)

  // Update clock every second
  useEffect(() => {
    const timer = setInterval(() => setClock(utcNow()), 1000)
    return () => clearInterval(timer)
  }, [])

  const allFlights = getCombinedFlights()

  return (
    <div className="min-h-screen bg-tower-bg text-gray-100 flex flex-col">
      {/* Header */}
      <header className="bg-tower-surface border-b border-tower-border px-6 py-3 shrink-0">
        <div className="flex items-center justify-between">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <h1 className="text-lg font-bold text-white">
              FLIGHTMONITOR
              {slug && <span className="text-gray-400 ml-2">- {slug.toUpperCase()}</span>}
            </h1>
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <StatPill label="Starts" value={dayStats.startsToday} color="text-tower-qdr" />
              <StatPill label="In der Luft" value={dayStats.inAir} color="text-tower-fly" />
              <StatPill label="Gelandet" value={dayStats.landedToday} color="text-green-400" />
              {dayStats.longest.durationS > 0 && (
                <span className="text-gray-500 text-xs">
                  Längster: <span className="text-white font-semibold">{dayStats.longest.reg}</span>
                  {' '}{formatDuration(dayStats.longest.durationS)}
                </span>
              )}
              {dayStats.highest.altitudeM > 0 && (
                <span className="text-gray-500 text-xs">
                  Höchster: <span className="text-white font-semibold">{dayStats.highest.reg}</span>
                  {' '}{dayStats.highest.altitudeM}m
                </span>
              )}
            </div>
          </div>

          <div className="flex items-center gap-4">
            {/* View toggle */}
            <div className="flex bg-tower-bg rounded-lg border border-tower-border overflow-hidden">
              <ViewButton label="Tabelle" active={view === 'table'} onClick={() => setView('table')} />
              <ViewButton label="Karte" active={view === 'map'} onClick={() => setView('map')} />
              <ViewButton label="Split" active={view === 'split'} onClick={() => setView('split')} />
            </div>
            <ConnectionStatus />
            <div className="text-gray-400 font-mono text-sm">{clock} UTC</div>
          </div>
        </div>
      </header>

      {/* Main content */}
      <div className="flex-1 flex flex-col min-h-0">
        <div className="p-4 shrink-0">
          <AlarmBanner />
        </div>

        {view === 'table' && (
          <div className="flex-1 overflow-auto px-4 pb-12">
            <FlightTable flights={allFlights} onSelect={setSelectedFlight} />
          </div>
        )}

        {view === 'map' && (
          <div className="flex-1 px-4 pb-12">
            <MapView flights={allFlights} />
          </div>
        )}

        {view === 'split' && (
          <div className="flex-1 flex gap-4 px-4 pb-12 min-h-0">
            <div className="w-1/2 overflow-auto">
              <FlightTable flights={allFlights} onSelect={setSelectedFlight} />
            </div>
            <div className="w-1/2">
              <MapView flights={allFlights} />
            </div>
          </div>
        )}
      </div>

      <FlightDetailDrawer
        flight={selectedFlight ? (allFlights.find(f => f.flarmId === selectedFlight.flarmId) || selectedFlight) : null}
        onClose={() => setSelectedFlight(null)}
      />

      {/* Disclaimer footer */}
      <footer className="fixed bottom-0 left-0 right-0 bg-tower-surface/80 backdrop-blur border-t border-tower-border py-1.5 text-center z-10">
        <p className="text-gray-600 text-xs">
          Assistenzsystem - ersetzt nicht die Pflichten des Flugleiters
        </p>
      </footer>
    </div>
  )
}

function ViewButton({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-1.5 text-xs font-medium transition-colors ${
        active ? 'bg-tower-qdr text-white' : 'text-gray-400 hover:text-white'
      }`}
    >
      {label}
    </button>
  )
}

function StatPill({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className={`font-bold ${color}`}>{value}</span>
      <span className="text-gray-500 text-xs">{label}</span>
    </div>
  )
}

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}h` : `${m}min`
}

function utcNow(): string {
  return new Date().toLocaleTimeString('de-DE', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    timeZone: 'UTC',
  })
}
