/**
 * Tower Monitor flight table.
 *
 * Design: QDR, Distance, Height are the largest elements.
 * Flight controller must read these from 3 meters away.
 *
 * Sections: NOTFALL > ALARM > FLIEGEND > IM SCHLEPP > GELANDET
 */
import type { Flight } from '../types/flight'
import StatusBadge from './StatusBadge'
import ClimbIndicator from './ClimbIndicator'

interface FlightTableProps {
  flights: Flight[]
}

export default function FlightTable({ flights }: FlightTableProps) {
  // Sort by takeoff time descending (latest first), then group by status
  const sorted = [...flights].sort((a, b) =>
    (b.takeoffTime || '').localeCompare(a.takeoffTime || '')
  )
  const emergency = sorted.filter((f) => f.status === 'emergency')
  const alarm = sorted.filter((f) => ['alarm', 'signal_lost'].includes(f.status))
  const towing = sorted.filter((f) => f.status === 'towing')
  const flying = sorted.filter((f) => f.status === 'flying')
  const outlanding = sorted.filter((f) => ['outlanding', 'outlanding_pending', 'diverted'].includes(f.status))
  const landed = sorted.filter((f) => ['landing', 'ground'].includes(f.status))

  return (
    <div className="space-y-3">
      {emergency.length > 0 && (
        <FlightSection title="NOTFALL" count={emergency.length} flights={emergency} color="red" blink />
      )}
      {alarm.length > 0 && (
        <FlightSection title="ALARM" count={alarm.length} flights={alarm} color="red" />
      )}
      {outlanding.length > 0 && (
        <FlightSection title="AUSSENLANDUNG" count={outlanding.length} flights={outlanding} color="orange" />
      )}
      {towing.length > 0 && (
        <FlightSection title="IM SCHLEPP" count={towing.length} flights={towing} color="yellow" />
      )}
      {flying.length > 0 && (
        <FlightSection title="FLIEGEND" count={flying.length} flights={flying} color="blue" />
      )}
      {landed.length > 0 && (
        <FlightSection title="GELANDET" count={landed.length} flights={landed} color="green" />
      )}
      {flights.length === 0 && (
        <div className="text-center text-gray-500 py-16 text-lg">
          Keine Fluege aktiv
        </div>
      )}
    </div>
  )
}

function FlightSection({ title, count, flights, color, blink }: {
  title: string; count: number; flights: Flight[]; color: string; blink?: boolean
}) {
  const colorMap: Record<string, string> = {
    red: 'text-red-400 border-red-800',
    orange: 'text-orange-400 border-orange-800',
    yellow: 'text-yellow-400 border-yellow-800',
    blue: 'text-tower-fly border-blue-900',
    green: 'text-green-400 border-green-900',
  }
  const bgMap: Record<string, string> = {
    red: 'bg-red-950/40',
    orange: 'bg-orange-950/30',
    yellow: 'bg-yellow-950/20',
    blue: 'bg-[#16213e]/60',
    green: 'bg-[#1b4332]/30',
  }

  return (
    <div>
      <div className={`flex items-center gap-3 mb-2 ${blink ? 'animate-pulse' : ''}`}>
        <h3 className={`text-sm font-bold uppercase tracking-wider ${colorMap[color]?.split(' ')[0]}`}>
          {title} ({count})
        </h3>
        <div className={`flex-1 h-px ${colorMap[color]?.split(' ')[1] || 'border-tower-border'} border-t`} />
      </div>

      <div className="space-y-1">
        {flights.map((flight) => (
          <FlightRow key={flight.flarmId} flight={flight} bgColor={bgMap[color] || ''} />
        ))}
      </div>
    </div>
  )
}

function FlightRow({ flight, bgColor }: { flight: Flight; bgColor: string }) {
  const takeoffStr = flight.takeoffTime
    ? new Date(flight.takeoffTime).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })
    : '-'

  const distKm = (flight.distanceM / 1000).toFixed(1)
  const launchLabel = formatLaunchType(flight.launchType)
  const elapsedMin = flight.elapsedS > 0 ? Math.floor(flight.elapsedS / 60) : undefined

  return (
    <div className={`${bgColor} rounded-lg border border-tower-border/30 px-4 py-3 flex items-center gap-4 hover:brightness-110 transition-all`}>
      {/* Column 1: Aircraft info */}
      <div className="w-24 shrink-0">
        <div className="text-white font-bold text-sm">{flight.registration || flight.flarmId}</div>
        <div className="text-gray-400 text-xs">{flight.competitionSign || ''}</div>
        <div className="text-gray-500 text-[10px]">{flight.aircraftModel || ''}</div>
      </div>

      {/* Column 2: Time + Launch type */}
      <div className="w-16 shrink-0 text-center">
        <div className="text-gray-300 text-sm font-mono">{takeoffStr}</div>
        {launchLabel && <div className="text-gray-500 text-[10px] mt-0.5">{launchLabel}</div>}
      </div>

      {/* Column 3: PRIMARY - QDR, Distance, Height */}
      <div className="flex-1 flex items-center gap-6">
        {/* QDR */}
        <div className="text-center">
          <div className="text-white font-mono font-bold text-tower-xl leading-none">
            {flight.qdrDeg}&deg;
          </div>
          <div className="text-gray-500 text-xs mt-0.5">{flight.bearingText}</div>
        </div>

        {/* Distance */}
        <div className="text-center">
          <div className="text-tower-distance font-mono font-bold text-tower-xl leading-none">
            {distKm}
            <span className="text-base ml-0.5">km</span>
          </div>
        </div>

        {/* Height */}
        <div className="text-center">
          <div className="text-tower-altitude font-mono font-bold text-tower-xl leading-none">
            {flight.altitudeM}
            <span className="text-base ml-0.5">m</span>
          </div>
          {flight.altitudeAgl > 0 && (
            <div className="text-gray-500 text-[10px] mt-0.5">{flight.altitudeAgl}m AGL</div>
          )}
        </div>

        {/* Climb indicator */}
        <ClimbIndicator verticalSpeedMs={flight.verticalSpeedMs} className="text-xl" />

        {/* Secondary info */}
        <div className="text-gray-500 text-xs space-y-0.5 min-w-[120px]">
          <div>{flight.speedKmh} km/h</div>
          <div>
            {flight.verticalSpeedMs >= 0 ? '+' : ''}{flight.verticalSpeedMs.toFixed(1)} m/s
            {' '}Kurs {flight.trackDeg}&deg;
          </div>
        </div>
      </div>

      {/* Column 4: Status */}
      <div className="w-20 shrink-0 flex justify-center">
        <StatusBadge status={flight.status} elapsedMinutes={elapsedMin} />
      </div>
    </div>
  )
}

function formatLaunchType(lt: string | null): string {
  switch (lt) {
    case 'winch': return 'Winde'
    case 'aerotow': return 'F-Schlepp'
    case 'self': return 'Eigen'
    default: return ''
  }
}
