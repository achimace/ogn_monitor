/**
 * Airfield configuration page.
 */
import { useState, useEffect, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'

interface Airfield {
  id: number
  name: string
  slug: string
  icao_code: string
  latitude: number
  longitude: number
  elevation_m: number
  runway_direction_deg: number
  is_active: boolean
}

export default function AirfieldConfigPage() {
  const [_airfields, setAirfields] = useState<Airfield[]>([])
  const [editing, setEditing] = useState<Airfield | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  useEffect(() => {
    loadAirfields()
  }, [])

  async function loadAirfields() {
    try {
      const data = await api.get<Airfield[]>('/airfields/')
      setAirfields(data)
      if (data.length > 0 && !editing) {
        setEditing(data[0] ?? null)
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Laden fehlgeschlagen')
    } finally {
      setLoading(false)
    }
  }

  async function handleSave(e: FormEvent) {
    e.preventDefault()
    if (!editing) return
    setSaving(true)
    setError('')
    setSuccess('')

    try {
      if (editing.id) {
        await api.put(`/airfields/${editing.id}`, editing)
        setSuccess('Flugplatz gespeichert')
      } else {
        await api.post('/airfields/', editing)
        setSuccess('Flugplatz erstellt')
      }
      await loadAirfields()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Speichern fehlgeschlagen')
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <div className="p-6 text-gray-400">Laden...</div>

  return (
    <div className="p-6">
      <h2 className="text-xl font-bold text-white mb-6">Flugplatz konfigurieren</h2>

      {error && (
        <div className="bg-red-900/50 border border-red-500 text-red-200 rounded-lg p-3 mb-4 text-sm">{error}</div>
      )}
      {success && (
        <div className="bg-green-900/50 border border-green-500 text-green-200 rounded-lg p-3 mb-4 text-sm">{success}</div>
      )}

      <div className="bg-tower-surface border border-tower-border rounded-xl p-6">
        <form onSubmit={handleSave} className="space-y-4 max-w-xl">
          <FormField label="Name" value={editing?.name || ''} onChange={(v) => setEditing((e) => e ? { ...e, name: v } : null)} />
          <FormField label="ICAO Code" value={editing?.icao_code || ''} onChange={(v) => setEditing((e) => e ? { ...e, icao_code: v } : null)} placeholder="EDMO" />

          <div className="grid grid-cols-2 gap-4">
            <FormField label="Breitengrad" type="number" step="0.000001" value={String(editing?.latitude || '')} onChange={(v) => setEditing((e) => e ? { ...e, latitude: parseFloat(v) || 0 } : null)} placeholder="47.3895" />
            <FormField label="Laengengrad" type="number" step="0.000001" value={String(editing?.longitude || '')} onChange={(v) => setEditing((e) => e ? { ...e, longitude: parseFloat(v) || 0 } : null)} placeholder="11.1886" />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <FormField label="Hoehe (m MSL)" type="number" value={String(editing?.elevation_m || '')} onChange={(v) => setEditing((e) => e ? { ...e, elevation_m: parseInt(v) || 0 } : null)} placeholder="660" />
            <FormField label="Pistenrichtung (Grad)" type="number" value={String(editing?.runway_direction_deg || '')} onChange={(v) => setEditing((e) => e ? { ...e, runway_direction_deg: parseInt(v) || 0 } : null)} placeholder="260" />
          </div>

          <div className="flex items-center gap-3 mt-2">
            <input
              type="checkbox"
              checked={editing?.is_active ?? true}
              onChange={(e) => setEditing((prev) => prev ? { ...prev, is_active: e.target.checked } : null)}
              className="w-4 h-4 accent-tower-qdr"
            />
            <label className="text-sm text-gray-300">Flugplatz aktiv (empfaengt OGN-Daten)</label>
          </div>

          <button
            type="submit"
            disabled={saving}
            className="bg-tower-qdr hover:bg-cyan-500 text-white font-semibold rounded-lg px-6 py-2.5 transition-colors disabled:opacity-50"
          >
            {saving ? 'Wird gespeichert...' : 'Speichern'}
          </button>
        </form>
      </div>
    </div>
  )
}

function FormField({ label, value, onChange, type = 'text', placeholder, step }: {
  label: string; value: string; onChange: (v: string) => void;
  type?: string; placeholder?: string; step?: string
}) {
  return (
    <div>
      <label className="block text-sm text-gray-400 mb-1">{label}</label>
      <input
        type={type}
        step={step}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full bg-tower-bg border border-tower-border rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-tower-qdr"
      />
    </div>
  )
}
