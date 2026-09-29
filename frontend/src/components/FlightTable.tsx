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
import AlarmStateBadge from './AlarmStateBadge'
import ClimbIndicator from './ClimbIndicator'
import type { StripField } from '../store/monitorStore'
import { ALL_STRIP_FIELDS } from '../store/monitorStore'

interface FlightTableProps {
  flights: Flight[]
  /** Row click (used when no onFocus is given) – opens the detail drawer. */
  onSelect?: (flight: Flight) => void
  /** Row click in split view – focuses the aircraft on the map instead of onSelect. */
  onFocus?: (flight: Flight) => void
  /** When given, every row shows a small "Details" button that calls this. */
  onDetails?: (flight: Flight) => void
  /** FLARM ID of the aircraft currently focused on the map (subtle highlight). */
  focusedFlarmId?: string
  stripFields?: StripField[]
  /** Narrow container (split view): smaller landed-strip columns. */
  dense?: boolean
}

/** Callbacks + highlight state shared by every section/row. */
interface RowHandlers {
  onSelect?: (f: Flight) => void
  onFocus?: (f: Flight) => void
  onDetails?: (f: Flight) => void
  focusedFlarmId?: string
  dense?: boolean
}

export default function FlightTable({ flights, onSelect, onFocus, onDetails, focusedFlarmId, stripFields, dense }: FlightTableProps) {
  const handlers: RowHandlers = { onSelect, onFocus, onDetails, focusedFlarmId, dense }
  const fields = new Set<StripField>(stripFields && stripFields.length > 0
    ? stripFields
    : ALL_STRIP_FIELDS)
  const show = (f: StripField) => fields.has(f)
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
  // "FLIEGEND" includes the brief TAKEOFF phase right after lift-off so
  // the strip appears the moment the aircraft is airborne — same moment
  // it turns blue on the map.
  const flying = sorted.filter((f) => f.status === 'flying' || f.status === 'takeoff')
  // A landing at a known foreign airfield (landingType 'foreign') is a
  // regular landing ("GELANDET" with the field name), never an outlanding –
  // regardless of which status the backend attached to it.
  const outlanding = sorted.filter((f) =>
    ['outlanding', 'outlanding_pending', 'diverted'].includes(f.status) && !isForeignLanding(f))
  const landed = sorted.filter(isLandedStrip)

  return (
    <div className="space-y-3">
      {emergency.length > 0 && (
        <FlightSection title="NOTFALL" count={emergency.length} flights={emergency} color="red" blink handlers={handlers} show={show} />
      )}
      {alarm.length > 0 && (
        <FlightSection title="ALARM" count={alarm.length} flights={alarm} color="red" handlers={handlers} show={show} />
      )}
      {signalLost.length > 0 && (
        <FlightSection title="KEIN SIGNAL" count={signalLost.length} flights={signalLost} color="yellow" handlers={handlers} show={show} />
      )}
      {outlanding.length > 0 && (
        <FlightSection title="AUSSENLANDUNG" count={outlanding.length} flights={outlanding} color="orange" handlers={handlers} show={show} />
      )}
      {towing.length > 0 && (
        <FlightSection title="IM SCHLEPP" count={towing.length} flights={towing} color="yellow" handlers={handlers} show={show} />
      )}
      {flying.length > 0 && (
        <FlightSection title="FLIEGEND" count={flying.length} flights={flying} color="blue" handlers={handlers} show={show} />
      )}
      {landed.length > 0 && (
        <FlightSection title="GELANDET" count={landed.length} flights={landed} color="green" handlers={handlers} show={show} />
      )}
      {flights.length === 0 && (
        <div className="text-center text-gray-500 py-16 text-lg">
          Keine Fluege aktiv
        </div>
      )}
    </div>
  )
}

function FlightSection({ title, count, flights, color, blink, handlers, show }: {
  title: string; count: number; flights: Flight[]; color: string; blink?: boolean
  handlers: RowHandlers
  show: (f: StripField) => boolean
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
            handlers={handlers} show={show}
          />
        ))}
      </div>
    </div>
  )
}

