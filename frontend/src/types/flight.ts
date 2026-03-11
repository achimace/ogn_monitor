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

export type LaunchType = 'winch' | 'aerotow' | 'self' | 'unknown'

export type AlarmSeverity = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'

export type SignalLossScenario =
  | 'DIVERTED'
  | 'OUTLANDED'
  | 'EMERGENCY'
  | 'SIGNAL_LOST'

export interface Flight {
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
