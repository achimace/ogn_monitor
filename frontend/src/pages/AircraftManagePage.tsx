/**
 * Aircraft management page - add/edit tenant aircraft.
 */
import { useState, useEffect, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'

interface Aircraft {
  id: number
  registration: string
  competition_sign: string
  flarm_id: string
  aircraft_model: string
  aircraft_type: string
  is_active: boolean
}

export default function AircraftManagePage() {
  const [aircraft, setAircraft] = useState<Aircraft[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({
    registration: '', competition_sign: '', flarm_id: '',
    aircraft_model: '', aircraft_type: 'glider', is_active: true,
  })
  const [saving, setSaving] = useState(false)

  useEffect(() => { loadAircraft() }, [])

  async function loadAircraft() {
    try {
      const data = await api.get<Aircraft[]>('/aircraft/')
      setAircraft(data)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Laden fehlgeschlagen')
    } finally {
      setLoading(false)
    }
  }

  async function handleAdd(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError('')
    try {
      await api.post('/aircraft/', form)
      setShowForm(false)
      setForm({ registration: '', competition_sign: '', flarm_id: '', aircraft_model: '', aircraft_type: 'glider', is_active: true })
      await loadAircraft()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Speichern fehlgeschlagen')
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete(id: number) {
    try {
      await api.delete(`/aircraft/${id}`)
      await loadAircraft()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Loeschen fehlgeschlagen')
    }
  }

  if (loading) return <div className="p-6 text-gray-400">Laden...</div>

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold text-white">Flugzeuge</h2>
        <button
          onClick={() => setShowForm(!showForm)}
          className="bg-tower-qdr hover:bg-cyan-500 text-white text-sm font-semibold rounded-lg px-4 py-2 transition-colors"
        >
          + Flugzeug hinzufuegen
        </button>
      </div>

      {error && (
        <div className="bg-red-900/50 border border-red-500 text-red-200 rounded-lg p-3 mb-4 text-sm">{error}</div>
      )}

      {showForm && (
        <div className="bg-tower-surface border border-tower-border rounded-xl p-6 mb-6">
          <form onSubmit={handleAdd} className="grid grid-cols-2 md:grid-cols-3 gap-4">
            <Input label="Kennzeichen" value={form.registration} onChange={(v) => setForm({ ...form, registration: v })} placeholder="D-KMSF" required />
            <Input label="Wettbewerbskz." value={form.competition_sign} onChange={(v) => setForm({ ...form, competition_sign: v })} placeholder="SF" />
            <Input label="FLARM-ID" value={form.flarm_id} onChange={(v) => setForm({ ...form, flarm_id: v })} placeholder="000239" />
            <Input label="Flugzeugtyp" value={form.aircraft_model} onChange={(v) => setForm({ ...form, aircraft_model: v })} placeholder="Ventus ct" />
            <div>
              <label className="block text-sm text-gray-400 mb-1">Kategorie</label>
              <select
                value={form.aircraft_type}
                onChange={(e) => setForm({ ...form, aircraft_type: e.target.value })}
                className="w-full bg-tower-bg border border-tower-border rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-tower-qdr"
              >
                <option value="glider">Segelflugzeug</option>
                <option value="tow_plane">Schleppflugzeug</option>
                <option value="motor_glider">Motorsegler</option>
                <option value="ultralight">Ultraleicht</option>
              </select>
            </div>
            <div className="flex items-end">
              <button type="submit" disabled={saving} className="bg-green-600 hover:bg-green-500 text-white text-sm font-semibold rounded-lg px-6 py-2.5 transition-colors disabled:opacity-50">
                {saving ? 'Speichern...' : 'Hinzufuegen'}
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
                  <td className="px-4 py-3 text-gray-400">{ac.aircraft_type}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-xs ${ac.is_active ? 'bg-green-900/50 text-green-300' : 'bg-gray-800 text-gray-500'}`}>
                      {ac.is_active ? 'Aktiv' : 'Inaktiv'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => handleDelete(ac.id)}
                      className="text-gray-500 hover:text-red-400 text-xs"
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

function Input({ label, value, onChange, placeholder, required }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string; required?: boolean
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
        className="w-full bg-tower-bg border border-tower-border rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-tower-qdr"
      />
    </div>
  )
}
