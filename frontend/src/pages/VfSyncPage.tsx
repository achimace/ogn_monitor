/**
 * Vereinsflieger-Sync page (VF-Sync AP-10):
 * status header, tenant config (credentials write-only), sessions and audit.
 */
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { ApiError } from '../api/client'
import {
  getVfSyncAudit,
  getVfSyncConfig,
  getVfSyncSessions,
  getVfSyncStatus,
  putVfSyncConfig,
} from '../api/vfsync'
import { md5Hex } from '../lib/md5'
import { useAirfields } from '../hooks/useAirfields'
import type {
  VfAuditEntry,
  VfBudgetStage,
  VfSession,
  VfSessionState,
  VfSyncConfig,
  VfSyncConfigUpdate,
  VfSyncFlags,
  VfSyncStatus,
} from '../types/vfsync'

const STATUS_POLL_MS = 30_000

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Local-date ISO (YYYY-MM-DD), matching what <input type="date"> uses. */
function localIso(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}
const todayIso = localIso(new Date())

function shiftDate(iso: string, deltaDays: number): string {
  const d = new Date(iso + 'T12:00:00') // noon avoids DST edge cases
  d.setDate(d.getDate() + deltaDays)
  return localIso(d)
}

/** Local time HH:MM from an ISO UTC string. */
function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '-'
  return d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
}

