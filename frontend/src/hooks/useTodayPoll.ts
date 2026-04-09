/**
 * Periodically poll /api/monitor/{slug}/today to:
 *  - get day-statistics for the header
 *  - feed the store with flights archived earlier today that are no longer
 *    in the hot state (so they keep showing in the table all day long)
 *
 * The hot-state itself stays driven by the WebSocket — this hook only
 * supplements with archived data and aggregates.
 */
import { useEffect } from 'react'
import { useMonitorStore, type DayStats, type StripField, ALL_STRIP_FIELDS } from '../store/monitorStore'
import type { Flight } from '../types/flight'

const POLL_INTERVAL_MS = 30_000

interface TodayResponse {
  airfield: string
  timestamp: string
  flights: Array<Record<string, unknown>>
  day_stats: {
    starts_today: number
    landed_today: number
    in_air: number
    by_launch: Record<string, number>
    longest: { reg: string; duration_s: number }
    highest: { reg: string; altitude_m: number }
  }
  config?: {
    strip_fields?: string[]
    landed_visible_minutes?: number
  }
}

function rawToFlight(raw: Record<string, unknown>): Flight {
  return {
    flarmId: String(raw.flarmId || ''),
    registration: raw.registration ? String(raw.registration) : null,
    aircraftModel: raw.aircraftModel ? String(raw.aircraftModel) : null,
    competitionSign: raw.competitionSign ? String(raw.competitionSign) : null,
    status: 'landing',
    qdrDeg: 0,
    bearingText: '',
    distanceM: 0,
    altitudeM: Number(raw.maxAltitudeM || 0),
    altitudeAgl: 0,
    speedKmh: 0,
    verticalSpeedMs: 0,
    trackDeg: 0,
    latitude: 0,
    longitude: 0,
    takeoffTime: String(raw.takeoffTime || ''),
    landingTime: raw.landingTime ? String(raw.landingTime) : null,
    lastSeen: String(raw.landingTime || raw.takeoffTime || ''),
    elapsedS: 0,
    maxAltitudeM: Number(raw.maxAltitudeM || 0),
    maxDistanceM: Number(raw.maxDistanceM || 0),
    launchType: (raw.launchType as Flight['launchType']) || null,
    towPlaneReg: null,
    releaseAltM: null,
  }
}

export function useTodayPoll(slug: string | null) {
  const setTodayData = useMonitorStore((s) => s.setTodayData)

  useEffect(() => {
    if (!slug) return
    let cancelled = false

    async function fetchOnce() {
      try {
        const res = await fetch(`/api/monitor/${slug}/today`)
        if (!res.ok) return
        const data: TodayResponse = await res.json()
        if (cancelled) return

        const archived = data.flights
          .filter((f) => f.source === 'archived')
          .map(rawToFlight)

        const stats: DayStats = {
          startsToday: data.day_stats.starts_today,
          landedToday: data.day_stats.landed_today,
          inAir: data.day_stats.in_air,
          byLaunch: data.day_stats.by_launch || {},
          longest: {
            reg: data.day_stats.longest?.reg || '',
            durationS: data.day_stats.longest?.duration_s || 0,
          },
          highest: {
            reg: data.day_stats.highest?.reg || '',
            altitudeM: data.day_stats.highest?.altitude_m || 0,
          },
        }

        const allowed = new Set<string>(ALL_STRIP_FIELDS)
        const stripFields = (data.config?.strip_fields || ALL_STRIP_FIELDS)
          .filter((f) => allowed.has(f)) as StripField[]
        setTodayData(archived, stats, stripFields)
      } catch {
        // ignore - WebSocket is the primary live source
      }
    }

    fetchOnce()
    const id = setInterval(fetchOnce, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [slug, setTodayData])
}
