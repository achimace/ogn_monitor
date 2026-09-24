/**
 * Typed API functions for the Vereinsflieger sync endpoints (/api/vfsync/*).
 */
import { api, ApiError } from './client'
import type {
  VfAuditPage,
  VfSessionPage,
  VfSessionState,
  VfSyncConfig,
  VfSyncConfigUpdate,
  VfSyncStatus,
} from '../types/vfsync'

export const VFSYNC_DEFAULT_BUDGET = 450

/** Defaults used when the tenant has never configured VF-Sync (GET returns 404). */
export function defaultVfSyncConfig(airfieldId: string): VfSyncConfig {
  return {
    airfield_id: airfieldId,
    enabled: false,
    dry_run: true,
    vf_base_url: 'https://www.vereinsflieger.de',
    vf_cid: null,
    vf_username: null,
    has_password: false,
    has_appkey: false,
    flags: { live_release: false, live_touchgo: false, auto_create: false, join_towflights: false },
    daily_budget: VFSYNC_DEFAULT_BUDGET,
    updated_at: null,
  }
}

/** Load the tenant config; a 404 is mapped to the defaults (not an error). */
export async function getVfSyncConfig(airfieldId: string): Promise<VfSyncConfig> {
  try {
    const cfg = await api.get<VfSyncConfig>(`/vfsync/config/${airfieldId}`)
    // Be defensive: backend may omit nested objects in early versions.
    const def = defaultVfSyncConfig(airfieldId)
    return { ...def, ...cfg, flags: { ...def.flags, ...(cfg.flags ?? {}) } }
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return defaultVfSyncConfig(airfieldId)
    throw e
  }
}

export function putVfSyncConfig(airfieldId: string, body: VfSyncConfigUpdate): Promise<VfSyncConfig> {
  return api.put<VfSyncConfig>(`/vfsync/config/${airfieldId}`, body)
}

export function getVfSyncStatus(airfieldId: string): Promise<VfSyncStatus> {
  return api.get<VfSyncStatus>(`/vfsync/status/${airfieldId}`)
}

export function getVfSyncSessions(opts: {
  airfieldId: string
  date: string
  state?: VfSessionState
  page?: number
  pageSize?: number
}): Promise<VfSessionPage> {
  const params: Record<string, string> = {
    airfield_id: opts.airfieldId,
    date: opts.date,
    page: String(opts.page ?? 1),
    page_size: String(opts.pageSize ?? 50),
  }
  if (opts.state) params.state = opts.state
  return api.get<VfSessionPage>('/vfsync/sessions', params)
}

export function getVfSyncAudit(opts: {
  airfieldId: string
  sessionId?: string
  limit?: number
}): Promise<VfAuditPage> {
  const params: Record<string, string> = {
    airfield_id: opts.airfieldId,
    limit: String(opts.limit ?? 200),
  }
  if (opts.sessionId) params.session_id = opts.sessionId
  return api.get<VfAuditPage>('/vfsync/audit', params)
}
