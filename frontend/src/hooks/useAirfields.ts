/**
 * Loads the tenant's airfields and manages the selected airfield id.
 * Same mechanism as AirfieldConfigPage/AircraftManagePage: list from
 * GET /airfields, first entry selected by default.
 */
import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'

export interface AirfieldSummary {
  id: string
  name: string
  slug: string
}

export function useAirfields() {
  const [airfields, setAirfields] = useState<AirfieldSummary[]>([])
  const [airfieldId, setAirfieldId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    api.get<AirfieldSummary[]>('/airfields')
      .then((data) => {
        if (cancelled) return
        setAirfields(data)
        setAirfieldId(data[0]?.id ?? null)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setError(e instanceof ApiError ? e.message : 'Flugplaetze laden fehlgeschlagen')
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  const airfield = airfields.find((a) => a.id === airfieldId) ?? null
  return { airfields, airfield, airfieldId, setAirfieldId, loading, error }
}
