/**
 * Flight log page with pagination and filters.
 * Shows historical flight data from PostgreSQL.
 */
import { useState, useEffect } from 'react'
import { api, ApiError } from '../api/client'

interface FlightLog {
  id: number
  registration: string
  competition_sign: string
  aircraft_model: string
  takeoff_time: string
  landing_time: string | null
  flight_duration_s: number | null
  max_altitude_m: number
  max_distance_m: number
  launch_type: string | null
  end_status: string
}

interface PagedResult {
  items: FlightLog[]
  total: number
  page: number
  pages: number
}

export default function FlightLogPage() {
  const [logs, setLogs] = useState<FlightLog[]>([])
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)
  const [total, setTotal] = useState(0)
  const [dateFilter, setDateFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [exporting, setExporting] = useState(false)

  useEffect(() => {
    loadLogs()
  }, [page, dateFilter]) // eslint-disable-line react-hooks/exhaustive-deps

  async function loadLogs() {
    setLoading(true)
    setError('')
    try {
      const params: Record<string, string> = { page: String(page), per_page: '25' }
      if (dateFilter) params.date = dateFilter
      const data = await api.get<PagedResult>('/flight-log/', params)
      setLogs(data.items)
      setTotalPages(data.pages)
      setTotal(data.total)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Laden fehlgeschlagen')
    } finally {
      setLoading(false)
    }
  }

  async function handleExportCsv() {
    setExporting(true)
    try {
      const params: Record<string, string> = {}
      if (dateFilter) params.date = dateFilter

      const qs = new URLSearchParams(params).toString()
      const token = localStorage.getItem('token')
      const res = await fetch(`/api/flight-log/export/csv${qs ? '?' + qs : ''}`, {
        headers: { Authorization: `Bearer ${token || ''}` },
      })
      if (!res.ok) throw new Error('Export fehlgeschlagen')

      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `fluglog-${dateFilter || 'alle'}.csv`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Export fehlgeschlagen')
    } finally {
      setExporting(false)
    }
  }

  function formatDuration(seconds: number | null): string {
    if (!seconds) return '-'
    const h = Math.floor(seconds / 3600)
    const m = Math.floor((seconds % 3600) / 60)
    return `${h}:${String(m).padStart(2, '0')}`
  }

  function formatTime(iso: string | null): string {
    if (!iso) return '-'
    return new Date(iso).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })
  }

  function formatLaunch(lt: string | null): string {
    switch (lt) {
      case 'winch': return 'Winde'
      case 'aerotow': return 'F-Schlepp'
      case 'self': return 'Eigen'
      default: return '-'
    }
  }

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold text-white">Flugbuch</h2>
        <div className="flex items-center gap-3">
          <input
            type="date"
            value={dateFilter}
            onChange={(e) => { setDateFilter(e.target.value); setPage(1) }}
            className="bg-tower-bg border border-tower-border rounded-lg px-3 py-2 text-white text-sm focus:outline-none focus:border-tower-qdr"
          />
          <button
            onClick={handleExportCsv}
            disabled={exporting}
            className="bg-green-700 hover:bg-green-600 text-white text-sm font-semibold rounded-lg px-4 py-2 transition-colors disabled:opacity-50"
          >
            {exporting ? 'Exportieren...' : 'CSV Export'}
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-red-900/50 border border-red-500 text-red-200 rounded-lg p-3 mb-4 text-sm">{error}</div>
      )}

      <div className="bg-tower-surface border border-tower-border rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-tower-border text-left text-gray-500 uppercase text-xs">
              <th className="px-4 py-3">Kennzeichen</th>
              <th className="px-4 py-3">WB</th>
              <th className="px-4 py-3">Typ</th>
              <th className="px-4 py-3">Start</th>
              <th className="px-4 py-3">Landung</th>
              <th className="px-4 py-3">Dauer</th>
              <th className="px-4 py-3">Max H</th>
              <th className="px-4 py-3">Max D</th>
              <th className="px-4 py-3">Startart</th>
              <th className="px-4 py-3">Status</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={10} className="px-4 py-8 text-center text-gray-500">Laden...</td></tr>
            ) : logs.length === 0 ? (
              <tr><td colSpan={10} className="px-4 py-8 text-center text-gray-500">Keine Eintraege</td></tr>
            ) : (
              logs.map((log) => (
                <tr key={log.id} className="border-b border-tower-border/50 hover:bg-white/5">
                  <td className="px-4 py-2.5 text-white font-medium">{log.registration || '-'}</td>
                  <td className="px-4 py-2.5 text-gray-300">{log.competition_sign || '-'}</td>
                  <td className="px-4 py-2.5 text-gray-400 text-xs">{log.aircraft_model || '-'}</td>
                  <td className="px-4 py-2.5 text-gray-300 font-mono">{formatTime(log.takeoff_time)}</td>
                  <td className="px-4 py-2.5 text-gray-300 font-mono">{formatTime(log.landing_time)}</td>
                  <td className="px-4 py-2.5 text-gray-300 font-mono">{formatDuration(log.flight_duration_s)}</td>
                  <td className="px-4 py-2.5 text-tower-altitude font-mono">{log.max_altitude_m}m</td>
                  <td className="px-4 py-2.5 text-tower-distance font-mono">{(log.max_distance_m / 1000).toFixed(1)}km</td>
                  <td className="px-4 py-2.5 text-gray-300">{formatLaunch(log.launch_type)}</td>
                  <td className="px-4 py-2.5">
                    <span className={`px-2 py-0.5 rounded text-xs ${
                      log.end_status === 'landing' ? 'bg-green-900/50 text-green-300' :
                      log.end_status === 'outlanding' ? 'bg-orange-900/50 text-orange-300' :
                      'bg-gray-800 text-gray-400'
                    }`}>
                      {log.end_status}
                    </span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between mt-4">
          <div className="text-sm text-gray-500">{total} Fluege gesamt</div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage(Math.max(1, page - 1))}
              disabled={page <= 1}
              className="px-3 py-1.5 text-sm bg-tower-surface border border-tower-border rounded-lg text-gray-300 hover:bg-white/5 disabled:opacity-30"
            >
              Zurueck
            </button>
            <span className="text-sm text-gray-400">Seite {page} / {totalPages}</span>
            <button
              onClick={() => setPage(Math.min(totalPages, page + 1))}
              disabled={page >= totalPages}
              className="px-3 py-1.5 text-sm bg-tower-surface border border-tower-border rounded-lg text-gray-300 hover:bg-white/5 disabled:opacity-30"
            >
              Weiter
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