function FlightRow({ flight, bgColor, handlers, show }: {
  flight: Flight; bgColor: string; handlers: RowHandlers
  show: (f: StripField) => boolean
}) {
  const { onSelect, onFocus, onDetails, focusedFlarmId, dense } = handlers
  const takeoffStr = formatHM(flight.takeoffTime)
  const landingStr = formatHM(flight.landingTime)
  const durationStr = computeDurationShort(flight)
  const landedDurationStr = formatDurationLanded(flight)

  const distKm = (flight.distanceM / 1000).toFixed(1)
  const launchLabel = formatLaunchType(flight.launchType)
  const elapsedMin = flight.elapsedS > 0 ? Math.floor(flight.elapsedS / 60) : undefined

  // Landed strips ("GELANDET" section) drop the stale live data (QDR,
  // distance, altitude, climb, speed/track) and show takeoff/landing
  // airfield instead. Fallbacks match FlightDetailDrawer.
  const landedStrip = isLandedStrip(flight)
  const takeoffField = flight.takeoffAirfield || 'unbekannt'
  const isOutlanded = flight.landingType === 'outlanding'
  const landingField = flight.landingAirfield || (isOutlanded ? 'Außenlandung' : '—')

  // Landing at a known foreign airfield: badge stays "LDG"; the field name
  // is shown by the airfield pair of the landed strip.
  const foreignLanding = isForeignLanding(flight)
  const badgeStatus: Flight['status'] = foreignLanding ? 'landing' : flight.status
  // Visitor: took off elsewhere and arrived here.
  const visitorHint = flight.isVisitor
    ? (flight.takeoffAirfield ? `Besucher von ${flight.takeoffAirfield}` : 'Besucher')
    : ''

  // Split view: the row click focuses the map (onFocus) and the drawer is
  // reached via the "Details" button. Plain table view: row click = drawer.
  const rowAction = onFocus || onSelect
  const handleClick = rowAction ? () => rowAction(flight) : undefined
  const clickable = rowAction ? 'cursor-pointer hover:brightness-110' : ''
  const focused = focusedFlarmId !== undefined && focusedFlarmId === flight.flarmId
  const focusRing = focused ? 'ring-1 ring-tower-qdr border-tower-qdr/60' : ''

  // Stop propagation so the row click (focus) does not fire as well.
  const detailsButton = onDetails ? (
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); onDetails(flight) }}
      className="rounded border border-tower-border text-gray-400 hover:text-white hover:border-gray-500
        text-xs px-2 py-1 transition-colors shrink-0"
      title="Flugdetails anzeigen"
    >
      Details
    </button>
  ) : null

  // Tower alarm-handling state (B2) – muted badge stacked under the status.
  const alarmStateBadge = flight.alarmState ? (
    <AlarmStateBadge state={flight.alarmState} />
  ) : null

  return (
    <div
      onClick={handleClick}
      className={`${bgColor} rounded-lg border border-tower-border/30 ${clickable} ${focusRing} transition-all`}
    >
      {/* ===== Mobile card layout (< md) ===== */}
      <div className="md:hidden p-3">
        <div className="flex items-start justify-between gap-2 mb-2">
          <div className="min-w-0">
            <div className="text-white font-bold text-base truncate">
              {flight.registration || flight.flarmId}
              {show('competition_sign') && flight.competitionSign && (
                <span className="text-tower-qdr text-sm font-mono ml-2">{flight.competitionSign}</span>
              )}
            </div>
            <div className="text-gray-500 text-xs truncate">
              {(landedStrip
                ? [
                    show('aircraft_model') && flight.aircraftModel,
                    show('launch_type') && launchLabel,
                  ]
                : [
                    show('takeoff_time') && takeoffStr ? `Start ${takeoffStr}` : '',
                    show('landing_time') && landingStr ? `Landung ${landingStr}` : '',
                    show('duration') && durationStr,
                    show('launch_type') && launchLabel,
                    show('aircraft_model') && flight.aircraftModel,
                  ]
              ).filter(Boolean).join(' · ')}
            </div>
            {visitorHint && (
              <div className="text-sky-300 text-xs truncate">{visitorHint}</div>
            )}
          </div>
          <div className="flex flex-col items-end gap-1.5 shrink-0">
            <StatusBadge status={badgeStatus} elapsedMinutes={elapsedMin} />
            {alarmStateBadge}
            {detailsButton}
          </div>
        </div>
        {landedStrip ? (
          <div className="grid grid-cols-3 gap-3">
            <LandedTimeBlock label="Start" time={show('takeoff_time') ? takeoffStr : undefined}
              timeClass="text-sky-400" airfield={takeoffField} mobile />
            <LandedTimeBlock label="Landung" time={show('landing_time') ? landingStr : undefined}
              timeClass="text-green-400" airfield={landingField} outlanding={isOutlanded} mobile />
            {show('duration') && (
              <LandedTimeBlock label="Flugdauer" time={landedDurationStr}
                timeClass="text-white" airfield="" mobile />
            )}
          </div>
        ) : (
        <div className="grid grid-cols-3 gap-2 text-center">
          <div>
            {show('qdr') ? (
              <>
                <div className="text-white font-mono font-bold text-2xl leading-none">{flight.qdrDeg}&deg;</div>
                <div className="text-gray-500 text-[10px] mt-1">{flight.bearingText}</div>
              </>
            ) : <div />}
          </div>
          <div>
            {show('distance') ? (
              <>
                <div className="text-tower-distance font-mono font-bold text-2xl leading-none">
                  {distKm}<span className="text-sm ml-0.5">km</span>
                </div>
                {show('speed') && (
                  <div className="text-gray-500 text-[10px] mt-1">{flight.speedKmh} km/h</div>
                )}
              </>
            ) : <div />}
          </div>
          <div>
            {show('altitude') ? (
              <>
                <div className="text-tower-altitude font-mono font-bold text-2xl leading-none">
                  {flight.altitudeM}<span className="text-sm ml-0.5">m</span>
                </div>
                {show('vs') && (
                  <div className="text-gray-500 text-[10px] mt-1">
                    {flight.verticalSpeedMs >= 0 ? '+' : ''}{flight.verticalSpeedMs.toFixed(1)} m/s
                  </div>
                )}
              </>
            ) : <div />}
          </div>
        </div>
        )}
      </div>

      {/* ===== Desktop landed strip (>= md) =====
          Four clear columns: aircraft | START | LANDUNG | FLUGDAUER, each
          with a small label, the time large and the airfield underneath;
          launch type muted, status badge on the right. Live data (QDR,
          distance, altitude) is stale once landed and not shown. */}
      {landedStrip && (
      <div className={`hidden md:flex items-center ${dense ? 'px-4 py-3 gap-3' : 'px-5 py-4 gap-6'}`}>
        <div className={`${dense ? 'w-28' : 'w-44'} shrink-0 min-w-0`}>
          <div className="text-white font-bold text-lg leading-tight truncate">
            {flight.registration || flight.flarmId}
            {show('competition_sign') && flight.competitionSign && (
              <span className="text-gray-400 text-sm font-mono font-normal ml-2">{flight.competitionSign}</span>
            )}
          </div>
          {show('aircraft_model') && flight.aircraftModel && (
            <div className="text-gray-500 text-sm truncate mt-0.5">{flight.aircraftModel}</div>
          )}
          {visitorHint && (
            <div className="text-sky-300 text-xs leading-tight truncate mt-0.5" title={visitorHint}>
              {visitorHint}
            </div>
          )}
        </div>

        <LandedTimeBlock label="Start" time={show('takeoff_time') ? takeoffStr : undefined}
          timeClass="text-sky-400" airfield={takeoffField} dense={dense} />
        <LandedTimeBlock label="Landung" time={show('landing_time') ? landingStr : undefined}
          timeClass="text-green-400" airfield={landingField} outlanding={isOutlanded} dense={dense} />
        {show('duration') && (
          // Split view: no room for a launch-type column – it goes under the
          // duration. Otherwise an empty line keeps the labels aligned.
          <LandedTimeBlock label="Flugdauer" time={landedDurationStr} timeClass="text-white" dense={dense}
            airfield={dense && show('launch_type') ? launchLabel : ''} muted />
        )}

        {show('launch_type') && !(dense && show('duration')) && (
          <div className={`${dense ? 'w-20 text-sm' : 'w-32 text-lg'} min-w-0 shrink text-gray-400 truncate`}
            title={launchLabel || undefined}>
            {launchLabel}
          </div>
        )}

        <div className="flex-1" />

        <div className="w-20 shrink-0 flex flex-col items-center gap-1">
          <StatusBadge status={badgeStatus} elapsedMinutes={elapsedMin} />
          {alarmStateBadge}
          {detailsButton}
        </div>
      </div>
      )}

      {/* ===== Desktop row layout (>= md), live strips ===== */}
      {!landedStrip && (
      <div className="hidden md:flex px-4 py-3 items-center gap-4">
        {/* Column 1: Aircraft info */}
        <div className="w-24 shrink-0">
          <div className="text-white font-bold text-sm">{flight.registration || flight.flarmId}</div>
          {show('competition_sign') && flight.competitionSign && (
            <div className="text-gray-400 text-xs">{flight.competitionSign}</div>
          )}
          {show('aircraft_model') && flight.aircraftModel && (
            <div className="text-gray-500 text-[10px]">{flight.aircraftModel}</div>
          )}
          {visitorHint && (
            <div className="text-sky-300 text-[10px] leading-tight truncate" title={visitorHint}>
              {visitorHint}
            </div>
          )}
        </div>

        {/* Column 2: Times + Launch type */}
        {(show('takeoff_time') || show('landing_time') || show('duration') || show('launch_type')) && (
          <div className="w-20 shrink-0 text-center">
            {show('takeoff_time') && takeoffStr && (
              <div className="text-gray-300 text-sm font-mono leading-tight">{takeoffStr}</div>
            )}
            {show('landing_time') && landingStr && (
              <div className="text-green-400 text-sm font-mono leading-tight">↓ {landingStr}</div>
            )}
            {show('duration') && durationStr && (
              <div className="text-gray-500 text-[10px] mt-0.5">{durationStr}</div>
            )}
            {show('launch_type') && launchLabel && (
              <div className="text-gray-500 text-[10px] mt-0.5">{launchLabel}</div>
            )}
          </div>
        )}

        {/* Column 3: PRIMARY - QDR, Distance, Height.
            min-w-0 lets the column shrink in the half-width split pane instead
            of pushing the status column out of the visible area. */}
        <div className="flex-1 min-w-0 flex items-center gap-3 lg:gap-6">
          {show('qdr') && (
            <div className="text-center">
              <div className="text-white font-mono font-bold text-tower-xl leading-none">
                {flight.qdrDeg}&deg;
              </div>
              <div className="text-gray-500 text-xs mt-0.5">{flight.bearingText}</div>
            </div>
          )}

          {show('distance') && (
            <div className="text-center">
              <div className="text-tower-distance font-mono font-bold text-tower-xl leading-none">
                {distKm}
                <span className="text-base ml-0.5">km</span>
              </div>
            </div>
          )}

          {show('altitude') && (
            <div className="text-center">
              <div className="text-tower-altitude font-mono font-bold text-tower-xl leading-none">
                {flight.altitudeM}
                <span className="text-base ml-0.5">m</span>
              </div>
              {show('agl') && flight.altitudeAgl > 0 && (
                <div className="text-gray-500 text-[10px] mt-0.5">{flight.altitudeAgl}m AGL</div>
              )}
            </div>
          )}

          {show('vs') && (
            <ClimbIndicator verticalSpeedMs={flight.verticalSpeedMs} className="text-xl" />
          )}

          {(show('speed') || show('vs') || show('track')) && (
            <div className="text-gray-500 text-xs space-y-0.5 whitespace-nowrap">
              {show('speed') && <div>{flight.speedKmh} km/h</div>}
              {(show('vs') || show('track')) && (
                <div>
                  {show('vs') && <>{flight.verticalSpeedMs >= 0 ? '+' : ''}{flight.verticalSpeedMs.toFixed(1)} m/s</>}
                  {show('vs') && show('track') && ' '}
                  {show('track') && <>Kurs {flight.trackDeg}&deg;</>}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Column 4: Status, with the alarm-handling badge and the Details
            button (split view only) stacked underneath so the row keeps its
            width. */}
        <div className="w-20 shrink-0 flex flex-col items-center gap-1">
          <StatusBadge status={badgeStatus} elapsedMinutes={elapsedMin} />
          {alarmStateBadge}
          {detailsButton}
        </div>
      </div>
      )}
    </div>
  )
}

/**
 * One column of a landed strip: small label, the time large (mono) and the
 * airfield underneath. The time never truncates (fixed min width); a long
 * airfield name does, with the full name as tooltip. Outlandings are
 * highlighted orange like the outlanding status.
 * ``dense``: split view (half width); ``mobile``: phone card.
 */
function LandedTimeBlock({ label, time, timeClass, airfield, outlanding, muted, dense, mobile }: {
  label: string
  /** undefined = time hidden by the strip-field config, '' = unknown */
  time?: string
  timeClass: string
  /** Line under the time; '' = empty placeholder line (keeps alignment) */
  airfield?: string
  outlanding?: boolean
  /** Secondary text (launch type) instead of an airfield name */
  muted?: boolean
  dense?: boolean
  mobile?: boolean
}) {
  const box = mobile ? 'min-w-0' : dense ? 'flex-1 min-w-[6.5rem]' : 'w-48 min-w-[8.5rem] shrink'
  const timeSize = mobile ? 'text-xl' : dense ? 'text-2xl' : 'text-tower-xl'
  return (
    <div className={box}>
      <div className={`text-gray-500 uppercase tracking-wider ${mobile ? 'text-[10px]' : 'text-xs'}`}>
        {label}
      </div>
      {time !== undefined && (
        <div className={`${time ? timeClass : 'text-gray-600'} ${timeSize} font-mono font-bold leading-tight whitespace-nowrap`}>
          {time || '—'}
        </div>
      )}
      {airfield !== undefined && (
        <div
          className={`${outlanding ? 'text-orange-400' : muted ? 'text-gray-400' : 'text-gray-200'} leading-tight truncate ${
            mobile ? 'text-xs mt-0.5' : dense ? 'text-sm mt-0.5' : 'text-base mt-1'}`}
          title={airfield || undefined}
        >
          {airfield || '\u00a0'}
        </div>
      )}
    </div>
  )
}

/**
 * Strip belongs in the "GELANDET" section and gets the landed layout
 * (airfields instead of live data). Matches FlightDetailDrawer.isLanded
 * for these rows – outlandings without a known foreign field live in the
 * separate "AUSSENLANDUNG" section and keep the regular strip layout.
 */
function isLandedStrip(f: Flight): boolean {
  return ['landing', 'ground'].includes(f.status) || isForeignLanding(f)
}

/**
 * Landed at a known foreign airfield. The contract sends this as status
 * `landing` + `landingType: 'foreign'`; the status check keeps airborne
 * flights (landingType still set from a previous leg) out of it.
 */
function isForeignLanding(f: Flight): boolean {
  if (f.landingType !== 'foreign') return false
  return ['landing', 'ground', 'outlanding', 'outlanding_pending', 'diverted'].includes(f.status)
}

function formatHM(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })
}

