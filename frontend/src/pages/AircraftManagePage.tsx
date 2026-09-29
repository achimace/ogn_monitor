/**
 * Aircraft management page - add/edit/delete tenant aircraft, CSV import.
 * Aircraft API is nested: /api/airfields/{airfield_id}/aircraft
 * PUT and DELETE address an aircraft by its FLARM-ID (not by the row id).
 */
import { useState, useEffect, useRef, type ChangeEvent, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'

interface Airfield {
  id: string
  name: string
  slug: string
}

interface Aircraft {
  id: string
  registration: string
  competition_sign: string | null
  flarm_id: string
  aircraft_model: string | null
  aircraft_type: string | null
  is_active: boolean
}

interface CsvImportResult {
  imported: number
  skipped: number
  errors: string[]
}

interface AircraftForm {
  registration: string
  competition_sign: string
  flarm_id: string
  aircraft_model: string
  aircraft_type: string
}

const EMPTY_FORM: AircraftForm = {
  registration: '', competition_sign: '', flarm_id: '',
  aircraft_model: '', aircraft_type: 'glider',
}

const inputClass = 'w-full bg-tower-bg border border-tower-border rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-tower-qdr'

export default function AircraftManagePage() {
  const [airfieldId, setAirfieldId] = useState<string | null>(null)
  const [aircraft, setAircraft] = useState<Aircraft[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showForm, setShowForm] = useState(false)
  // FLARM-ID of the aircraft being edited; null = add mode
  const [editingFlarmId, setEditingFlarmId] = useState<string | null>(null)
  const [form, setForm] = useState<AircraftForm>(EMPTY_FORM)
  const [saving, setSaving] = useState(false)
  const [importing, setImporting] = useState(false)
  const [importResult, setImportResult] = useState<CsvImportResult | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { loadAirfield() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  async function loadAirfield() {
    try {
      const airfields = await api.get<Airfield[]>('/airfields')
      if (airfields.length > 0) {
        const afId = airfields[0]!.id
        setAirfieldId(afId)
        await loadAircraft(afId)
      } else {
        setError('Bitte zuerst einen Flugplatz anlegen')
        setLoading(false)
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Laden fehlgeschlagen')
      setLoading(false)
    }
  }

  async function loadAircraft(afId?: string) {
    const id = afId || airfieldId
    if (!id) return
    try {
      const data = await api.get<Aircraft[]>(`/airfields/${id}/aircraft`)
      setAircraft(data)
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

  function openEditForm(ac: Aircraft) {
    setEditingFlarmId(ac.flarm_id)
    setForm({
      registration: ac.registration,
      competition_sign: ac.competition_sign ?? '',
      flarm_id: ac.flarm_id,
      aircraft_model: ac.aircraft_model ?? '',
      aircraft_type: ac.aircraft_type || 'glider',
    })
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
    setSaving(true)
    setError('')
    try {
      if (editingFlarmId) {
        await api.put(`/airfields/${airfieldId}/aircraft/${editingFlarmId}`, {
          registration: form.registration,
          competition_sign: form.competition_sign || null,
          aircraft_model: form.aircraft_model || null,
          aircraft_type: form.aircraft_type || null,
        })
      } else {
        await api.post(`/airfields/${airfieldId}/aircraft`, form)
      }
      closeForm()
      await loadAircraft()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Speichern fehlgeschlagen')
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete(flarmId: string, registration: string) {
    if (!airfieldId) return
    if (!window.confirm(`${registration} wirklich entfernen?`)) return
    setError('')
    try {
      await api.delete(`/airfields/${airfieldId}/aircraft/${flarmId}`)
      await loadAircraft()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Loeschen fehlgeschlagen')
    }
  }

  async function handleCsvSelected(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    // Reset so the same file can be selected again after a fix
    e.target.value = ''
    if (!file || !airfieldId) return
    setImporting(true)
    setError('')
    setImportResult(null)
    try {
      const fd = new FormData()
      fd.append('file', file)
      const result = await api.upload<CsvImportResult>(`/airfields/${airfieldId}/aircraft/import-csv`, fd)
      setImportResult(result)
      await loadAircraft()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'CSV-Import fehlgeschlagen')
    } finally {
      setImporting(false)
    }
  }

  if (loading) return <div className="p-6 text-gray-400">Laden...</div>

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6 gap-3 flex-wrap">
        <h2 className="text-xl font-bold text-white">Flugzeuge</h2>
        <div className="flex items-center gap-3">
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv"
            onChange={handleCsvSelected}
            className="hidden"
          />
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={!airfieldId || importing}
            className="bg-tower-surface hover:bg-white/10 border border-tower-border text-white text-sm font-semibold rounded-lg px-4 py-2 transition-colors disabled:opacity-50"
          >
            {importing ? 'Importiere...' : 'CSV importieren'}
          </button>
          <button
            onClick={() => (showForm && !editingFlarmId ? closeForm() : openAddForm())}
            disabled={!airfieldId}
            className="bg-tower-qdr hover:bg-cyan-500 text-white text-sm font-semibold rounded-lg px-4 py-2 transition-colors disabled:opacity-50"
          >
            + Flugzeug hinzufuegen
          </button>
        </div>
      </div>

      <p className="text-xs text-gray-500 mb-4">
        CSV-Import: Spalten <span className="font-mono text-gray-400">flarm_id,registration,competition_sign,aircraft_model,aircraft_type</span>
        {' '}(kommagetrennt, Kopfzeile optional; bestehende FLARM-IDs werden aktualisiert)
      </p>

      {error && (
        <div className="bg-red-900/50 border border-red-500 text-red-200 rounded-lg p-3 mb-4 text-sm">{error}</div>
      )}

      {importResult && (
        <div className={`border rounded-lg p-3 mb-4 text-sm ${importResult.errors.length > 0 ? 'bg-yellow-900/40 border-yellow-500 text-yellow-100' : 'bg-green-900/40 border-green-500 text-green-100'}`}>
          <div className="flex items-center justify-between gap-3">
            <span>
              CSV-Import: {importResult.imported} importiert, {importResult.skipped} uebersprungen
            </span>
            <button onClick={() => setImportResult(null)} className="text-current opacity-70 hover:opacity-100 px-2">&times;</button>
          </div>
          {importResult.errors.length > 0 && (
            <ul className="list-disc list-inside mt-2 text-xs space-y-0.5">
              {importResult.errors.map((err, i) => <li key={i}>{err}</li>)}
            </ul>
          )}
        </div>
      )}

      {showForm && (
        <div className="bg-tower-surface border border-tower-border rounded-xl p-6 mb-6">
          <h3 className="text-sm font-semibold text-gray-300 mb-4">
            {editingFlarmId ? `Flugzeug bearbeiten (${editingFlarmId})` : 'Neues Flugzeug'}
          </h3>
          <form onSubmit={handleSubmit} className="grid grid-cols-2 md:grid-cols-3 gap-4">
            <Input label="Kennzeichen" value={form.registration} onChange={(v) => setForm({ ...form, registration: v })} placeholder="D-KMSF" required />
            <Input label="Wettbewerbskz." value={form.competition_sign} onChange={(v) => setForm({ ...form, competition_sign: v })} placeholder="SF" />
            <Input label="FLARM-ID" value={form.flarm_id} onChange={(v) => setForm({ ...form, flarm_id: v })} placeholder="DD0239" readOnly={!!editingFlarmId} required />
            <Input label="Flugzeugtyp" value={form.aircraft_model} onChange={(v) => setForm({ ...form, aircraft_model: v })} placeholder="Ventus ct" />
            <div>
              <label className="block text-sm text-gray-400 mb-1">Kategorie</label>
              <select
                value={form.aircraft_type}
                onChange={(e) => setForm({ ...form, aircraft_type: e.target.value })}
                className={inputClass}
              >
                <option value="glider">Segelflugzeug</option>
                <option value="tow_plane">Schleppflugzeug</option>
                <option value="motor_glider">Motorsegler</option>
                <option value="ultralight">Ultraleicht</option>
              </select>
            </div>
            <div className="flex items-end gap-3">
              <button type="submit" disabled={saving} className="bg-green-600 hover:bg-green-500 text-white text-sm font-semibold rounded-lg px-6 py-2.5 transition-colors disabled:opacity-50">
                {saving ? 'Speichern...' : editingFlarmId ? 'Speichern' : 'Hinzufuegen'}
              </button>
              <button type="button" onClick={closeForm} className="text-gray-400 hover:text-white text-sm px-3 py-2.5">
                Abbrechen
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Aircraft table */}
      <div className="bg-tower-surface border border-tower-border rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-tower-border text-left text-gray-500 uppercase text-xs">
              <th className="px-4 py-3">Kennzeichen</th>
              <th className="px-4 py-3">WB-Kz.</th>
              <th className="px-4 py-3">FLARM-ID</th>
              <th className="px-4 py-3">Typ</th>
              <th className="px-4 py-3">Kategorie</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3"></th>
            </tr>
          </thead>
          <tbody>
            {aircraft.length === 0 ? (
              <tr><td colSpan={7} className="px-4 py-8 text-center text-gray-500">Keine Flugzeuge eingetragen</td></tr>
            ) : (
              aircraft.map((ac) => (
                <tr key={ac.id} className="border-b border-tower-border/50 hover:bg-white/5">
                  <td className="px-4 py-3 text-white font-medium">{ac.registration}</td>
                  <td className="px-4 py-3 text-gray-300">{ac.competition_sign || '-'}</td>
                  <td className="px-4 py-3 text-gray-400 font-mono text-xs">{ac.flarm_id || '-'}</td>
                  <td className="px-4 py-3 text-gray-300">{ac.aircraft_model || '-'}</td>
                  <td className="px-4 py-3 text-gray-400">{ac.aircraft_type || '-'}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-xs ${ac.is_active ? 'bg-green-900/50 text-green-300' : 'bg-gray-800 text-gray-500'}`}>
                      {ac.is_active ? 'Aktiv' : 'Inaktiv'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right whitespace-nowrap">
                    <button
                      onClick={() => openEditForm(ac)}
                      className="text-gray-400 hover:text-tower-qdr text-xs px-2 py-1 mr-2"
                    >
                      Bearbeiten
                    </button>
                    <button
                      onClick={() => handleDelete(ac.flarm_id, ac.registration)}
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
