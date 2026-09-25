/**
 * Core flight data types matching the WebSocket Delta protocol
 * and Redis Hot State schema.
 */

export type FlightStatus =
  | 'ground'
  | 'takeoff'
  | 'flying'
  | 'landing'
  | 'outlanding'
  | 'alarm'
  | 'towing'
  | 'outlanding_pending'
  | 'emergency'
  | 'diverted'
  | 'signal_lost'

export type LaunchType =
  | 'winch'
  | 'aerotow'
  | 'aerotow_ambiguous'
  | 'self'
  | 'powered'
  | 'unknown'

export type AlarmSeverity = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'

export type SignalLossScenario =
  | 'DIVERTED'
  | 'OUTLANDED'
  | 'EMERGENCY'
  | 'SIGNAL_LOST'

/** Tower-side handling state of an alarm (B2). Absent/null = unhandled. */
export type AlarmState =
  | 'acknowledged'
  | 'retrieval_underway'
  | 'resolved'
  | 'false_alarm'

export const ALARM_STATES: readonly AlarmState[] = [
  'acknowledged',
  'retrieval_underway',
  'resolved',
  'false_alarm',
] as const

export const ALARM_STATE_LABELS: Record<AlarmState, string> = {
  acknowledged: 'Quittiert',
  retrieval_underway: 'Rückholung unterwegs',
  resolved: 'Erledigt',
  false_alarm: 'Fehlalarm',
}

/**
 * Where a flight ended: at the home airfield, at a foreign airfield
 * (known field, not an outlanding), in the field, or not yet landed ("").
 * Kept open to plain `string` so an unknown backend value never breaks the UI.
 */
export type LandingType = 'home' | 'foreign' | 'outlanding' | ''

/**
 * Airfield attribution of a flight (all optional – the backend may not
 * send them yet, and a delta may carry only some of them).
 */
export interface AirfieldFields {
  /** Name/ICAO of the takeoff airfield, e.g. "DASSU", "Gundelfingen (EDMU)", "unbekannt" */
  takeoffAirfield?: string
  /** Name/ICAO of the landing airfield (empty while airborne) */
  landingAirfield?: string
  landingType?: LandingType | string
  /** True when the aircraft took off elsewhere and arrived at this airfield */
  isVisitor?: boolean
}

/** The four alarm-handling fields carried by hot-state flights (all optional). */
export interface AlarmStateFields {
  alarmState?: AlarmState | null
  alarmComment?: string | null
  alarmSetBy?: string | null
  /** ISO-8601 timestamp (UTC, "Z") */
  alarmSetAt?: string | null
}

/** One entry of the alarm-action history (GET …/flights/{flarmId}/actions). */
export interface AlarmActionItem {
  /** UUID */
  id: string
  state: AlarmState
  comment: string | null
  alarmKind: string | null
  setBy: string | null
  createdAt: string
  flightTakeoffTs: string | null
}

export interface AlarmActionsResponse {
  items: AlarmActionItem[]
  count: number
}

/** Body of POST …/flights/{flarmId}/actions */
export interface AlarmActionRequest {
  state: AlarmState
  comment?: string
  alarm_kind?: string
}

/** 201 response of POST …/flights/{flarmId}/actions */
export interface AlarmActionResponse {
  action: AlarmActionItem
  alarmState: {
    alarmState: AlarmState
    alarmComment: string | null
    alarmSetBy: string | null
    alarmSetAt: string
  }
}

export interface Flight extends AlarmStateFields, AirfieldFields {
  flarmId: string
  registration: string | null
  aircraftModel: string | null
  competitionSign: string | null
  status: FlightStatus

  // Primary info for tower controller
  qdrDeg: number
  bearingText: string
  distanceM: number
  altitudeM: number
  altitudeAgl: number

  // Movement
  speedKmh: number
  verticalSpeedMs: number
  trackDeg: number

  // Position (for map)
  latitude: number
  longitude: number

  // Times
  takeoffTime: string
  landingTime: string | null
  lastSeen: string
  elapsedS: number

  // Statistics
  maxAltitudeM: number
  maxDistanceM: number

  // Launch type
  launchType: LaunchType | null
  towPlaneReg: string | null
  releaseAltM: number | null
}

/** Delta update - only changed fields */
export type FlightDelta = Partial<Omit<Flight, 'flarmId'>>

/** WebSocket messages from server */
export type WsMessage =
  | { type: 'full_state'; flights: Flight[]; stats: FlightStats }
  | { type: 'flight_update'; flarmId: string; ts: string; d: FlightDelta }
  | { type: 'flight_added'; flight: Flight }
  | { type: 'flight_removed'; flarmId: string; reason: string; summary: FlightSummary }
  | { type: 'alarm'; severity: AlarmSeverity; flarmId: string; registration: string; scenario: SignalLossScenario; message: string; lastPosition: AlarmPosition }
  | { type: 'ping'; ts: string }

export interface FlightStats {
  flying: number
  landed: number
  alarm: number
  outlanding: number
}

export interface FlightSummary {
  landingTime: string
  flightDurationS: number
  maxAltitudeM: number
  maxDistanceM: number
  launchType: LaunchType | null
}

/** One stored position of a flight track (GET /monitor/{slug}/flights/{flarmId}/track) */
export interface TrackPoint {
  /** ISO-8601 timestamp (UTC, "Z") */
  t: string
  lat: number
  lon: number
  alt: number
  agl?: number | null
  speed?: number | null
  vs?: number | null
  track?: number | null
}

export interface FlightTrack {
  airfield: string
  flarmId: string
  since: string
  points: TrackPoint[]
}

export interface AlarmPosition {
  latitude: number
  longitude: number
  altitudeM: number
  qdrDeg: number
  bearingText: string
  distanceKm: number
  lastSeen: string
  elapsedMinutes: number
}
