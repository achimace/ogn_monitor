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
  onSelect?: (flight: Flight) => void
}

export default function FlightTable({ flights, onSelect }: FlightTableProps) {
  // Sort by "last activity" desc — for landed flights this is the landing
  // time, for active flights the takeoff time. The most recently active
  // aircraft always shows up first in its section.
  const lastActivity = (f: Flight) => f.landingTime || f.takeoffTime || ''
  const sorted = [...flights].sort((a, b) =>
    lastActivity(b).localeCompare(lastActivity(a))
  )
  const emergency = sorted.filter((f) => f.status === 'emergency')
  const alarm = sorted.filter((f) => f.status === 'alarm')
  const signalLost = sorted.filter((f) => f.status === 'signal_lost')
  const towing = sorted.filter((f) => f.status === 'towing')
  const flying = sorted.filter((f) => f.status === 'flying')
  const outlanding = sorted.filter((f) => ['outlanding', 'outlanding_pending', 'diverted'].includes(f.status))
  const landed = sorted.filter((f) => ['landing', 'ground'].includes(f.status))

  return (
    <div className="space-y-3">
      {emergency.length > 0 && (
        <FlightSection title="NOTFALL" count={emergency.length} flights={emergency} color="red" blink onSelect={onSelect} />
      )}
      {alarm.length > 0 && (
        <FlightSection title="ALARM" count={alarm.length} flights={alarm} color="red" onSelect={onSelect} />
      )}
      {signalLost.length > 0 && (
        <FlightSection title="KEIN SIGNAL" count={signalLost.length} flights={signalLost} color="yellow" onSelect={onSelect} />
      )}
      {outlanding.length > 0 && (
        <FlightSection title="AUSSENLANDUNG" count={outlanding.length} flights={outlanding} color="orange" onSelect={onSelect} />
      )}
      {towing.length > 0 && (
        <FlightSection title="IM SCHLEPP" count={towing.length} flights={towing} color="yellow" onSelect={onSelect} />
      )}
      {flying.length > 0 && (
        <FlightSection title="FLIEGEND" count={flying.length} flights={flying} color="blue" onSelect={onSelect} />
      )}
      {landed.length > 0 && (
        <FlightSection title="GELANDET" count={landed.length} flights={landed} color="green" onSelect={onSelect} />
      )}
      {flights.length === 0 && (
        <div className="text-center text-gray-500 py-16 text-lg">
          Keine Fluege aktiv
        </div>
      )}
    </div>
  )
}

function FlightSection({ title, count, flights, color, blink, onSelect }: {
  title: string; count: number; flights: Flight[]; color: string; blink?: boolean
  onSelect?: (f: Flight) => void
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
          <FlightRow
            key={flight.flarmId}
            flight={flight}
            bgColor={bgMap[color] || ''}
            onSelect={onSelect}
          />
        ))}
      </div>
    </div>
  )
}

function FlightRow({ flight, bgColor, onSelect }: {
  flight: Flight; bgColor: string; onSelect?: (f: Flight) => void
}) {
  const takeoffStr = flight.takeoffTime
    ? new Date(flight.takeoffTime).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })
    : '-'

  const distKm = (flight.distanceM / 1000).toFixed(1)
  const launchLabel = formatLaunchType(flight.launchType)
  const elapsedMin = flight.elapsedS > 0 ? Math.floor(flight.elapsedS / 60) : undefined

  const handleClick = onSelect ? () => onSelect(flight) : undefined
  const clickable = onSelect ? 'cursor-pointer hover:brightness-110' : ''

  return (
    <div
      onClick={handleClick}
      className={`${bgColor} rounded-lg border border-tower-border/30 ${clickable} transition-all`}
    >
      {/* ===== Mobile card layout (< md) ===== */}
      <div className="md:hidden p-3">
        <div className="flex items-start justify-between gap-2 mb-2">
          <div className="min-w-0">
            <div className="text-white font-bold text-base truncate">
              {flight.registration || flight.flarmId}
              {flight.competitionSign && (
                <span className="text-tower-qdr text-sm font-mono ml-2">{flight.competitionSign}</span>
              )}
            </div>
            <div className="text-gray-500 text-xs truncate">
              {takeoffStr}{launchLabel && ` · ${launchLabel}`}
              {flight.aircraftModel && ` · ${flight.aircraftModel}`}
            </div>
          </div>
          <StatusBadge status={flight.status} elapsedMinutes={elapsedMin} />
        </div>
        <div className="grid grid-cols-3 gap-2 text-center">
          <div>
            <div className="text-white font-mono font-bold text-2xl leading-none">{flight.qdrDeg}&deg;</div>
            <div className="text-gray-500 text-[10px] mt-1">{flight.bearingText}</div>
          </div>
          <div>
            <div className="text-tower-distance font-mono font-bold text-2xl leading-none">
              {distKm}<span className="text-sm ml-0.5">km</span>
            </div>
            <div className="text-gray-500 text-[10px] mt-1">{flight.speedKmh} km/h</div>
          </div>
          <div>
            <div className="text-tower-altitude font-mono font-bold text-2xl leading-none">
              {flight.altitudeM}<span className="text-sm ml-0.5">m</span>
            </div>
            <div className="text-gray-500 text-[10px] mt-1">
              {flight.verticalSpeedMs >= 0 ? '+' : ''}{flight.verticalSpeedMs.toFixed(1)} m/s
            </div>
          </div>
        </div>
      </div>

      {/* ===== Desktop row layout (>= md) ===== */}
      <div className="hidden md:flex px-4 py-3 items-center gap-4">
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
