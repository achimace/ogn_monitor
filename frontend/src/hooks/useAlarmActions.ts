/**
 * Tower alarm handling (B2) for one aircraft in the detail drawer.
 *
 * - loads the action history (GET …/actions?date=) when the section opens
 * - submits a new action (POST …/actions), applies the returned alarm-state
 *   fields to the monitor store immediately and reloads the history
 *
 * Both endpoints need a dashboard login; the caller decides whether to show
 * the controls at all (token present). 401/403 are turned into readable
 * German messages, everything else falls back to the ApiError text.
 */
import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '../api/client'
import { fetchAlarmActions, postAlarmAction } from '../api/monitor'
import { useMonitorStore } from '../store/monitorStore'
import type { AlarmActionItem, AlarmState } from '../types/flight'

const COMMENT_MAX_LEN = 500

export { COMMENT_MAX_LEN }

interface UseAlarmActionsOptions {
  slug: string | null
  flarmId: string | null
  /** Day of the flight (YYYY-MM-DD, UTC) used for the history query. */
  date: string
  /** Only fetch when the section is actually visible and a token exists. */
  enabled: boolean
}

export interface UseAlarmActionsResult {
  history: AlarmActionItem[]
  historyLoading: boolean
  historyError: string
  submitting: boolean
  submitError: string
  submit: (state: AlarmState, comment: string) => Promise<boolean>
  reload: () => void
}

function describeError(e: unknown, fallback: string): string {
  if (e instanceof ApiError) {
    if (e.status === 401) return 'Nicht angemeldet – bitte zuerst anmelden.'
    if (e.status === 403) return 'Kein Zugriff: Dieser Flugplatz gehört zu einem anderen Verein.'
    return e.message || fallback
  }
  return fallback
}

export function useAlarmActions({ slug, flarmId, date, enabled }: UseAlarmActionsOptions): UseAlarmActionsResult {
  const applyAlarmState = useMonitorStore((s) => s.applyAlarmState)
  const [history, setHistory] = useState<AlarmActionItem[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyError, setHistoryError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const [reloadTick, setReloadTick] = useState(0)

  useEffect(() => {
    if (!enabled || !slug || !flarmId) {
      setHistory([])
      setHistoryError('')
      return
    }
    let cancelled = false
    setHistoryLoading(true)
    setHistoryError('')
    fetchAlarmActions(slug, flarmId, date)
      .then((res) => {
        if (!cancelled) setHistory(res.items)
      })
      .catch((e: unknown) => {
        if (!cancelled) setHistoryError(describeError(e, 'Verlauf konnte nicht geladen werden.'))
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false)
      })
    return () => { cancelled = true }
  }, [enabled, slug, flarmId, date, reloadTick])

  const reload = useCallback(() => setReloadTick((t) => t + 1), [])

  const submit = useCallback(async (state: AlarmState, comment: string): Promise<boolean> => {
    if (!slug || !flarmId) return false
    const trimmed = comment.trim().slice(0, COMMENT_MAX_LEN)
    setSubmitting(true)
    setSubmitError('')
    try {
      const res = await postAlarmAction(slug, flarmId, {
        state,
        ...(trimmed ? { comment: trimmed } : {}),
      })
      // Optimistic store update – the regular WebSocket `flight_update`
      // delta carries the same fields to every other monitor client.
      const a = res.alarmState
      applyAlarmState(flarmId, {
        alarmState: a?.alarmState ?? state,
        alarmComment: a?.alarmComment ?? (trimmed || null),
        alarmSetBy: a?.alarmSetBy ?? null,
        alarmSetAt: a?.alarmSetAt ?? new Date().toISOString(),
      })
      reload()
      return true
    } catch (e: unknown) {
      setSubmitError(describeError(e, 'Aktion konnte nicht gespeichert werden.'))
      return false
    } finally {
      setSubmitting(false)
    }
  }, [slug, flarmId, applyAlarmState, reload])

  return { history, historyLoading, historyError, submitting, submitError, submit, reload }
}
