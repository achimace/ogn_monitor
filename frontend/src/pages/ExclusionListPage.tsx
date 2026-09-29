/**
 * Exclusion list page - manage per-airfield ignored aircraft (blacklist).
 * API: /api/airfields/{airfield_id}/ignored-aircraft (camelCase response,
 * snake_case request bodies). PUT/DELETE address an entry by its FLARM-ID.
 */
import { useState, useEffect, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'

interface Airfield {
  id: string
  name: string
  slug: string
}

/** Exclusion-list entry: /api/airfields/{airfield_id}/ignored-aircraft (camelCase response). */
interface IgnoredAircraft {
  id: string
  flarmId: string
  note?: string | null
  createdAt?: string | null
}

interface IgnoreForm {
  flarm_id: string
  note: string
}

const EMPTY_FORM: IgnoreForm = { flarm_id: '', note: '' }

const FLARM_ID_RE = /^[0-9A-F]{4,16}$/

function formatDate(iso?: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '-' : d.toLocaleDateString('de-DE')
}

const inputClass = 'w-full bg-tower-bg border border-tower-border rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-tower-qdr'

export default function ExclusionListPage() {
  const [airfieldId, setAirfieldId] = useState<string | null>(null)
  const [entries, setEntries] = useState<IgnoredAircraft[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showForm, setShowForm] = useState(false)
  // FLARM-ID of the entry being edited; null = add mode
  const [editingFlarmId, setEditingFlarmId] = useState<string | null>(null)
  const [form, setForm] = useState<IgnoreForm>(EMPTY_FORM)
  const [saving, setSaving] = useState(false)

  useEffect(() => { loadAirfield() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  async function loadAirfield() {
    try {
      const airfields = await api.get<Airfield[]>('/airfields')
      if (airfields.length > 0) {
        const afId = airfields[0]!.id
        setAirfieldId(afId)
        await loadEntries(afId)
      } else {
        setError('Bitte zuerst einen Flugplatz anlegen')
        setLoading(false)
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Laden fehlgeschlagen')
      setLoading(false)
    }
  }

  async function loadEntries(afId?: string) {
    const id = afId || airfieldId
    if (!id) return
    try {
      const data = await api.get<IgnoredAircraft[]>(`/airfields/${id}/ignored-aircraft`)
      setEntries(data)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Laden fehlgeschlagen')
    } finally {
      setLoading(false)
    }
  }

  function openAddForm() {
    setEditingFlarmId(null)
    setForm(EMPTY_FORM)
    setShowForm(true)
  }

  function openEditForm(entry: IgnoredAircraft) {
    setEditingFlarmId(entry.flarmId)
    setForm({ flarm_id: entry.flarmId, note: entry.note ?? '' })
    setShowForm(true)
  }

  function closeForm() {
    setShowForm(false)
    setEditingFlarmId(null)
    setForm(EMPTY_FORM)
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!airfieldId) return
    const flarmId = form.flarm_id.trim().toUpperCase()
    const note = form.note.trim()
    if (!FLARM_ID_RE.test(flarmId)) {
      setError('FLARM-ID muss 4-16 Hex-Zeichen haben (z. B. DD0239)')
      return
    }
    if (!note) {
      setError('Bitte einen Hinweis angeben (z. B. "Rettungshubschrauber Klinik")')
      return
    }
    setSaving(true)
    setError('')
    try {
      if (editingFlarmId) {
        await api.put(`/airfields/${airfieldId}/ignored-aircraft/${editingFlarmId}`, { note })
      } else {
        await api.post(`/airfields/${airfieldId}/ignored-aircraft`, { flarm_id: flarmId, note })
      }
      closeForm()
      await loadEntries()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError(`${flarmId}: Steht bereits auf der Ausschlussliste`)
      } else if (e instanceof ApiError && e.status === 404 && editingFlarmId) {
        setError(`${editingFlarmId}: Eintrag nicht mehr vorhanden`)
      } else {
        setError(e instanceof ApiError ? e.message : 'Speichern fehlgeschlagen')
      }
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete(flarmId: string) {
    if (!airfieldId) return
    if (!window.confirm(`${flarmId} von der Ausschlussliste entfernen?`)) return
    setError('')
    try {
      await api.delete(`/airfields/${airfieldId}/ignored-aircraft/${flarmId}`)
      await loadEntries()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Loeschen fehlgeschlagen')
    }
  }

  if (loading) return <div className="p-6 text-gray-400">Laden...</div>

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-2 gap-3 flex-wrap">
        <h2 className="text-xl font-bold text-white">Ausschlussliste</h2>
        <button
          onClick={() => (showForm && !editingFlarmId ? closeForm() : openAddForm())}
          disabled={!airfieldId}
          className="bg-tower-qdr hover:bg-cyan-500 text-white text-sm font-semibold rounded-lg px-4 py-2 transition-colors disabled:opacity-50"
        >
          + Eintrag hinzufuegen
        </button>
      </div>

      <p className="text-xs text-gray-500 mb-4">
        Geraete auf dieser Liste werden fuer diesen Flugplatz nicht mehr beobachtet – ihre Beacons
        werden verworfen, z. B. Hubschrauber einer nahen Klinik. Wirkt innerhalb weniger Sekunden;
        bestehende Flugbucheintraege bleiben erhalten.
      </p>

      {error && (
        <div className="bg-red-900/50 border border-red-500 text-red-200 rounded-lg p-3 mb-4 text-sm">{error}</div>
      )}

      {showForm && (
        <div className="bg-tower-surface border border-tower-border rounded-xl p-6 mb-6">
          <h3 className="text-sm font-semibold text-gray-300 mb-4">
            {editingFlarmId ? `Eintrag bearbeiten (${editingFlarmId})` : 'Neuer Eintrag'}
          </h3>
          <form onSubmit={handleSubmit} className="grid grid-cols-2 md:grid-cols-3 gap-4">
            <Input
              label="FLARM-ID"
              value={form.flarm_id}
              onChange={(v) => setForm({ ...form, flarm_id: v.toUpperCase() })}
              placeholder="3D1234"
              readOnly={!!editingFlarmId}
              required
            />
            <Input
              label="Hinweis"
              value={form.note}
              onChange={(v) => setForm({ ...form, note: v.slice(0, 120) })}
              placeholder="Rettungshubschrauber Klinik"
              required
            />
            <div className="flex items-end gap-3">
              <button
                type="submit"
                disabled={saving}
                className="bg-green-600 hover:bg-green-500 text-white text-sm font-semibold rounded-lg px-6 py-2.5 transition-colors disabled:opacity-50"
              >
                {saving ? 'Speichern...' : editingFlarmId ? 'Speichern' : 'Hinzufuegen'}
              </button>
              <button type="button" onClick={closeForm} className="text-gray-400 hover:text-white text-sm px-3 py-2.5">
                Abbrechen
              </button>
            </div>
          </form>
        </div>
      )}

      <div className="bg-tower-surface border border-tower-border rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-tower-border text-left text-gray-500 uppercase text-xs">
              <th className="px-4 py-3">FLARM-ID</th>
              <th className="px-4 py-3">Hinweis</th>
              <th className="px-4 py-3">Seit</th>
              <th className="px-4 py-3"></th>
            </tr>
          </thead>
          <tbody>
            {entries.length === 0 ? (
              <tr><td colSpan={4} className="px-4 py-8 text-center text-gray-500">Keine Eintraege auf der Ausschlussliste</td></tr>
            ) : (
              entries.map((entry) => (
                <tr key={entry.id} className="border-b border-tower-border/50 hover:bg-white/5">
                  <td className="px-4 py-3 text-white font-mono">{entry.flarmId}</td>
                  <td className="px-4 py-3 text-gray-300">{entry.note || '-'}</td>
                  <td className="px-4 py-3 text-gray-400 whitespace-nowrap">{formatDate(entry.createdAt)}</td>
                  <td className="px-4 py-3 text-right whitespace-nowrap">
                    <button
                      onClick={() => openEditForm(entry)}
                      className="text-gray-400 hover:text-tower-qdr text-xs px-2 py-1 mr-2"
                    >
                      Bearbeiten
                    </button>
                    <button
                      onClick={() => handleDelete(entry.flarmId)}
                      className="text-gray-500 hover:text-red-400 text-xs px-2 py-1"
                    >
                      Entfernen
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function Input({ label, value, onChange, placeholder, required, readOnly }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string; required?: boolean; readOnly?: boolean
}) {
  return (
    <div>
      <label className="block text-sm text-gray-400 mb-1">{label}</label>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        required={required}
        readOnly={readOnly}
        className={`${inputClass} ${readOnly ? 'opacity-60 cursor-not-allowed' : ''}`}
      />
    </div>
  )
}
