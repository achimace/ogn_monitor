/**
 * Zustand monitor store - Flight data with delta merge.
 *
 * Manages flights grouped by status for the tower display.
 * Handles WebSocket delta updates efficiently.
 */
import { create } from 'zustand'
import type { Flight, FlightDelta, FlightStats, AlarmPosition, AlarmSeverity, SignalLossScenario } from '../types/flight'

/** Status number to string mapping (from backend IntEnum) */
const STATUS_MAP: Record<number, Flight['status']> = {
  0: 'ground',
  1: 'takeoff',
  2: 'flying',
  3: 'landing',
  4: 'outlanding',
  5: 'alarm',
  6: 'towing',
  7: 'outlanding_pending',
  8: 'emergency',
  9: 'diverted',
  10: 'signal_lost',
}

export interface AlarmEvent {
  flarmId: string
  severity: AlarmSeverity
  scenario: SignalLossScenario
  registration: string
  competitionSign?: string
  aircraftModel?: string
  message: string
  lastPosition: AlarmPosition
  timestamp: string
  acknowledged: boolean
}

export interface DayStats {
  startsToday: number
  landedToday: number
  inAir: number
  byLaunch: Record<string, number>
  longest: { reg: string; durationS: number }
  highest: { reg: string; altitudeM: number }
}

const EMPTY_DAY_STATS: DayStats = {
  startsToday: 0,
  landedToday: 0,
  inAir: 0,
  byLaunch: {},
  longest: { reg: '', durationS: 0 },
  highest: { reg: '', altitudeM: 0 },
}

export const ALL_STRIP_FIELDS = [
  'competition_sign', 'aircraft_model', 'takeoff_time', 'landing_time',
  'duration', 'launch_type', 'qdr', 'distance', 'altitude', 'agl',
  'speed', 'vs', 'track',
] as const
export type StripField = typeof ALL_STRIP_FIELDS[number]

interface MonitorState {
  slug: string | null
  flights: Map<string, Flight>
  archivedToday: Flight[]  // archived earlier today, not in hot state
  dayStats: DayStats
  stripFields: StripField[]
  stats: FlightStats
  alarms: AlarmEvent[]
  connected: boolean
  lastUpdate: string | null

  // Actions
  setSlug: (slug: string) => void
  setFullState: (flights: Flight[], stats: FlightStats) => void
  applyDelta: (flarmId: string, delta: FlightDelta) => void
  addFlight: (flight: Flight) => void
  removeFlight: (flarmId: string) => void
  setTodayData: (archived: Flight[], stats: DayStats, stripFields: StripField[]) => void
  addAlarm: (alarm: Omit<AlarmEvent, 'acknowledged'>) => void
  acknowledgeAlarm: (flarmId: string) => void
  clearAlarm: (flarmId: string) => void
  setConnected: (connected: boolean) => void

  // Computed
  getFlightsByStatus: (...statuses: Flight['status'][]) => Flight[]
  getAlarmFlights: () => Flight[]
  getCombinedFlights: () => Flight[]
}

function normalizeFlightData(raw: Record<string, unknown>): Flight {
  const statusRaw = raw.status
  let status: Flight['status'] = 'ground'
  if (typeof statusRaw === 'number') {
    status = STATUS_MAP[statusRaw] || 'ground'
  } else if (typeof statusRaw === 'string') {
    // Could be a string status name or a numeric string
    const num = parseInt(statusRaw, 10)
    if (!isNaN(num) && STATUS_MAP[num]) {
      status = STATUS_MAP[num]
    } else {
      status = statusRaw as Flight['status']
    }
  }

  return {
    flarmId: String(raw.flarmId || ''),
    registration: raw.registration ? String(raw.registration) : null,
    aircraftModel: raw.aircraftModel ? String(raw.aircraftModel) : null,
    competitionSign: raw.competitionSign ? String(raw.competitionSign) : null,
    status,
    qdrDeg: Number(raw.qdrDeg || 0),
    bearingText: String(raw.bearingText || ''),
    distanceM: raw.distanceM ? Number(raw.distanceM) : (raw.distanceKm ? Number(raw.distanceKm) * 1000 : 0),
    altitudeM: Number(raw.altitudeM || 0),
    altitudeAgl: Number(raw.altitudeAgl || 0),
    speedKmh: Number(raw.speedKmh || 0),
    verticalSpeedMs: Number(raw.verticalSpeedMs || 0),
    trackDeg: Number(raw.trackDeg || 0),
    latitude: Number(raw.latitude || 0),
    longitude: Number(raw.longitude || 0),
    takeoffTime: String(raw.takeoffTime || ''),
    landingTime: raw.landingTime ? String(raw.landingTime) : null,
    lastSeen: String(raw.lastSeen || ''),
    elapsedS: Number(raw.elapsedS || 0),
    maxAltitudeM: Number(raw.maxAltitudeM || 0),
    maxDistanceM: Number(raw.maxDistanceM || 0),
    launchType: (raw.launchType as Flight['launchType']) || null,
    towPlaneReg: raw.towPlaneReg ? String(raw.towPlaneReg) : null,
    releaseAltM: raw.releaseAltM ? Number(raw.releaseAltM) : null,
  }
}

