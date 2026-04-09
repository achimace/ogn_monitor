/**
 * Airfield configuration page - create, edit, delete airfields.
 * Backend schema requires slug + operational parameters.
 */
import { useState, useEffect, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import HomePolygonEditor, { type GeoJsonPolygon } from '../components/HomePolygonEditor'

interface Airfield {
  id: string
  tenant_id: string
  name: string
  slug: string
  icao_code: string | null
  latitude: number
  longitude: number
  elevation_m: number
  home_radius_m: number
  ogn_filter_radius_km: number
  alarm_timeout_s: number
  signal_loss_timeout_s: number
  takeoff_speed_kmh: number
  takeoff_alt_offset_m: number
  tow_plane_flarm_ids: string[]
  winch_vs_threshold_ms: number
  is_active: boolean
  home_polygon: GeoJsonPolygon | null
  landed_visible_minutes: number
  monitor_strip_fields: string[]
}

const ALL_STRIP_FIELDS: { id: string; label: string }[] = [
  { id: 'competition_sign', label: 'Wettbewerbszeichen' },
  { id: 'aircraft_model',   label: 'Flugzeug-Typ' },
  { id: 'takeoff_time',     label: 'Startzeit' },
  { id: 'landing_time',     label: 'Landezeit' },
  { id: 'duration',         label: 'Flugdauer' },
  { id: 'launch_type',      label: 'Startart' },
  { id: 'qdr',              label: 'QDR / Peilung' },
  { id: 'distance',         label: 'Distanz' },
  { id: 'altitude',         label: 'Hoehe MSL' },
  { id: 'agl',              label: 'Hoehe AGL' },
  { id: 'speed',            label: 'Geschwindigkeit' },
  { id: 'vs',               label: 'Steigen/Sinken' },
  { id: 'track',            label: 'Kurs' },
]

type AirfieldForm = Omit<Airfield, 'id' | 'tenant_id'> & { id?: string }

const EMPTY_FORM: AirfieldForm = {
  name: '',
  slug: '',
  icao_code: '',
  latitude: 0,
  longitude: 0,
  elevation_m: 0,
  home_radius_m: 800,
  ogn_filter_radius_km: 500,
  alarm_timeout_s: 600,
  signal_loss_timeout_s: 300,
  takeoff_speed_kmh: 40,
  takeoff_alt_offset_m: 50,
  tow_plane_flarm_ids: [],
  winch_vs_threshold_ms: 8.0,
  is_active: true,
  home_polygon: null,
  landed_visible_minutes: 1440,
  monitor_strip_fields: ALL_STRIP_FIELDS.map(f => f.id),
}

export default function AirfieldConfigPage() {
  const [airfields, setAirfields] = useState<Airfield[]>([])
  const [form, setForm] = useState<AirfieldForm>({ ...EMPTY_FORM })
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [towPlaneInput, setTowPlaneInput] = useState('')

  useEffect(() => { loadAirfields() }, [])

  async function loadAirfields() {
    try {
      const data = await api.get<Airfield[]>('/airfields')
      setAirfields(data)
      if (data.length > 0 && !form.id) {
        selectAirfield(data[0]!)
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Laden fehlgeschlagen')
    } finally {
      setLoading(false)
    }
  }

  function selectAirfield(af: Airfield) {
    setForm({
      id: af.id,
      name: af.name,
      slug: af.slug,
      icao_code: af.icao_code || '',
      latitude: af.latitude,
      longitude: af.longitude,
      elevation_m: af.elevation_m,
      home_radius_m: af.home_radius_m,
      ogn_filter_radius_km: af.ogn_filter_radius_km,
      alarm_timeout_s: af.alarm_timeout_s,
      signal_loss_timeout_s: af.signal_loss_timeout_s,
      takeoff_speed_kmh: af.takeoff_speed_kmh,
      takeoff_alt_offset_m: af.takeoff_alt_offset_m,
      tow_plane_flarm_ids: af.tow_plane_flarm_ids || [],
      winch_vs_threshold_ms: af.winch_vs_threshold_ms,
      is_active: af.is_active,
      home_polygon: af.home_polygon ?? null,
      landed_visible_minutes: af.landed_visible_minutes ?? 1440,
      monitor_strip_fields: af.monitor_strip_fields ?? ALL_STRIP_FIELDS.map(f => f.id),
    })
    setTowPlaneInput((af.tow_plane_flarm_ids || []).join(', '))
    setError('')
    setSuccess('')
  }

  function startNew() {
    setForm({ ...EMPTY_FORM })
    setTowPlaneInput('')
    setError('')
    setSuccess('')
  }

  function buildPayload() {
    const ids = towPlaneInput
      .split(/[,;\s]+/)
      .map(s => s.trim().toUpperCase())
      .filter(s => s.length > 0)
    return {
      name: form.name,
      slug: form.slug,
      icao_code: form.icao_code || null,
      latitude: form.latitude,
      longitude: form.longitude,
      elevation_m: form.elevation_m,
      home_radius_m: form.home_radius_m,
      ogn_filter_radius_km: form.ogn_filter_radius_km,
      alarm_timeout_s: form.alarm_timeout_s,
      signal_loss_timeout_s: form.signal_loss_timeout_s,
      takeoff_speed_kmh: form.takeoff_speed_kmh,
      takeoff_alt_offset_m: form.takeoff_alt_offset_m,
      tow_plane_flarm_ids: ids,
      winch_vs_threshold_ms: form.winch_vs_threshold_ms,
      is_active: form.is_active,
      home_polygon: form.home_polygon,
      landed_visible_minutes: form.landed_visible_minutes,
      monitor_strip_fields: form.monitor_strip_fields,
    }
  }

  async function handleSave(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError('')
    setSuccess('')

    try {
      const payload = buildPayload()
      if (form.id) {
        await api.put(`/airfields/${form.id}`, payload)
        setSuccess('Flugplatz gespeichert')
      } else {
        await api.post('/airfields', payload)
        setSuccess('Flugplatz erstellt')
      }
      await loadAirfields()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Speichern fehlgeschlagen')
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete() {
    if (!form.id) return
    if (!confirm('Flugplatz und alle zugehoerigen Daten wirklich loeschen?')) return
    setError('')
    setSuccess('')

    try {
      await api.delete(`/airfields/${form.id}`)
      setSuccess('Flugplatz geloescht')
      setForm({ ...EMPTY_FORM })
      setTowPlaneInput('')
      await loadAirfields()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Loeschen fehlgeschlagen')
    }
  }

  function setField<K extends keyof AirfieldForm>(key: K, value: AirfieldForm[K]) {
    setForm(f => ({ ...f, [key]: value }))
  }

  // Auto-generate slug from name
  function handleNameChange(name: string) {
    const isNew = !form.id
    setForm(f => ({
      ...f,
      name,
      ...(isNew ? { slug: name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') } : {}),
    }))
  }

  if (loading) return <div className="p-6 text-gray-400">Laden...</div>

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-bold text-white">Flugplatz konfigurieren</h2>
        <button
          onClick={startNew}
          className="bg-tower-qdr hover:bg-cyan-500 text-white text-sm font-semibold rounded-lg px-4 py-2 transition-colors"
        >
          + Neuer Flugplatz
        </button>
      </div>

      {/* Airfield selector (if multiple) */}
      {airfields.length > 1 && (
        <div className="flex flex-wrap gap-2 mb-4">
          {airfields.map(af => (
            <div
              key={af.id}
              className={`flex items-stretch rounded-lg overflow-hidden border transition-colors ${
                form.id === af.id
                  ? 'border-tower-qdr'
                  : 'border-tower-border'
              }`}
            >
              <button
                onClick={() => selectAirfield(af)}
                className={`px-3 py-1.5 text-sm font-medium transition-colors ${
                  form.id === af.id
                    ? 'bg-tower-qdr text-white'
                    : 'bg-tower-surface text-gray-400 hover:text-white'
                }`}
              >
                {af.name}
              </button>
              <a
                href={`/monitor/${af.slug}`}
                target="_blank"
                rel="noopener"
                title={`Tower-Monitor ${af.name} oeffnen`}
                className="px-2 flex items-center text-xs font-medium bg-tower-surface
                  text-gray-500 hover:text-tower-qdr border-l border-tower-border
                  transition-colors"
              >
                Monitor ↗
              </a>
            </div>
          ))}
        </div>
      )}

      {/* Tower-Monitor link for the currently selected airfield */}
      {form.id && form.slug && (
        <div className="mb-4">
          <a
            href={`/monitor/${form.slug}`}
            target="_blank"
            rel="noopener"
            className="inline-flex items-center gap-2 bg-tower-qdr/10 hover:bg-tower-qdr/20
              border border-tower-qdr/40 text-tower-qdr text-sm font-semibold
              rounded-lg px-4 py-2 transition-colors"
          >
            Tower-Monitor "{form.name}" oeffnen ↗
          </a>
        </div>
      )}

      {error && (
        <div className="bg-red-900/50 border border-red-500 text-red-200 rounded-lg p-3 mb-4 text-sm">{error}</div>
      )}
      {success && (
        <div className="bg-green-900/50 border border-green-500 text-green-200 rounded-lg p-3 mb-4 text-sm">{success}</div>
      )}

      <div className="bg-tower-surface border border-tower-border rounded-xl p-6">
        <form onSubmit={handleSave} className="space-y-6">
          {/* Basic info */}
          <fieldset>
            <legend className="text-sm font-semibold text-gray-300 uppercase tracking-wider mb-3">Grunddaten</legend>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <FormField label="Name *" value={form.name} onChange={handleNameChange} placeholder="Segelflugplatz Ohlstadt" required />
              <FormField label="Slug *" value={form.slug} onChange={v => setField('slug', v)} placeholder="ohlstadt" required disabled={!!form.id} />
              <FormField label="ICAO Code" value={form.icao_code || ''} onChange={v => setField('icao_code', v)} placeholder="EDMO" />
            </div>
          </fieldset>

          {/* Position */}
          <fieldset>
            <legend className="text-sm font-semibold text-gray-300 uppercase tracking-wider mb-3">Position</legend>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <FormField label="Breitengrad *" type="number" step="0.000001" value={String(form.latitude || '')} onChange={v => setField('latitude', parseFloat(v) || 0)} placeholder="47.3895" required />
              <FormField label="Laengengrad *" type="number" step="0.000001" value={String(form.longitude || '')} onChange={v => setField('longitude', parseFloat(v) || 0)} placeholder="11.1886" required />
              <FormField label="Hoehe (m MSL) *" type="number" value={String(form.elevation_m || '')} onChange={v => setField('elevation_m', parseInt(v) || 0)} placeholder="660" required />
            </div>
          </fieldset>

          {/* Monitoring parameters */}
          <fieldset>
            <legend className="text-sm font-semibold text-gray-300 uppercase tracking-wider mb-3">Monitoring-Parameter</legend>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <FormField label="Platzradius (m)" type="number" value={String(form.home_radius_m)} onChange={v => setField('home_radius_m', parseInt(v) || 800)} hint="200-5000" />
              <FormField label="OGN-Filter Radius (km)" type="number" value={String(form.ogn_filter_radius_km)} onChange={v => setField('ogn_filter_radius_km', parseInt(v) || 500)} hint="50-1000" />
              <FormField label="Alarm-Timeout (s)" type="number" value={String(form.alarm_timeout_s)} onChange={v => setField('alarm_timeout_s', parseInt(v) || 600)} hint="60-3600" />
              <FormField label="Signalverlust-Timeout (s)" type="number" value={String(form.signal_loss_timeout_s)} onChange={v => setField('signal_loss_timeout_s', parseInt(v) || 300)} hint="60-1800" />
              <FormField label="Start-Geschwindigkeit (km/h)" type="number" value={String(form.takeoff_speed_kmh)} onChange={v => setField('takeoff_speed_kmh', parseInt(v) || 40)} hint="20-100" />
              <FormField label="Start-Hoehe Offset (m)" type="number" value={String(form.takeoff_alt_offset_m)} onChange={v => setField('takeoff_alt_offset_m', parseInt(v) || 50)} hint="10-200" />
              <FormField label="Winden-VS Schwelle (m/s)" type="number" step="0.1" value={String(form.winch_vs_threshold_ms)} onChange={v => setField('winch_vs_threshold_ms', parseFloat(v) || 8.0)} hint="3.0-15.0" />
            </div>
          </fieldset>

          {/* Tow planes */}
          <fieldset>
            <legend className="text-sm font-semibold text-gray-300 uppercase tracking-wider mb-3">Schleppflugzeuge</legend>
            <div>
              <label className="block text-sm text-gray-400 mb-1">FLARM-IDs (kommagetrennt)</label>
              <input
                type="text"
                value={towPlaneInput}
                onChange={e => setTowPlaneInput(e.target.value)}
                placeholder="DD1234, DD5678"
                className="w-full bg-tower-bg border border-tower-border rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-tower-qdr font-mono"
              />
              <p className="text-xs text-gray-500 mt-1">FLARM-IDs der Schleppflugzeuge fuer automatische F-Schlepp-Erkennung</p>
            </div>
          </fieldset>

          {/* Monitor display config */}
          <fieldset>
            <legend className="text-sm font-semibold text-gray-300 uppercase tracking-wider mb-3">
              Monitor-Anzeige
            </legend>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
              <FormField
                label="Gelandet sichtbar (Min.)"
                type="number"
                value={String(form.landed_visible_minutes)}
                onChange={v => setField('landed_visible_minutes', parseInt(v) || 1440)}
                hint="1 = 1 Minute, 1440 = 24 Stunden"
              />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-2">
                Felder auf dem Flugstreifen
              </label>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
                {ALL_STRIP_FIELDS.map(f => {
                  const checked = form.monitor_strip_fields.includes(f.id)
                  return (
                    <label key={f.id} className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer hover:text-white">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(e) => {
                          const next = e.target.checked
                            ? [...form.monitor_strip_fields, f.id]
                            : form.monitor_strip_fields.filter(x => x !== f.id)
                          setField('monitor_strip_fields', next)
                        }}
                        className="w-4 h-4 accent-tower-qdr"
                      />
                      {f.label}
                    </label>
                  )
                })}
              </div>
              <p className="text-xs text-gray-500 mt-2">
                Kennzeichen und Status werden immer angezeigt.
              </p>
            </div>
          </fieldset>

          {/* Home area polygon */}
          {form.latitude !== 0 && form.longitude !== 0 && (
            <fieldset>
              <legend className="text-sm font-semibold text-gray-300 uppercase tracking-wider mb-3">
                Heimatbereich (Polygon)
              </legend>
              <HomePolygonEditor
                latitude={form.latitude}
                longitude={form.longitude}
                radiusM={form.home_radius_m}
                polygon={form.home_polygon}
                onChange={(p) => setField('home_polygon', p)}
              />
            </fieldset>
          )}

          {/* Active toggle */}
          <div className="flex items-center gap-3">
            <input
              type="checkbox"
              checked={form.is_active}
              onChange={e => setField('is_active', e.target.checked)}
              className="w-4 h-4 accent-tower-qdr"
            />
            <label className="text-sm text-gray-300">Flugplatz aktiv (empfaengt OGN-Daten)</label>
          </div>

          {/* Actions */}
          <div className="flex items-center gap-3 pt-2">
            <button
              type="submit"
              disabled={saving}
              className="bg-tower-qdr hover:bg-cyan-500 text-white font-semibold rounded-lg px-6 py-2.5 transition-colors disabled:opacity-50"
            >
              {saving ? 'Wird gespeichert...' : form.id ? 'Speichern' : 'Erstellen'}
            </button>
            {form.id && (
              <button
                type="button"
                onClick={handleDelete}
                className="text-gray-500 hover:text-red-400 text-sm transition-colors"
              >
                Flugplatz loeschen
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  )
}

function FormField({ label, value, onChange, type = 'text', placeholder, step, required, disabled, hint }: {
  label: string; value: string; onChange: (v: string) => void;
  type?: string; placeholder?: string; step?: string; required?: boolean; disabled?: boolean; hint?: string
}) {
  return (
    <div>
      <label className="block text-sm text-gray-400 mb-1">{label}</label>
      <input
        type={type}
        step={step}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        required={required}
        disabled={disabled}
        className="w-full bg-tower-bg border border-tower-border rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-tower-qdr disabled:opacity-50 disabled:cursor-not-allowed"
      />
      {hint && <p className="text-xs text-gray-600 mt-0.5">{hint}</p>}
    </div>
  )
}
