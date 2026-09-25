/**
 * Small, muted badge for the tower alarm-handling state (B2).
 *
 * Deliberately low-key: the loud part of an alarm is the StatusBadge and the
 * banner. This badge only tells the crew "someone is already on it".
 */
import type { AlarmState } from '../types/flight'
import { ALARM_STATE_LABELS } from '../types/flight'

const STYLE: Record<AlarmState, string> = {
  acknowledged: 'border-cyan-700 text-cyan-300',
  retrieval_underway: 'border-amber-700 text-amber-300',
  resolved: 'border-gray-600 text-gray-400',
  false_alarm: 'border-gray-600 text-gray-400',
}

interface Props {
  state: AlarmState
  /** Larger variant for the detail drawer. */
  size?: 'sm' | 'md'
  className?: string
}

export default function AlarmStateBadge({ state, size = 'sm', className = '' }: Props) {
  const style = STYLE[state] ?? STYLE.resolved
  const sizing = size === 'md'
    ? 'px-2.5 py-1 text-xs'
    : 'px-1.5 py-0.5 text-[10px] leading-tight text-center'
  return (
    <span
      className={`inline-block rounded border bg-transparent font-semibold max-w-full ${sizing} ${style} ${className}`}
      title={`Alarm-Bearbeitung: ${ALARM_STATE_LABELS[state] ?? state}`}
    >
      {ALARM_STATE_LABELS[state] ?? state}
    </span>
  )
}