/** Local date+time for audit rows. */
function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '-'
  return d.toLocaleString('de-DE', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

/** Human-readable age ("vor 3 min") for a timestamp; null → "nie". */
function fmtAge(iso: string | null | undefined, now: number): string {
  if (!iso) return 'nie'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return '-'
  const s = Math.max(0, Math.round((now - t) / 1000))
  if (s < 60) return `vor ${s} s`
  const m = Math.round(s / 60)
  if (m < 60) return `vor ${m} min`
  const h = Math.round(m / 60)
  if (h < 48) return `vor ${h} h`
  return `vor ${Math.round(h / 24)} Tagen`
}

function fmtStartType(t: string | null): string {
  switch (t) {
    case 'aerotow': return 'F-Schlepp'
    case 'aerotow_ambiguous': return 'F-Schlepp?'
    case 'winch': return 'Winde'
    case 'self': return 'Eigenstart'
    case 'powered': return 'Motorflug'
    case 'unknown': return 'Unbekannt'
    case null: return '-'
    default: return t
  }
}

const STATE_LABEL: Record<VfSessionState, string> = {
  tracking: 'Tracking',
  awaiting_match: 'Wartet auf VF-Flug',
  matched: 'Zugeordnet',
  departure_written: 'Start geschrieben',
  completed: 'Abgeschlossen',
  review: 'Pruefen',
  expired: 'Abgelaufen',
}

const STATE_CLASS: Record<VfSessionState, string> = {
  tracking: 'bg-blue-900/50 text-blue-300',
  awaiting_match: 'bg-amber-900/50 text-amber-300',
  matched: 'bg-cyan-900/50 text-cyan-300',
  departure_written: 'bg-cyan-900/50 text-cyan-200',
  completed: 'bg-green-900/50 text-green-300',
  review: 'bg-red-900/70 text-red-200 border border-red-500',
  expired: 'bg-gray-800 text-gray-400',
}

const STAGE_LABEL: Record<VfBudgetStage, string> = {
  normal: 'Normalbetrieb',
  no_live_departure: 'Keine Live-Startzeit mehr',
  aerotow_only: 'Nur noch F-Schlepp',
  hard_stop: 'Hard-Stop – nichts wird geschrieben',
}

const STAGE_BAR_CLASS: Record<VfBudgetStage, string> = {
  normal: 'bg-green-500',
  no_live_departure: 'bg-amber-500',
  aerotow_only: 'bg-orange-500',
  hard_stop: 'bg-red-600',
}

const ACTION_CLASS: Record<string, string> = {
  edit: 'text-green-300',
  dryrun_edit: 'text-amber-300',
  match: 'text-cyan-300',
  get: 'text-gray-400',
  abstain: 'text-orange-300',
  error: 'text-red-300',
  recovery: 'text-purple-300',
}

const FLAG_DEFS: { key: keyof VfSyncFlags; label: string; hint: string }[] = [
  { key: 'live_release', label: 'Ausklinkhoehe live schreiben', hint: 'Schlepphoehe sofort beim Ausklinken statt erst mit dem Lande-Update.' },
  { key: 'live_touchgo', label: 'Touch & Go live zaehlen', hint: 'Landungszaehler bei jedem Touch & Go sofort aktualisieren (mehr API-Calls).' },
  { key: 'auto_create', label: 'Fluege automatisch anlegen', hint: 'Fehlenden VF-Flug selbst anlegen (flight/add). In V1 nicht empfohlen.' },
  { key: 'join_towflights', label: 'Schleppfluege verknuepfen', hint: 'jointowflights in VF setzen. In V1 nicht empfohlen.' },
]

const SESSION_STATES: VfSessionState[] = [
  'tracking', 'awaiting_match', 'matched', 'departure_written', 'completed', 'review', 'expired',
]

function fieldsToText(obj: Record<string, unknown> | null): string {
  if (!obj) return ''
  return Object.entries(obj)
    .map(([k, v]) => `${k}=${typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v)}`)
    .join('  ')
}

function errMsg(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback
}

interface ConfigForm {
  dry_run: boolean
  vf_base_url: string
  vf_cid: string
  vf_username: string
  vf_password: string
  vf_appkey: string
  flags: VfSyncFlags
  daily_budget: string
}

function toForm(cfg: VfSyncConfig): ConfigForm {
  return {
    dry_run: cfg.dry_run,
    vf_base_url: cfg.vf_base_url,
    vf_cid: cfg.vf_cid == null ? '' : String(cfg.vf_cid),
    vf_username: cfg.vf_username ?? '',
    vf_password: '',
    vf_appkey: '',
    flags: { ...cfg.flags },
    daily_budget: String(cfg.daily_budget),
  }
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function VfSyncPage() {
  const { airfields, airfieldId, setAirfieldId, loading: afLoading, error: afError } = useAirfields()

  // Status (polled)
  const [status, setStatus] = useState<VfSyncStatus | null>(null)
  const [statusError, setStatusError] = useState('')
  const [now, setNow] = useState(() => Date.now())

  // Config
  const [config, setConfig] = useState<VfSyncConfig | null>(null)
  const [form, setForm] = useState<ConfigForm | null>(null)
  const [configError, setConfigError] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState('')
  const [saveError, setSaveError] = useState('')

  // Sessions
  const [date, setDate] = useState(todayIso)
  const [stateFilter, setStateFilter] = useState<VfSessionState | ''>('')
  const [page, setPage] = useState(1)
  const [sessions, setSessions] = useState<VfSession[]>([])
  const [sessionsTotal, setSessionsTotal] = useState(0)
  const [sessionsPages, setSessionsPages] = useState(1)
  const [sessionsLoading, setSessionsLoading] = useState(false)
  const [sessionsError, setSessionsError] = useState('')
  const [selectedSession, setSelectedSession] = useState<VfSession | null>(null)

  // Audit
  const [audit, setAudit] = useState<VfAuditEntry[]>([])
  const [auditLoading, setAuditLoading] = useState(false)
  const [auditError, setAuditError] = useState('')

  // --- loaders -------------------------------------------------------------

  const loadStatus = useCallback(async (id: string) => {
    try {
      const s = await getVfSyncStatus(id)
      setStatus(s)
      setStatusError('')
    } catch (e) {
      setStatusError(errMsg(e, 'Status laden fehlgeschlagen'))
    } finally {
      setNow(Date.now())
    }
  }, [])

  const loadConfig = useCallback(async (id: string) => {
    try {
      const cfg = await getVfSyncConfig(id)
      setConfig(cfg)
      setForm(toForm(cfg))
      setConfigError('')
    } catch (e) {
      setConfigError(errMsg(e, 'Konfiguration laden fehlgeschlagen'))
    }
  }, [])

  const loadSessions = useCallback(async (id: string, d: string, st: VfSessionState | '', p: number) => {
    setSessionsLoading(true)
    setSessionsError('')
    try {
      const res = await getVfSyncSessions({ airfieldId: id, date: d, state: st || undefined, page: p })
      setSessions(res.items)
      setSessionsTotal(res.total)
      setSessionsPages(Math.max(1, res.pages))
    } catch (e) {
      setSessionsError(errMsg(e, 'Sessions laden fehlgeschlagen'))
    } finally {
      setSessionsLoading(false)
    }
  }, [])

  const loadAudit = useCallback(async (id: string, sessionId?: string) => {
    setAuditLoading(true)
    setAuditError('')
    try {
      const res = await getVfSyncAudit({ airfieldId: id, sessionId })
      setAudit(res.items)
    } catch (e) {
      setAuditError(errMsg(e, 'Audit laden fehlgeschlagen'))
    } finally {
      setAuditLoading(false)
    }
  }, [])

  // --- effects -------------------------------------------------------------

  // Airfield change: reset and load config + status; poll status every 30 s.
  useEffect(() => {
    if (!airfieldId) return
    setSelectedSession(null)
    setSaveMsg('')
    setSaveError('')
    loadConfig(airfieldId)
    loadStatus(airfieldId)
    const timer = window.setInterval(() => loadStatus(airfieldId), STATUS_POLL_MS)
    return () => window.clearInterval(timer)
  }, [airfieldId, loadConfig, loadStatus])

  // Sessions follow date / filter / page.
  useEffect(() => {
    if (!airfieldId) return
    loadSessions(airfieldId, date, stateFilter, page)
  }, [airfieldId, date, stateFilter, page, loadSessions])

  // Audit follows the selected session (or shows the latest entries).
  useEffect(() => {
    if (!airfieldId) return
    loadAudit(airfieldId, selectedSession?.session_id)
  }, [airfieldId, selectedSession, loadAudit])

  // --- handlers ------------------------------------------------------------

  function setFormField<K extends keyof ConfigForm>(key: K, value: ConfigForm[K]) {
    setForm((f) => (f ? { ...f, [key]: value } : f))
  }

  async function handleSave(e: FormEvent) {
    e.preventDefault()
    if (!airfieldId || !form) return
    setSaving(true)
    setSaveMsg('')
    setSaveError('')

    const cidTrim = form.vf_cid.trim()
    const cid = cidTrim === '' ? null : Number.parseInt(cidTrim, 10)
    if (cid !== null && Number.isNaN(cid)) {
      setSaveError('Vereinsnummer (CID) muss eine Zahl sein')
      setSaving(false)
      return
    }
    const budget = Number.parseInt(form.daily_budget, 10)
    if (Number.isNaN(budget) || budget < 1) {
      setSaveError('Tagesbudget muss eine positive Zahl sein')
      setSaving(false)
      return
    }

    const body: VfSyncConfigUpdate = {
      dry_run: form.dry_run,
      vf_base_url: form.vf_base_url.trim(),
      vf_cid: cid,
      vf_username: form.vf_username.trim() || null,
      flags: form.flags,
      daily_budget: budget,
    }
    // Credentials only when entered – plaintext password never leaves the browser.
    if (form.vf_password.length > 0) body.vf_password_md5 = md5Hex(form.vf_password)
    if (form.vf_appkey.trim().length > 0) body.vf_appkey = form.vf_appkey.trim()

    try {
      await putVfSyncConfig(airfieldId, body)
      setSaveMsg('Konfiguration gespeichert')
      await loadConfig(airfieldId)
      await loadStatus(airfieldId)
    } catch (err) {
      setSaveError(errMsg(err, 'Speichern fehlgeschlagen'))
    } finally {
      setSaving(false)
    }
  }

  function handleRowClick(s: VfSession) {
    setSelectedSession((cur) => (cur?.session_id === s.session_id ? null : s))
  }

  // --- render --------------------------------------------------------------

  if (afLoading) return <div className="p-6 text-gray-400">Laden...</div>
  if (afError) return <div className="p-6 text-red-300">{afError}</div>
  if (!airfieldId) {
    return (
      <div className="p-6 text-gray-400">
        Kein Flugplatz vorhanden – bitte zuerst unter <strong className="text-gray-200">Flugplatz</strong> anlegen.
      </div>
    )
  }

  const enabled = status?.enabled ?? config?.enabled ?? false
  const dryRun = status?.dry_run ?? config?.dry_run ?? true

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h2 className="text-xl font-bold text-white">Vereinsflieger-Sync</h2>
        {airfields.length > 1 && (
          <div className="flex flex-wrap gap-2">
            {airfields.map((af) => (
              <button
                key={af.id}
                type="button"
                onClick={() => setAirfieldId(af.id)}
                className={`px-3 py-1.5 text-sm font-medium rounded-lg border transition-colors ${
                  af.id === airfieldId
                    ? 'bg-tower-qdr text-white border-tower-qdr'
                    : 'bg-tower-surface text-gray-400 hover:text-white border-tower-border'
                }`}
              >
                {af.name}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* (a) Status header */}
      <section className="bg-tower-surface border border-tower-border rounded-xl p-5 space-y-4">
        <div className="flex items-center flex-wrap gap-3">
          <Badge
            className={enabled ? 'bg-green-900/60 text-green-200 border-green-500' : 'bg-gray-800 text-gray-300 border-gray-600'}
          >
            {enabled ? 'Freigeschaltet' : 'Nicht freigeschaltet'}
          </Badge>
          {dryRun ? (
            <Badge className="bg-amber-500/20 text-amber-200 border-amber-400 text-base font-bold">
              Dry-Run – es wird nichts geschrieben
            </Badge>
          ) : (
            <Badge className="bg-red-900/60 text-red-200 border-red-500 font-bold">
              Scharf – schreibt in Vereinsflieger
            </Badge>
          )}
          {statusError && <span className="text-sm text-red-300">{statusError}</span>}
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* Budget */}
          <div className="bg-tower-bg border border-tower-border rounded-lg p-4">
            <div className="text-xs text-gray-500 uppercase tracking-wider mb-2">
              API-Budget {status?.budget.day ? `(${status.budget.day})` : ''}
            </div>
            {status ? (
              <BudgetBar used={status.budget.used} total={status.budget.daily_budget} stage={status.budget.stage} />
            ) : (
              <div className="text-gray-500 text-sm">-</div>
            )}
          </div>

          {/* Worker health */}
          <div className="bg-tower-bg border border-tower-border rounded-lg p-4">
            <div className="text-xs text-gray-500 uppercase tracking-wider mb-2">VF-Sync-Worker</div>
            {status?.worker ? (
              <>
                <div className={`text-xl font-bold ${
                  status.worker.status === 'ok' || status.worker.status === 'running' ? 'text-green-400'
                    : status.worker.status === 'degraded' ? 'text-amber-400'
                    : 'text-red-400'
                }`}>
                  {status.worker.status}
                </div>
                <div className="text-xs text-gray-400 mt-1">
                  Letztes Event: <span className="text-gray-200">{fmtAge(status.worker.last_event_ts, now)}</span>
                </div>
                <div className="text-xs text-gray-500">
                  Heartbeat: {fmtAge(status.worker.updated_at, now)}
                </div>
              </>
            ) : (
              <div className="text-xl font-bold text-red-400">
                kein Heartbeat
                <div className="text-xs font-normal text-gray-500 mt-1">Worker laeuft nicht oder meldet sich nicht</div>
              </div>
            )}
          </div>

          {/* Session counters */}
          <div className="bg-tower-bg border border-tower-border rounded-lg p-4">
            <div className="text-xs text-gray-500 uppercase tracking-wider mb-2">Sessions</div>
            <div className="grid grid-cols-4 gap-2 text-center">
              <Counter label="Offen" value={status?.sessions.open} color="text-blue-300" />
              <Counter label="Wartend" value={status?.sessions.awaiting_match} color="text-amber-300" />
              <Counter label="Pruefen" value={status?.sessions.review} color={status && status.sessions.review > 0 ? 'text-red-400' : 'text-gray-300'} />
              <Counter label="Heute fertig" value={status?.sessions.completed_today} color="text-green-300" />
            </div>
          </div>
        </div>
      </section>

      {/* (b) Config form */}
      <section className="bg-tower-surface border border-tower-border rounded-xl p-5">
        <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wider mb-4">Konfiguration</h3>
        {configError && (
          <div className="bg-red-900/50 border border-red-500 text-red-200 rounded-lg p-3 mb-4 text-sm">{configError}</div>
        )}
        {form && config && (
          <form onSubmit={handleSave} className="space-y-5">
            <div className="flex flex-wrap items-center gap-6">
              <div className="flex items-center gap-3">
                <input
                  id="vf-enabled"
                  type="checkbox"
                  checked={config.enabled}
                  disabled
                  className="w-5 h-5 accent-tower-qdr opacity-60 cursor-not-allowed"
                />
                <label htmlFor="vf-enabled" className="text-sm text-gray-300">
                  Freigeschaltet
                  <span className="block text-xs text-gray-500">Freischaltung erfolgt durch den Betreiber</span>
                </label>
              </div>
              <div className="flex items-center gap-3">
                <input
                  id="vf-dryrun"
                  type="checkbox"
                  checked={form.dry_run}
                  onChange={(e) => setFormField('dry_run', e.target.checked)}
                  className="w-5 h-5 accent-amber-400"
                />
                <label htmlFor="vf-dryrun" className="text-sm text-gray-300">
                  Dry-Run
                  <span className="block text-xs text-gray-500">Alles wird simuliert und protokolliert, nichts nach VF geschrieben</span>
                </label>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <Field label="VF Base-URL" value={form.vf_base_url} onChange={(v) => setFormField('vf_base_url', v)} placeholder="https://www.vereinsflieger.de" />
              <Field label="Vereinsnummer (CID)" value={form.vf_cid} onChange={(v) => setFormField('vf_cid', v)} type="number" placeholder="optional" />
              <Field label="Technischer Benutzer" value={form.vf_username} onChange={(v) => setFormField('vf_username', v)} placeholder="vf-sync@verein.de" autoComplete="off" />
              <Field
                label="Passwort"
                value={form.vf_password}
                onChange={(v) => setFormField('vf_password', v)}
                type="password"
                placeholder={config.has_password ? 'unveraendert lassen' : 'Passwort eingeben'}
                autoComplete="new-password"
                hint={<SetHint set={config.has_password} note="wird nur als MD5-Hash uebertragen" />}
              />
              <Field
                label="AppKey"
                value={form.vf_appkey}
                onChange={(v) => setFormField('vf_appkey', v)}
                type="password"
                placeholder={config.has_appkey ? 'unveraendert lassen' : 'AppKey eingeben'}
                autoComplete="new-password"
                hint={<SetHint set={config.has_appkey} />}
              />
              <Field
                label="Tagesbudget (API-Calls)"
                value={form.daily_budget}
                onChange={(v) => setFormField('daily_budget', v)}
                type="number"
                hint="VF-Limit 500/Tag je AppKey – Standard 450"
              />
            </div>

            <div>
              <div className="text-sm text-gray-400 mb-2">Optionen (Feature-Flags)</div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {FLAG_DEFS.map((f) => (
                  <label key={f.key} className="flex items-start gap-3 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={form.flags[f.key]}
                      onChange={(e) => setFormField('flags', { ...form.flags, [f.key]: e.target.checked })}
                      className="w-5 h-5 mt-0.5 accent-tower-qdr"
                    />
                    <span className="text-sm text-gray-300">
                      {f.label}
                      <span className="block text-xs text-gray-500">{f.hint}</span>
                    </span>
                  </label>
                ))}
              </div>
            </div>

            {saveError && (
              <div className="bg-red-900/50 border border-red-500 text-red-200 rounded-lg p-3 text-sm">{saveError}</div>
            )}
            {saveMsg && (
              <div className="bg-green-900/50 border border-green-500 text-green-200 rounded-lg p-3 text-sm">{saveMsg}</div>
            )}

            <div className="flex items-center gap-4">
              <button
                type="submit"
                disabled={saving}
                className="bg-tower-qdr hover:bg-cyan-500 text-white font-semibold rounded-lg px-6 py-2.5 transition-colors disabled:opacity-50"
              >
                {saving ? 'Wird gespeichert...' : 'Speichern'}
              </button>
              {config.updated_at && (
                <span className="text-xs text-gray-500">Zuletzt geaendert: {fmtDateTime(config.updated_at)}</span>
              )}
            </div>
          </form>
        )}
      </section>

      {/* (c) Sessions */}
      <section>
        <div className="flex items-center justify-between flex-wrap gap-3 mb-3">
          <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Sessions</h3>
          <div className="flex items-center gap-3">
            <select
              value={stateFilter}
              onChange={(e) => { setStateFilter(e.target.value as VfSessionState | ''); setPage(1) }}
              aria-label="Status-Filter"
              className="bg-tower-bg border border-tower-border rounded-lg px-3 py-2 text-white text-sm focus:outline-none focus:border-tower-qdr"
            >
              <option value="">Alle Status</option>
              {SESSION_STATES.map((s) => <option key={s} value={s}>{STATE_LABEL[s]}</option>)}
            </select>
            <div className="flex items-stretch rounded-lg border border-tower-border overflow-hidden">
              <button
                type="button"
                onClick={() => { setDate(shiftDate(date || todayIso, -1)); setPage(1) }}
                title="Ein Tag zurueck"
                className="px-3 bg-tower-bg hover:bg-tower-surface text-gray-300 hover:text-white text-base font-bold transition-colors"
              >
                ‹
              </button>
              <input
                type="date"
                value={date}
                max={todayIso}
                onChange={(e) => { if (e.target.value) { setDate(e.target.value); setPage(1) } }}
                className="bg-tower-bg px-3 py-2 text-white text-sm focus:outline-none border-l border-r border-tower-border"
              />
              <button
                type="button"
                onClick={() => { setDate(shiftDate(date || todayIso, 1)); setPage(1) }}
                disabled={date >= todayIso}
                title="Ein Tag vor"
                className="px-3 bg-tower-bg hover:bg-tower-surface text-gray-300 hover:text-white text-base font-bold transition-colors
                  disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:text-gray-300 disabled:hover:bg-tower-bg"
              >
                ›
              </button>
            </div>
            <button
              type="button"
              onClick={() => loadSessions(airfieldId, date, stateFilter, page)}
              className="px-3 py-2 text-sm bg-tower-surface border border-tower-border rounded-lg text-gray-300 hover:bg-white/5"
            >
              Aktualisieren
            </button>
          </div>
        </div>

        {sessionsError && (
          <div className="bg-red-900/50 border border-red-500 text-red-200 rounded-lg p-3 mb-3 text-sm">{sessionsError}</div>
        )}

        <div className="bg-tower-surface border border-tower-border rounded-xl overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-tower-border text-left text-gray-500 uppercase text-xs">
                <th className="px-3 py-3">Kennz.</th>
                <th className="px-3 py-3">Start</th>
                <th className="px-3 py-3">Landung</th>
                <th className="px-3 py-3">Startart</th>
                <th className="px-3 py-3">Schlepper</th>
                <th className="px-3 py-3 text-right">Ausklink AGL</th>
                <th className="px-3 py-3 text-right">Schlepp</th>
                <th className="px-3 py-3 text-right">Ldg</th>
                <th className="px-3 py-3">Status</th>
                <th className="px-3 py-3 text-right">VF-Flug</th>
              </tr>
            </thead>
            <tbody>
              {sessionsLoading && sessions.length === 0 ? (
                <tr><td colSpan={10} className="px-4 py-8 text-center text-gray-500">Laden...</td></tr>
              ) : sessions.length === 0 ? (
                <tr><td colSpan={10} className="px-4 py-8 text-center text-gray-500">Keine Sessions</td></tr>
              ) : (
                sessions.map((s) => {
                  const selected = selectedSession?.session_id === s.session_id
                  return (
                    <tr
                      key={s.session_id}
                      onClick={() => handleRowClick(s)}
                      className={`border-b border-tower-border/50 cursor-pointer ${
                        selected ? 'bg-tower-qdr/15' : s.state === 'review' ? 'bg-red-900/20 hover:bg-red-900/30' : 'hover:bg-white/5'
                      }`}
                    >
                      <td className="px-3 py-2.5 text-white font-medium whitespace-nowrap">
                        {s.registration || <span className="text-gray-500 font-mono text-xs">{s.flarm_id}</span>}
                        {s.landing_method === 'silence' && (
                          <span className="ml-1 text-xs text-gray-500" title="Landung durch Funkstille angenommen">(still)</span>
                        )}
                      </td>
                      <td className="px-3 py-2.5 text-gray-300 font-mono">{fmtTime(s.takeoff_ts)}</td>
                      <td className="px-3 py-2.5 text-gray-300 font-mono">{fmtTime(s.landing_ts)}</td>
                      <td className="px-3 py-2.5 text-gray-300 whitespace-nowrap">{fmtStartType(s.start_type_detected)}</td>
                      <td className="px-3 py-2.5 text-gray-300">{s.tow_registration || '-'}</td>
                      <td className="px-3 py-2.5 text-tower-altitude font-mono text-right">
                        {s.release_alt_agl_m != null ? `${s.release_alt_agl_m} m` : '-'}
                        {s.release_method === 'towplane_max' && (
                          <span className="ml-1 text-xs text-gray-500" title="Fallback: Maximalhoehe des Schleppers">*</span>
                        )}
                      </td>
                      <td className="px-3 py-2.5 text-gray-300 font-mono text-right">{s.tow_time_min != null ? `${s.tow_time_min} min` : '-'}</td>
                      <td className="px-3 py-2.5 text-gray-300 font-mono text-right">{s.landing_count}</td>
                      <td className="px-3 py-2.5">
                        <span className={`px-2 py-0.5 rounded text-xs whitespace-nowrap ${STATE_CLASS[s.state] ?? 'bg-gray-800 text-gray-400'}`}>
                          {STATE_LABEL[s.state] ?? s.state}
                        </span>
                        {s.state === 'review' && s.review_reason && (
                          <div className="text-xs text-red-300 mt-1 max-w-xs" title={s.review_reason}>{s.review_reason}</div>
                        )}
                      </td>
                      <td className="px-3 py-2.5 text-gray-300 font-mono text-right">{s.matched_flid ?? '-'}</td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>

        {sessionsPages > 1 && (
          <div className="flex items-center justify-between mt-3">
            <div className="text-sm text-gray-500">{sessionsTotal} Sessions gesamt</div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setPage(Math.max(1, page - 1))}
                disabled={page <= 1}
                className="px-3 py-1.5 text-sm bg-tower-surface border border-tower-border rounded-lg text-gray-300 hover:bg-white/5 disabled:opacity-30"
              >
                Zurueck
              </button>
              <span className="text-sm text-gray-400">Seite {page} / {sessionsPages}</span>
              <button
                onClick={() => setPage(Math.min(sessionsPages, page + 1))}
                disabled={page >= sessionsPages}
                className="px-3 py-1.5 text-sm bg-tower-surface border border-tower-border rounded-lg text-gray-300 hover:bg-white/5 disabled:opacity-30"
              >
                Weiter
              </button>
            </div>
          </div>
        )}
      </section>

      {/* (d) Audit */}
      <section>
        <div className="flex items-center justify-between flex-wrap gap-3 mb-3">
          <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">
            Audit
            {selectedSession && (
              <span className="ml-2 normal-case font-normal text-gray-400">
                – Session {selectedSession.registration || selectedSession.flarm_id} {fmtTime(selectedSession.takeoff_ts)}
              </span>
            )}
          </h3>
          <div className="flex items-center gap-2">
            {selectedSession && (
              <button
                type="button"
                onClick={() => setSelectedSession(null)}
                className="px-3 py-2 text-sm bg-tower-surface border border-tower-border rounded-lg text-gray-300 hover:bg-white/5"
              >
                Alle Eintraege
              </button>
            )}
            <button
              type="button"
              onClick={() => loadAudit(airfieldId, selectedSession?.session_id)}
              className="px-3 py-2 text-sm bg-tower-surface border border-tower-border rounded-lg text-gray-300 hover:bg-white/5"
            >
              Aktualisieren
            </button>
          </div>
        </div>

        {auditError && (
          <div className="bg-red-900/50 border border-red-500 text-red-200 rounded-lg p-3 mb-3 text-sm">{auditError}</div>
        )}

        <div className="bg-tower-surface border border-tower-border rounded-xl overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-tower-border text-left text-gray-500 uppercase text-xs">
                <th className="px-3 py-3">Zeit</th>
                <th className="px-3 py-3">Aktion</th>
                <th className="px-3 py-3 text-right">VF-Flug</th>
                <th className="px-3 py-3 text-right">HTTP</th>
                <th className="px-3 py-3">Gesendete Felder</th>
                <th className="px-3 py-3">Detail</th>
              </tr>
            </thead>
            <tbody>
              {auditLoading && audit.length === 0 ? (
                <tr><td colSpan={6} className="px-4 py-8 text-center text-gray-500">Laden...</td></tr>
              ) : audit.length === 0 ? (
                <tr><td colSpan={6} className="px-4 py-8 text-center text-gray-500">Keine Audit-Eintraege</td></tr>
              ) : (
                audit.map((a) => (
                  <tr key={a.id} className={`border-b border-tower-border/50 align-top ${a.action === 'error' ? 'bg-red-900/20' : ''}`}>
                    <td className="px-3 py-2 text-gray-300 font-mono whitespace-nowrap">{fmtDateTime(a.ts)}</td>
                    <td className={`px-3 py-2 font-medium whitespace-nowrap ${ACTION_CLASS[a.action] ?? 'text-gray-300'}`}>{a.action}</td>
                    <td className="px-3 py-2 text-gray-300 font-mono text-right">{a.flid ?? '-'}</td>
                    <td className={`px-3 py-2 font-mono text-right ${
                      a.http_status == null ? 'text-gray-500' : a.http_status >= 400 ? 'text-red-300' : 'text-green-300'
                    }`}>
                      {a.http_status ?? '-'}
                    </td>
                    <td className="px-3 py-2 text-gray-400 font-mono text-xs break-all">{fieldsToText(a.fields_sent) || '-'}</td>
                    <td className="px-3 py-2 text-gray-300 text-xs">{a.detail || '-'}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Small presentational pieces
// ---------------------------------------------------------------------------

function Badge({ className, children }: { className: string; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center px-3 py-1.5 rounded-lg border text-sm ${className}`}>
      {children}
    </span>
  )
}

function Counter({ label, value, color }: { label: string; value: number | undefined; color: string }) {
  return (
    <div>
      <div className={`text-2xl font-bold ${color}`}>{value ?? '-'}</div>
      <div className="text-xs text-gray-500">{label}</div>
    </div>
  )
}

function BudgetBar({ used, total, stage }: { used: number; total: number; stage: VfBudgetStage }) {
  const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0
  const stageLabel = STAGE_LABEL[stage] ?? stage
  const barClass = STAGE_BAR_CLASS[stage] ?? 'bg-gray-500'
  return (
    <div>
      <div className="flex items-baseline justify-between mb-1">
        <span className="text-xl font-bold text-white font-mono">
          {used} <span className="text-gray-500 text-sm">/ {total}</span>
        </span>
        <span className="text-sm text-gray-400 font-mono">{pct}%</span>
      </div>
      <div className="h-3 w-full bg-gray-800 rounded overflow-hidden" role="progressbar" aria-valuenow={used} aria-valuemin={0} aria-valuemax={total}>
        <div className={`h-full ${barClass}`} style={{ width: `${pct}%` }} />
      </div>
      <div className={`text-xs mt-1 ${stage === 'normal' ? 'text-gray-400' : stage === 'hard_stop' ? 'text-red-300 font-semibold' : 'text-amber-300'}`}>
        {stageLabel}
      </div>
    </div>
  )
}

function SetHint({ set, note }: { set: boolean; note?: string }) {
  return (
    <span>
      gesetzt: <span className={set ? 'text-green-300' : 'text-red-300'}>{set ? 'ja' : 'nein'}</span>
      {note && <span className="text-gray-600"> – {note}</span>}
    </span>
  )
}

function Field({ label, value, onChange, type = 'text', placeholder, hint, autoComplete }: {
  label: string
  value: string
  onChange: (v: string) => void
  type?: string
  placeholder?: string
  hint?: ReactNode
  autoComplete?: string
}) {
  return (
    <div>
      <label className="block text-sm text-gray-400 mb-1">{label}</label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoComplete={autoComplete}
        className="w-full bg-tower-bg border border-tower-border rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-tower-qdr"
      />
      {hint && <p className="text-xs text-gray-500 mt-0.5">{hint}</p>}
    </div>
  )
}