function recalcStats(flights: Map<string, Flight>): FlightStats {
  const stats: FlightStats = { flying: 0, landed: 0, alarm: 0, outlanding: 0 }
  for (const f of flights.values()) {
    if (f.status === 'flying' || f.status === 'towing') stats.flying++
    else if (f.status === 'landing' || f.status === 'ground') stats.landed++
    else if (f.status === 'alarm' || f.status === 'emergency') stats.alarm++
    else if (f.status === 'outlanding' || f.status === 'outlanding_pending' || f.status === 'diverted') stats.outlanding++
    else if (f.status === 'signal_lost') stats.alarm++
  }
  return stats
}

export const useMonitorStore = create<MonitorState>((set, get) => ({
  slug: null,
  flights: new Map(),
  archivedToday: [],
  dayStats: EMPTY_DAY_STATS,
  stripFields: [...ALL_STRIP_FIELDS],
  stats: { flying: 0, landed: 0, alarm: 0, outlanding: 0 },
  alarms: [],
  connected: false,
  lastUpdate: null,

  setSlug: (slug) => set({ slug }),

  setFullState: (rawFlights, stats) => {
    const flights = new Map<string, Flight>()
    for (const raw of rawFlights) {
      const f = normalizeFlightData(raw as unknown as Record<string, unknown>)
      if (f.flarmId) flights.set(f.flarmId, f)
    }
    set({ flights, stats, lastUpdate: new Date().toISOString() })
  },

  applyDelta: (flarmId, delta) => {
    const flights = new Map(get().flights)
    const existing = flights.get(flarmId)
    if (!existing) return

    // Merge delta into existing flight
    const updated = { ...existing }
    for (const [key, val] of Object.entries(delta)) {
      if (val !== undefined) {
        // Handle status conversion
        if (key === 'status') {
          const num = parseInt(String(val), 10)
          if (!isNaN(num) && STATUS_MAP[num]) {
            updated.status = STATUS_MAP[num]
          } else {
            updated.status = val as Flight['status']
          }
        } else {
          (updated as Record<string, unknown>)[key] = val
        }
      }
    }

    flights.set(flarmId, updated)
    set({ flights, stats: recalcStats(flights), lastUpdate: new Date().toISOString() })
  },

  addFlight: (rawFlight) => {
    const flights = new Map(get().flights)
    const f = normalizeFlightData(rawFlight as unknown as Record<string, unknown>)
    flights.set(f.flarmId, f)
    set({ flights, stats: recalcStats(flights), lastUpdate: new Date().toISOString() })
  },

  removeFlight: (flarmId) => {
    const flights = new Map(get().flights)
    flights.delete(flarmId)
    set({ flights, stats: recalcStats(flights), lastUpdate: new Date().toISOString() })
  },

  addAlarm: (alarm) => {
    set((state) => ({
      alarms: [
        ...state.alarms.filter((a) => a.flarmId !== alarm.flarmId),
        { ...alarm, acknowledged: false },
      ],
    }))
  },

  acknowledgeAlarm: (flarmId) => {
    set((state) => ({
      alarms: state.alarms.map((a) =>
        a.flarmId === flarmId ? { ...a, acknowledged: true } : a
      ),
    }))
  },

  clearAlarm: (flarmId) => {
    set((state) => ({
      alarms: state.alarms.filter((a) => a.flarmId !== flarmId),
    }))
  },

  setTodayData: (archived, stats, stripFields) => {
    set({ archivedToday: archived, dayStats: stats, stripFields })
  },

  setConnected: (connected) => set({ connected }),

  getCombinedFlights: () => {
    const live = Array.from(get().flights.values())
    const liveIds = new Set(live.map(f => f.flarmId))
    // Append archived entries that are not currently in the hot state
    const extra = get().archivedToday.filter(f => !liveIds.has(f.flarmId))
    return [...live, ...extra]
  },

  getFlightsByStatus: (...statuses) => {
    const result: Flight[] = []
    for (const f of get().flights.values()) {
      if (statuses.includes(f.status)) result.push(f)
    }
    return result.sort((a, b) => (b.takeoffTime || '').localeCompare(a.takeoffTime || ''))
  },

  getAlarmFlights: () => {
    const result: Flight[] = []
    for (const f of get().flights.values()) {
      if (['alarm', 'emergency', 'signal_lost'].includes(f.status)) result.push(f)
    }
    return result
  },
}))
