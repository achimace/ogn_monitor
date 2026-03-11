/**
 * Public Tower Monitor page.
 *
 * Dark mode, large display, designed for tower controller readability.
 * Connects via WebSocket for real-time delta updates.
 */
import { useParams } from 'react-router-dom'
import { useWebSocket } from '../hooks/useWebSocket'
import { useMonitorStore } from '../store/monitorStore'
import FlightTable from '../components/FlightTable'
import ConnectionStatus from '../components/ConnectionStatus'
import AlarmBanner from '../components/AlarmBanner'
import { useEffect, useState } from 'react'

export default function MonitorPage() {
  const { slug } = useParams<{ slug: string }>()
  const flights = useMonitorStore((s) => s.flights)
  const stats = useMonitorStore((s) => s.stats)
  const setSlug = useMonitorStore((s) => s.setSlug)
  const [clock, setClock] = useState(utcNow())

  // Set slug and connect WebSocket
  useEffect(() => {
    if (slug) setSlug(slug)
  }, [slug, setSlug])

  useWebSocket(slug || null)

  // Update clock every second
  useEffect(() => {
    const timer = setInterval(() => setClock(utcNow()), 1000)
    return () => clearInterval(timer)
  }, [])

  const allFlights = Array.from(flights.values())

  return (
    <div className="min-h-screen bg-tower-bg text-gray-100">
      {/* Header */}
      <header className="bg-tower-surface border-b border-tower-border px-6 py-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <h1 className="text-lg font-bold text-white">
              FLIGHTMONITOR
              {slug && <span className="text-gray-400 ml-2">- {slug.toUpperCase()}</span>}
            </h1>
            <div className="flex items-center gap-3 text-sm">
              <StatPill label="Fliegend" value={stats.flying} color="text-tower-fly" />
              <StatPill label="Gelandet" value={stats.landed} color="text-green-400" />
              {stats.alarm > 0 && <StatPill label="Alarm" value={stats.alarm} color="text-red-400" />}
              {stats.outlanding > 0 && <StatPill label="Aussen" value={stats.outlanding} color="text-orange-400" />}
            </div>
          </div>
          <div className="flex items-center gap-6">
            <ConnectionStatus />
            <div className="text-gray-400 font-mono text-sm">{clock} UTC</div>
          </div>
        </div>
      </header>

      {/* Main content */}
      <div className="p-4 max-w-[1600px] mx-auto">
        <AlarmBanner />
        <FlightTable flights={allFlights} />
      </div>

      {/* Disclaimer footer */}
      <footer className="fixed bottom-0 left-0 right-0 bg-tower-surface/80 backdrop-blur border-t border-tower-border py-1.5 text-center">
        <p className="text-gray-600 text-xs">
          Assistenzsystem - ersetzt nicht die Pflichten des Flugleiters
        </p>
      </footer>
    </div>
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

function utcNow(): string {
  return new Date().toLocaleTimeString('de-DE', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    timeZone: 'UTC',
  })
}
