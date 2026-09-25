/**
 * Typed API functions for the monitor endpoints (/api/monitor/*).
 *
 * The public endpoints (track) use raw `fetch` deliberately instead of the
 * `api` client – same approach as useTodayPoll. No Bearer header, no auth
 * redirect on 401.
 *
 * The alarm-handling endpoints (actions) require a dashboard login and
 * therefore go through the `api` client, which adds the Bearer header from
 * localStorage and raises ApiError with the HTTP status (401/403).
 */
import { api } from './client'
import type {
  AlarmActionItem,
  AlarmActionRequest,
  AlarmActionResponse,
  AlarmActionsResponse,
  FlightTrack,
} from '../types/flight'

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

/**
 * Record a tower alarm-handling action for one aircraft (B2).
 *
 * Requires a Bearer token (dashboard login); the `api` client adds it from
 * localStorage. Throws ApiError with status 401 (not logged in) or 403
 * (foreign tenant). Returns the created action plus the flight's new
 * alarm-state fields, ready to merge into the monitor store.
 */
export async function postAlarmAction(
  slug: string,
  flarmId: string,
  body: AlarmActionRequest,
): Promise<AlarmActionResponse> {
  return api.post<AlarmActionResponse>(
    `/monitor/${encodeURIComponent(slug)}/flights/${encodeURIComponent(flarmId)}/actions`,
    body,
  )
}

/**
 * Load the alarm-action history of one aircraft for a given day
 * (YYYY-MM-DD, UTC), newest first. Requires a Bearer token.
 *
 * Defensive against an older backend: a missing/invalid `items` array is
 * returned as an empty list.
 */
export async function fetchAlarmActions(
  slug: string,
  flarmId: string,
  date: string,
): Promise<AlarmActionsResponse> {
  const data = await api.get<Partial<AlarmActionsResponse>>(
    `/monitor/${encodeURIComponent(slug)}/flights/${encodeURIComponent(flarmId)}/actions`,
    { date },
  )
  const items: AlarmActionItem[] = Array.isArray(data.items) ? data.items : []
  return { items, count: typeof data.count === 'number' ? data.count : items.length }
}
