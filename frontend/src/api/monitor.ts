/**
 * Typed API functions for the public monitor endpoints (/api/monitor/*).
 *
 * These endpoints are public (no auth), so raw `fetch` is used deliberately
 * instead of the `api` client – same approach as useTodayPoll. No Bearer
 * header, no auth redirect on 401.
 */
import type { FlightTrack } from '../types/flight'

/**
 * Load the stored positions of one aircraft for the last `hours` hours.
 *
 * Returns an empty `points` array when nothing is stored (the backend answers
 * 200 in that case). Pass an AbortSignal to cancel a stale request when the
 * focus changes quickly.
 */
export async function fetchTrack(
  slug: string,
  flarmId: string,
  hours = 24,
  signal?: AbortSignal,
): Promise<FlightTrack> {
  const url = `/api/monitor/${encodeURIComponent(slug)}/flights/${encodeURIComponent(flarmId)}/track?hours=${hours}`
  const res = await fetch(url, { signal })
  if (!res.ok) {
    throw new Error(`Track request failed: ${res.status}`)
  }
  const data = (await res.json()) as Partial<FlightTrack>
  // Be defensive: an older backend may not know the endpoint's fields yet.
  return {
    airfield: data.airfield ?? slug,
    flarmId: data.flarmId ?? flarmId,
    since: data.since ?? '',
    points: Array.isArray(data.points) ? data.points : [],
  }
}