function computeDurationShort(f: Flight): string {
  if (!f.takeoffTime) return ''
  const start = new Date(f.takeoffTime).getTime()
  const end = f.landingTime ? new Date(f.landingTime).getTime() : Date.now()
  if (!isFinite(start) || !isFinite(end) || end <= start) return ''
  const sec = Math.floor((end - start) / 1000)
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}h` : `${m}min`
}

/** Flight duration for landed strips: "05 min", "1:05 h" ('' if unknown). */
function formatDurationLanded(f: Flight): string {
  if (!f.takeoffTime || !f.landingTime) return ''
  const start = new Date(f.takeoffTime).getTime()
  const end = new Date(f.landingTime).getTime()
  if (!isFinite(start) || !isFinite(end) || end <= start) return ''
  const min = Math.floor((end - start) / 60000)
  const h = Math.floor(min / 60)
  const m = String(min % 60).padStart(2, '0')
  return h > 0 ? `${h}:${m} h` : `${m} min`
}

function formatLaunchType(lt: string | null): string {
  switch (lt) {
    case 'winch': return 'Winde'
    case 'aerotow': return 'F-Schlepp'
    case 'aerotow_ambiguous': return 'F-Schlepp?'
    case 'self': return 'Eigen'
    case 'powered': return 'Motor'
    default: return ''
  }
}
