/**
 * Types for the Vereinsflieger sync (VF-Sync) API: /api/vfsync/*
 * All timestamps are ISO-8601 UTC strings.
 */

export interface VfSyncFlags {
  live_release: boolean
  live_touchgo: boolean
  auto_create: boolean
  join_towflights: boolean
}

export interface VfSyncConfig {
  airfield_id: string
  enabled: boolean
  dry_run: boolean
  vf_base_url: string
  vf_cid: number | null
  vf_username: string | null
  has_password: boolean
  has_appkey: boolean
  flags: VfSyncFlags
  daily_budget: number
  updated_at: string | null
}

/** PUT body – all fields optional; credentials are write-only. */
export interface VfSyncConfigUpdate {
  dry_run?: boolean
  vf_base_url?: string
  vf_cid?: number | null
  vf_username?: string | null
  /** MD5 hex of the plaintext password entered by the user (never the plaintext). */
  vf_password_md5?: string
  vf_appkey?: string
  flags?: Partial<VfSyncFlags>
  daily_budget?: number
}

export type VfBudgetStage = 'normal' | 'no_live_departure' | 'aerotow_only' | 'hard_stop'

export interface VfSyncStatus {
  enabled: boolean
  dry_run: boolean
  budget: {
    day: string
    used: number
    daily_budget: number
    stage: VfBudgetStage
  }
  sessions: {
    open: number
    awaiting_match: number
    review: number
    completed_today: number
  }
  worker: {
    status: string
    last_event_ts: string | null
    updated_at: string | null
  } | null
}

export type VfSessionState =
  | 'tracking'
  | 'awaiting_match'
  | 'matched'
  | 'departure_written'
  | 'completed'
  | 'review'
  | 'expired'

export interface VfSession {
  session_id: string
  flarm_id: string
  registration: string | null
  takeoff_ts: string | null
  landing_ts: string | null
  landing_method: string | null
  start_type_detected: string | null
  tow_registration: string | null
  release_ts: string | null
  release_alt_agl_m: number | null
  release_method: string | null
  tow_time_min: number | null
  landing_count: number
  conf_pairing: number | null
  conf_landing: number | null
  conf_touchgo: number | null
  matched_flid: number | null
  state: VfSessionState
  review_reason: string | null
  attempts: number
  last_attempt: string | null
  created_at: string
  updated_at: string
}

export interface VfSessionPage {
  items: VfSession[]
  total: number
  page: number
  pages: number
}

export type VfAuditAction =
  | 'get'
  | 'edit'
  | 'match'
  | 'abstain'
  | 'error'
  | 'dryrun_edit'
  | 'recovery'

export interface VfAuditEntry {
  id: number
  ts: string
  action: VfAuditAction | string
  session_id: string | null
  flid: number | null
  fields_sent: Record<string, unknown> | null
  pre_state: Record<string, unknown> | null
  http_status: number | null
  detail: string | null
}

export interface VfAuditPage {
  items: VfAuditEntry[]
}
