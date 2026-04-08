/**
 * Slide-in detail drawer for a single flight.
 *
 * Desktop: slides in from the right.
 * Mobile:  slides up from the bottom.
 */
import { useEffect, useState } from 'react'
import type { Flight } from '../types/flight'
import { api, ApiError } from '../api/client'
import { useAuth } from '../hooks/useAuth'

interface Props {
  flight: Flight | null
  airfieldSlug: string | null
  onClose: () => void
}

export default function FlightDetailDrawer({ flight, airfieldSlug, onClose }: Props) {
  const { isAuthenticated } = useAuth()
  const [dismissing, setDismissing] = useState(false)
  const [dismissError, setDismissError] = useState('')
  useEffect(() => {
    if (!flight) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [flight, onClose])

  if (!flight) return null

  async function handleDismiss() {
    if (!flight || !airfieldSlug) return
    const label = flight.registration || flight.flarmId
    if (!confirm(
      `${label} aus der Liste entfernen?\n\n` +
      `Der Flug wird ins Flugbuch archiviert und verschwindet ` +
      `bei allen Monitor-Nutzern aus der Live-Ansicht.`
    )) return
    setDismissing(true)
    setDismissError('')
    try {
      await api.post(`/monitor/${airfieldSlug}/flights/${flight.flarmId}/dismiss`)
      onClose()
    } catch (e) {
      setDismissError(e instanceof ApiError ? e.message : 'Entfernen fehlgeschlagen')
    } finally {
      setDismissing(false)
    }
  }

  const takeoff = formatTime(flight.takeoffTime)
  const landing = formatTime(flight.landingTime)
  const duration = computeDuration(flight)
  const elapsedMin = flight.elapsedS > 0 ? Math.floor(flight.elapsedS / 60) : 0

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/60 z-40"
        onClick={onClose}
      />
      {/* Drawer */}
      <div
        className="fixed z-50 bg-tower-surface border-tower-border text-white overflow-y-auto
          inset-x-0 bottom-0 max-h-[85vh] rounded-t-2xl border-t
          md:inset-y-0 md:right-0 md:left-auto md:bottom-auto md:max-h-none md:max-w-md md:w-full md:rounded-none md:border-l md:border-t-0"
      >
        {/* Mobile drag handle */}
        <div className="md:hidden flex justify-center pt-2 pb-1">
          <div className="w-10 h-1 rounded-full bg-tower-border" />
        </div>

        {/* Header */}
        <div className="px-5 py-4 border-b border-tower-border flex items-start justify-between gap-3">
          <div>
            <div className="text-2xl font-bold leading-tight">
              {flight.registration || flight.flarmId}
            </div>
            {flight.competitionSign && (
              <div className="text-tower-qdr text-sm font-mono">{flight.competitionSign}</div>
            )}
            {flight.aircraftModel && (
              <div className="text-gray-400 text-xs mt-0.5">{flight.aircraftModel}</div>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-white text-2xl leading-none px-2"
            aria-label="Schliessen"
          >
            ×
          </button>
        </div>

        {/* Status banner */}
        <div className={`px-5 py-2 text-sm font-semibold uppercase tracking-wider ${statusBg(flight.status)}`}>
          {statusLabel(flight.status)}
          {elapsedMin > 0 && flight.status !== 'landing' && (
            <span className="ml-2 font-normal">
              · letztes Signal vor {elapsedMin} min
            </span>
          )}
        </div>

        {/* Live data grid */}
        <div className="px-5 py-4 grid grid-cols-3 gap-4 border-b border-tower-border">
          <Stat label="QDR" value={`${flight.qdrDeg}°`} hint={flight.bearingText} color="text-white" />
          <Stat label="Distanz" value={`${(flight.distanceM / 1000).toFixed(1)} km`} color="text-tower-distance" />
          <Stat label="Höhe" value={`${flight.altitudeM} m`} hint={flight.altitudeAgl > 0 ? `${flight.altitudeAgl} AGL` : ''} color="text-tower-altitude" />
          <Stat label="Speed" value={`${flight.speedKmh} km/h`} color="text-gray-200" />
          <Stat label="Steigen" value={`${flight.verticalSpeedMs >= 0 ? '+' : ''}${flight.verticalSpeedMs.toFixed(1)} m/s`} color="text-gray-200" />
          <Stat label="Kurs" value={`${flight.trackDeg}°`} color="text-gray-200" />
        </div>

        {/* Timeline */}
        <div className="px-5 py-4 space-y-2 text-sm border-b border-tower-border">
          <Row label="Start" value={takeoff} />
          {landing && <Row label="Landung" value={landing} />}
          {duration && <Row label="Dauer" value={duration} />}
          {flight.launchType && <Row label="Startart" value={launchLabel(flight.launchType)} />}
          {flight.maxAltitudeM > 0 && <Row label="Max. Höhe" value={`${flight.maxAltitudeM} m`} />}
          {flight.maxDistanceM > 0 && <Row label="Max. Distanz" value={`${(flight.maxDistanceM / 1000).toFixed(1)} km`} />}
        </div>

        {/* Last position */}
        {flight.latitude !== 0 && (
          <div className="px-5 py-4 text-sm border-b border-tower-border">
            <div className="text-gray-500 text-xs uppercase tracking-wider mb-1">Letzte Position</div>
            <div className="font-mono text-gray-200">
              {flight.latitude.toFixed(5)}°N {flight.longitude.toFixed(5)}°E
            </div>
            <a
              href={`https://www.google.com/maps?q=${flight.latitude},${flight.longitude}`}
              target="_blank"
              rel="noopener"
              className="text-tower-qdr hover:underline text-xs inline-block mt-1"
            >
              In Google Maps öffnen ↗
            </a>
          </div>
        )}

        {/* Operations actions (only for logged-in airfield owners) */}
        {isAuthenticated && airfieldSlug && (
          <div className="px-5 py-4 border-t border-tower-border space-y-2">
            {dismissError && (
              <div className="text-red-300 text-xs">{dismissError}</div>
            )}
            <button
              onClick={handleDismiss}
              disabled={dismissing}
              className="w-full bg-red-900/40 hover:bg-red-900/60 border border-red-700/50
                text-red-200 text-sm font-medium rounded-lg px-4 py-2.5
                transition-colors disabled:opacity-50"
            >
              {dismissing ? 'Wird entfernt...' : 'Aus Liste entfernen'}
            </button>
            <p className="text-[10px] text-gray-600 text-center">
              Archiviert ins Flugbuch und blendet bei allen Monitoren aus.
            </p>
          </div>
        )}

        <div className="px-5 py-4 text-xs text-gray-600">
          FLARM-ID: <span className="font-mono">{flight.flarmId}</span>
        </div>
      </div>
    </>
  )
}

function Stat({ label, value, hint, color }: { label: string; value: string; hint?: string; color: string }) {
  return (
    <div>
      <div className="text-gray-500 text-xs uppercase tracking-wider">{label}</div>
      <div className={`font-mono font-bold text-lg leading-tight ${color}`}>{value}</div>
      {hint && <div className="text-gray-500 text-[10px]">{hint}</div>}
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between">
      <span className="text-gray-500">{label}</span>
      <span className="text-gray-100 font-mono">{value}</span>
    </div>
  )
}

function formatTime(iso: string | null): string {
  if (!iso) return ''
  return new Date(iso).toLocaleTimeString('de-DE', {
    hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
  })
}

function computeDuration(f: Flight): string {
  if (!f.takeoffTime) return ''
  const start = new Date(f.takeoffTime).getTime()
  const end = f.landingTime ? new Date(f.landingTime).getTime() : Date.now()
  if (!isFinite(start) || !isFinite(end) || end <= start) return ''
  const sec = Math.floor((end - start) / 1000)
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}h` : `${m} min`
}

function launchLabel(lt: string): string {
  switch (lt) {
    case 'winch': return 'Winde'
    case 'aerotow': return 'F-Schlepp'
    case 'self': return 'Eigenstart'
    default: return lt
  }
}

function statusLabel(s: string): string {
  switch (s) {
    case 'flying': return 'Fliegend'
    case 'towing': return 'Im Schlepp'
    case 'takeoff': return 'Gestartet'
    case 'landing': return 'Gelandet'
    case 'ground': return 'Am Boden'
    case 'signal_lost': return 'Kein Signal'
    case 'alarm': return 'ALARM - Vermisst'
    case 'emergency': return 'NOTFALL'
    case 'outlanding': return 'Aussenlandung'
    case 'outlanding_pending': return 'Aussenlandung?'
    case 'diverted': return 'Umgeleitet'
    default: return s
  }
}

function statusBg(s: string): string {
  if (s === 'alarm' || s === 'emergency') return 'bg-red-950/60 text-red-300'
  if (s === 'signal_lost') return 'bg-yellow-950/60 text-yellow-300'
  if (s === 'outlanding' || s === 'outlanding_pending' || s === 'diverted') return 'bg-orange-950/60 text-orange-300'
  if (s === 'towing') return 'bg-yellow-900/50 text-yellow-300'
  if (s === 'flying' || s === 'takeoff') return 'bg-blue-950/60 text-tower-fly'
  if (s === 'landing' || s === 'ground') return 'bg-green-950/40 text-green-300'
  return 'bg-tower-bg text-gray-300'
}
