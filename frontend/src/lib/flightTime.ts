/**
 * Flight time as shown to the tower: difference of the displayed HH:MM times.
 *
 * Takeoff and landing are shown truncated to the minute (06:40, 08:13), so
 * the duration must be landing minute minus takeoff minute (1:33 h) – not the
 * floored exact difference (06:40:51 → 08:13:26 = 1:32:35 → 1:32 h), which
 * would not add up with the times next to it.
 */
import type { Flight } from '../types/flight'

const MINUTE_MS = 60_000

/**
 * Whole minutes between takeoff and landing (or now, while airborne), each
 * truncated to its minute. null if the takeoff is unknown or the times are
 * inconsistent.
 */
export function flightMinutes(f: Pick<Flight, 'takeoffTime' | 'landingTime'>,
                              now: number = Date.now()): number | null {
  if (!f.takeoffTime) return null
  const start = new Date(f.takeoffTime).getTime()
  const end = f.landingTime ? new Date(f.landingTime).getTime() : now
  if (!isFinite(start) || !isFinite(end) || end < start) return null
  return Math.floor(end / MINUTE_MS) - Math.floor(start / MINUTE_MS)
}
