/**
 * Helpers for the tower alarm-handling workflow (B2).
 */
import type { Flight } from '../types/flight'

/**
 * Statuses for which alarm handling is offered even before the first action.
 * `outlanding_pending` is included because the backend maps it to alarm kind
 * `outlanding`.
 */
const ALARM_STATUSES: ReadonlySet<Flight['status']> = new Set([
  'alarm', 'emergency', 'outlanding', 'outlanding_pending', 'signal_lost',
])

/** True when the detail drawer should show the "Alarm-Bearbeitung" section. */
export function isAlarmHandlingRelevant(flight: Flight): boolean {
  return ALARM_STATUSES.has(flight.status) || !!flight.alarmState
}
