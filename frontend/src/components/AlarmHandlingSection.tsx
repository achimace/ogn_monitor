/**
 * "Alarm-Bearbeitung" section of the flight detail drawer (B2).
 *
 * Shows the current tower handling state, four action buttons, a one-line
 * comment and the day's action history. Without a dashboard login only a
 * hint with a link to /login is shown – the state itself stays visible for
 * everyone (public monitor).
 */
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { AlarmActionItem, AlarmState, Flight } from '../types/flight'
import { ALARM_STATES, ALARM_STATE_LABELS } from '../types/flight'
import { useAuthStore } from '../store/authStore'
import { useAlarmActions, COMMENT_MAX_LEN } from '../hooks/useAlarmActions'
import AlarmStateBadge from './AlarmStateBadge'

interface Props {
  flight: Flight
  airfieldSlug: string | null
}

export default function AlarmHandlingSection({ flight, airfieldSlug }: Props) {
  const token = useAuthStore((s) => s.token)
  const hasToken = !!token
  const [comment, setComment] = useState('')
  const [pendingState, setPendingState] = useState<AlarmState | null>(null)

  const date = historyDate()
  const { history, historyLoading, historyError, submitting, submitError, submit } =
    useAlarmActions({
      slug: airfieldSlug,
      flarmId: flight.flarmId,
      date,
      enabled: hasToken && !!airfieldSlug,
    })

  // Reset the input when the drawer switches to another aircraft.
  useEffect(() => {
    setComment('')
    setPendingState(null)
  }, [flight.flarmId])

  async function handleAction(state: AlarmState) {
    if (submitting) return
    setPendingState(state)
    const ok = await submit(state, comment)
    setPendingState(null)
    if (ok) setComment('')
  }

  const current = flight.alarmState ?? null

  return (
    <div className="px-5 py-4 border-b border-tower-border space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-gray-500 text-xs uppercase tracking-wider">Alarm-Bearbeitung</div>
        {current
          ? <AlarmStateBadge state={current} size="md" />
          : <span className="text-xs text-gray-400 italic">unbearbeitet</span>}
      </div>

      {/* Public part (visible without login): `alarmSetBy` is a non-personal
          display label from the hot state (tenant name or "Flugleiter"),
          never an e-mail address – shown as-is. */}
      {current && (flight.alarmSetBy || flight.alarmSetAt) && (
        <div className="text-xs text-gray-400">
          {flight.alarmSetAt && <span className="font-mono">{formatHM(flight.alarmSetAt)} UTC</span>}
          {flight.alarmSetAt && flight.alarmSetBy && ' · '}
          {flight.alarmSetBy && <span>{flight.alarmSetBy}</span>}
          {flight.alarmComment && (
            <div className="text-gray-300 mt-0.5 break-words">„{flight.alarmComment}“</div>
          )}
        </div>
      )}

      {!hasToken ? (
        <div className="rounded-lg border border-tower-border bg-tower-bg px-3 py-2.5 text-sm text-gray-300">
          <Link to="/login" className="text-tower-qdr hover:underline font-medium">
            Zum Quittieren anmelden
          </Link>
        </div>
      ) : !airfieldSlug ? null : (
        <>
          <div className="grid grid-cols-2 gap-2">
            {ALARM_STATES.map((s) => (
              <ActionButton
                key={s}
                state={s}
                active={current === s}
                busy={pendingState === s}
                disabled={submitting}
                onClick={() => handleAction(s)}
              />
            ))}
          </div>

          <input
            type="text"
            value={comment}
            maxLength={COMMENT_MAX_LEN}
            onChange={(e) => setComment(e.target.value)}
            placeholder="Kommentar (optional)"
            aria-label="Kommentar zur Alarm-Bearbeitung"
            disabled={submitting}
            className="w-full bg-tower-bg border border-tower-border rounded-lg px-3 py-2.5 text-sm text-white
              placeholder-gray-500 focus:outline-none focus:border-tower-qdr disabled:opacity-50"
          />

          {submitError && (
            <div className="text-red-300 text-xs" role="alert">{submitError}</div>
          )}

          <div>
            <div className="text-gray-500 text-xs uppercase tracking-wider mb-1">Verlauf</div>
            {historyError && (
              <div className="text-red-300 text-xs" role="alert">{historyError}</div>
            )}
            {!historyError && historyLoading && history.length === 0 && (
              <div className="text-xs text-gray-500">Lade Verlauf…</div>
            )}
            {!historyError && !historyLoading && history.length === 0 && (
              <div className="text-xs text-gray-500">Noch keine Einträge.</div>
            )}
            {history.length > 0 && (
              <ul className="space-y-1.5 text-xs">
                {history.map((item) => <HistoryRow key={item.id} item={item} />)}
              </ul>
            )}
          </div>
        </>
      )}
    </div>
  )
}

function ActionButton({ state, active, busy, disabled, onClick }: {
  state: AlarmState; active: boolean; busy: boolean; disabled: boolean; onClick: () => void
}) {
  const base = 'rounded-lg px-3 py-2.5 text-sm font-medium border transition-colors disabled:opacity-50 min-h-[44px]'
  const look = active
    ? ACTIVE_STYLE[state]
    : 'bg-tower-bg border-tower-border text-gray-200 hover:border-gray-500 hover:text-white'
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={active}
      className={`${base} ${look}`}
    >
      {busy ? 'Speichern…' : BUTTON_LABELS[state]}
    </button>
  )
}

const BUTTON_LABELS: Record<AlarmState, string> = {
  acknowledged: 'Quittieren',
  retrieval_underway: ALARM_STATE_LABELS.retrieval_underway,
  resolved: ALARM_STATE_LABELS.resolved,
  false_alarm: ALARM_STATE_LABELS.false_alarm,
}

const ACTIVE_STYLE: Record<AlarmState, string> = {
  acknowledged: 'bg-cyan-900/50 border-cyan-500 text-cyan-100',
  retrieval_underway: 'bg-amber-900/50 border-amber-500 text-amber-100',
  resolved: 'bg-gray-700/60 border-gray-400 text-white',
  false_alarm: 'bg-gray-700/60 border-gray-400 text-white',
}

function HistoryRow({ item }: { item: AlarmActionItem }) {
  return (
    <li className="flex gap-2 items-baseline">
      <span className="font-mono text-gray-400 shrink-0">{formatHM(item.createdAt)}</span>
      <span className="text-gray-100 font-semibold shrink-0">
        {ALARM_STATE_LABELS[item.state] ?? item.state}
      </span>
      {/* `setBy` may be the user's e-mail; this row is only rendered inside
          the logged-in section. Long addresses are ellipsized, the full
          value is available via the tooltip. */}
      {item.setBy && (
        <span className="text-gray-500 shrink-0 truncate max-w-[9rem]" title={item.setBy}>
          {item.setBy}
        </span>
      )}
      {item.comment && <span className="text-gray-300 break-words min-w-0">{item.comment}</span>}
    </li>
  )
}

function formatHM(iso: string): string {
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })
}

/**
 * Day used for the history request (YYYY-MM-DD, UTC): always *today*.
 *
 * The backend filters the history by `created_at`, not by the takeoff day.
 * Actions are taken while the tower is on duty, i.e. on the current day –
 * even for a flight that took off before UTC midnight. Using the takeoff day
 * would therefore miss actions recorded after 00:00 UTC. A flight from a
 * previous UTC day whose actions were all recorded on that day is an edge
 * case we deliberately accept (history simply shows "Noch keine Einträge").
 */
function historyDate(): string {
  return new Date().toISOString().slice(0, 10)
}
