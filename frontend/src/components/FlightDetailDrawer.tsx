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
import AlarmHandlingSection from './AlarmHandlingSection'
import { isAlarmHandlingRelevant } from '../lib/alarmHandling'

interface Props {
  flight: Flight | null
  airfieldSlug: string | null
  onClose: () => void
}

export default function FlightDetailDrawer({ flight, airfieldSlug, onClose }: Props) {
  const { isAuthenticated } = useAuth()
  // "Nicht mehr beobachten" (exclusion list): inline form state
  const [showIgnoreForm, setShowIgnoreForm] = useState(false)
  const [ignoreNote, setIgnoreNote] = useState('')
  const [ignoring, setIgnoring] = useState(false)
  const [ignoreError, setIgnoreError] = useState('')
  // Reset the inline form whenever a different flight is opened
  useEffect(() => {
    setShowIgnoreForm(false)
    setIgnoreNote('')
    setIgnoreError('')
  }, [flight?.flarmId])
  useEffect(() => {
    if (!flight) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [flight, onClose])

  if (!flight) return null

  /** Put the aircraft on the per-airfield exclusion list (ignored-aircraft).
   *  The worker drops its beacons and removes it from the live view itself. */
  async function handleIgnore() {
    if (!flight || !airfieldSlug) return
    const note = ignoreNote.trim()
    if (!note) {
      setIgnoreError('Bitte einen Hinweis angeben')
      return
    }
    setIgnoring(true)
    setIgnoreError('')
    try {
      // No fallback: on a foreign club's monitor the slug matches none of the
      // tenant's own airfields, and the entry must not land on the wrong list.
      const airfields = await api.get<{ id: string; slug: string }[]>('/airfields')
      const airfield = airfields.find((af) => af.slug === airfieldSlug)
      if (!airfield) {
        setIgnoreError('Kein Zugriff auf diesen Flugplatz')
        return
      }
      await api.post(`/airfields/${airfield.id}/ignored-aircraft`, {
        flarm_id: flight.flarmId,
        note,
      })
      onClose()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setIgnoreError('Steht bereits auf der Ausschlussliste')
      } else {
        setIgnoreError(e instanceof ApiError ? e.message : 'Speichern fehlgeschlagen')
      }
    } finally {
      setIgnoring(false)
    }
  }

  const takeoff = formatTime(flight.takeoffTime)
  const landing = formatTime(flight.landingTime)
  const duration = computeDuration(flight)
  const elapsedMin = flight.elapsedS > 0 ? Math.floor(flight.elapsedS / 60) : 0
  const takeoffAirfield = flight.takeoffAirfield || 'unbekannt'
  // "Landeplatz" only makes sense once the flight is on the ground.
  const landed = isLanded(flight)
  const landingAirfield = flight.landingAirfield
    || (flight.landingType === 'outlanding' ? 'Außenlandung' : '—')

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/60 z-40"
        onClick={onClose}
      />
      {/* Drawer: flex column so the header stays fixed and only the body scrolls */}
      <div
        className="fixed z-50 bg-tower-surface border-tower-border text-white flex flex-col
          inset-x-0 bottom-0 max-h-[92vh] supports-[height:100dvh]:max-h-[92dvh] rounded-t-2xl border-t
          md:inset-y-0 md:right-0 md:left-auto md:bottom-auto md:max-h-none md:max-w-md md:w-full md:rounded-none md:border-l md:border-t-0"
      >
        {/* Mobile drag handle */}
        <div className="md:hidden flex justify-center pt-2 pb-1 shrink-0">
          <div className="w-10 h-1 rounded-full bg-tower-border" />
        </div>

        {/* Header (stays visible while the body scrolls) */}
        <div className="shrink-0 px-4 py-2.5 border-b border-tower-border flex items-start justify-between gap-3">
          <div>
            <div className="text-xl font-bold leading-tight flex items-center gap-2 flex-wrap">
              <span>{flight.registration || flight.flarmId}</span>
              {flight.isVisitor && (
                <span
                  className="px-2 py-0.5 rounded bg-sky-900/60 border border-sky-700/60 text-sky-200
                    text-xs font-semibold uppercase tracking-wider align-middle"
                  title={`Besucher – gestartet in ${takeoffAirfield}`}
                >
                  Besucher
                </span>
              )}
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

        {/* Scrollable body */}
        <div className="flex-1 min-h-0 overflow-y-auto">

        {/* Status banner */}
        <div className={`px-4 py-1.5 text-sm font-semibold uppercase tracking-wider ${statusBg(flight.status)}`}>
          {statusLabel(flight.status)}
          {elapsedMin > 0 && flight.status !== 'landing' && (
            <span className="ml-2 font-normal">
              · letztes Signal vor {elapsedMin} min
            </span>
          )}
        </div>

        {landed ? (
          /* Landed: live values (QDR/speed/…) are stale – show the flight
             summary (takeoff, landing, duration) prominently instead. */
          <div className="px-4 py-2.5 grid grid-cols-3 gap-x-4 gap-y-2 border-b border-tower-border">
            <Stat label="Start" value={takeoff || '—'} hint={takeoff ? 'UTC' : ''} color="text-white" />
            <Stat label="Landung" value={landing || '—'} hint={landing ? 'UTC' : ''} color="text-green-400" />
            <Stat label="Flugzeit" value={duration || '—'} color="text-tower-qdr" />
          </div>
        ) : (
          /* Live data grid */
          <div className="px-4 py-2.5 grid grid-cols-3 gap-x-4 gap-y-2 border-b border-tower-border">
            <Stat label="QDR" value={`${flight.qdrDeg}°`} hint={flight.bearingText} color="text-white" />
            <Stat label="Distanz" value={`${(flight.distanceM / 1000).toFixed(1)} km`} color="text-tower-distance" />
            <Stat label="Höhe" value={`${flight.altitudeM} m`} hint={flight.altitudeAgl > 0 ? `${flight.altitudeAgl} AGL` : ''} color="text-tower-altitude" />
            <Stat label="Speed" value={`${flight.speedKmh} km/h`} color="text-gray-200" />
            <Stat label="Steigen" value={`${flight.verticalSpeedMs >= 0 ? '+' : ''}${flight.verticalSpeedMs.toFixed(1)} m/s`} color="text-gray-200" />
            <Stat label="Kurs" value={`${flight.trackDeg}°`} color="text-gray-200" />
          </div>
        )}

        {/* Timeline: two columns to keep the drawer short even with all rows.
            When landed, Start/Landung/Dauer already sit in the grid above. */}
        <div className="px-4 py-2.5 grid grid-cols-2 gap-x-6 gap-y-1 text-sm border-b border-tower-border">
          {!landed && <Row label="Start" value={takeoff} />}
          {!landed && landing && <Row label="Landung" value={landing} />}
          <Row label="Startplatz" value={takeoffAirfield} mono={false} />
          {landed && <Row label="Landeplatz" value={landingAirfield} mono={false} />}
          {!landed && duration && <Row label="Dauer" value={duration} />}
          {flight.launchType && <Row label="Startart" value={launchLabel(flight.launchType)} mono={false} />}
          {flight.maxAltitudeM > 0 && <Row label="Max. Höhe" value={`${flight.maxAltitudeM} m`} />}
          {flight.maxDistanceM > 0 && <Row label="Max. Distanz" value={`${(flight.maxDistanceM / 1000).toFixed(1)} km`} />}
        </div>

        {/* Last position + FLARM ID (one compact section) */}
        <div className="px-4 py-2 text-xs border-b border-tower-border space-y-1">
          {flight.latitude !== 0 && (
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-gray-500 uppercase tracking-wider shrink-0">Letzte Position</span>
              <a
                href={`https://www.google.com/maps?q=${flight.latitude},${flight.longitude}`}
                target="_blank"
                rel="noopener"
                title="In Google Maps öffnen"
                className="font-mono text-gray-200 hover:text-tower-qdr hover:underline text-right"
              >
                {flight.latitude.toFixed(5)}°N {flight.longitude.toFixed(5)}°E ↗
              </a>
            </div>
          )}
          <div className="flex items-baseline justify-between gap-3 text-gray-600">
            <span className="text-gray-500 uppercase tracking-wider shrink-0">FLARM-ID</span>
            <span className="font-mono">{flight.flarmId}</span>
          </div>
        </div>

        {/* Tower alarm handling (B2): state visible to everyone, actions need a login */}
        {isAlarmHandlingRelevant(flight) && (
          <AlarmHandlingSection flight={flight} airfieldSlug={airfieldSlug} />
        )}

        {/* Operations actions (only for logged-in airfield owners) */}
        {isAuthenticated && airfieldSlug && (
          <div className="px-4 py-2.5 border-t border-tower-border space-y-1.5">
            {!showIgnoreForm ? (
              <button
                onClick={() => { setShowIgnoreForm(true); setIgnoreError('') }}
                className="w-full bg-tower-bg hover:bg-white/10 border border-tower-border
                  text-gray-300 text-sm font-medium rounded-lg px-4 py-2 transition-colors"
              >
                Nicht mehr beobachten
              </button>
            ) : (
              <div className="bg-tower-bg border border-tower-border rounded-lg p-2 space-y-1.5">
                <p
                  title="Setzt das Flugzeug auf die Ausschlussliste – es wird an diesem Flugplatz nicht mehr beobachtet."
                  className="text-[10px] leading-tight text-gray-400 whitespace-nowrap overflow-hidden text-ellipsis"
                >
                  Setzt das Flugzeug auf die Ausschlussliste dieses Flugplatzes.
                </p>
                {ignoreError && (
                  <div className="text-red-300 text-xs">{ignoreError}</div>
                )}
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={ignoreNote}
                    onChange={(e) => setIgnoreNote(e.target.value.slice(0, 120))}
                    placeholder="Rettungshubschrauber Klinik"
                    required
                    maxLength={120}
                    autoFocus
                    className="flex-1 min-w-0 bg-tower-surface border border-tower-border rounded-lg
                      px-3 py-2 text-sm text-white placeholder-gray-600
                      focus:outline-none focus:border-tower-qdr"
                    aria-label="Hinweis"
                  />
                  <button
                    onClick={handleIgnore}
                    disabled={ignoring || !ignoreNote.trim()}
                    className="shrink-0 bg-tower-qdr hover:bg-cyan-500 text-white text-sm
                      font-semibold rounded-lg px-3 py-2 transition-colors disabled:opacity-50"
                  >
                    {ignoring ? 'Speichert…' : 'Bestätigen'}
                  </button>
                  <button
                    onClick={() => { setShowIgnoreForm(false); setIgnoreNote(''); setIgnoreError('') }}
                    disabled={ignoring}
                    aria-label="Abbrechen"
                    title="Abbrechen"
                    className="shrink-0 text-gray-400 hover:text-white text-xl leading-none
                      px-2.5 py-2 min-w-[40px] rounded-lg border border-tower-border
                      disabled:opacity-50"
                  >
                    ×
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        </div>{/* /Scrollable body */}
      </div>
    </>
  )
}

function Stat({ label, value, hint, color }: { label: string; value: string; hint?: string; color: string }) {
  return (
    <div>
      <div className="text-gray-500 text-xs uppercase tracking-wider">{label}</div>
      <div className={`font-mono font-bold text-base leading-tight ${color}`}>{value}</div>
      {hint && <div className="text-gray-500 text-[10px]">{hint}</div>}
    </div>
  )
}

function Row({ label, value, mono = true }: { label: string; value: string; mono?: boolean }) {
  // Long values (airfield names, "Außenlandung") must not wrap in the
  // two-column grid – ellipsize and expose the full text as a tooltip.
  return (
    <div className="flex justify-between gap-2 min-w-0">
      <span className="text-gray-500 shrink-0">{label}</span>
      <span
        className={`text-gray-100 text-right truncate min-w-0 ${mono ? 'font-mono' : ''}`}
        title={value}
      >
        {value}
      </span>
    </div>
  )
}

/** On the ground after a flight – at home, at a foreign field or in the field. */
function isLanded(f: Flight): boolean {
  if (f.status === 'landing' || f.status === 'ground' || f.status === 'outlanding') return true
  return !!f.landingTime
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
    case 'aerotow_ambiguous': return 'F-Schlepp (unsicher)'
    case 'self': return 'Eigenstart'
    case 'powered': return 'Motorflug'
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
