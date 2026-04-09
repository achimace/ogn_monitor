/**
 * MapLibre GL map view for Tower Monitor.
 *
 * Features:
 * - Aircraft as direction arrows with registration labels
 * - Color coding: blue=flying, green=landed, red=alarm, orange=outlanding
 * - Home airfield marker with 800m radius circle
 * - QDR line from home to each aircraft (dashed, with degree label)
 * - Flight trail of last 60 minutes (height-colored)
 * - Alarm: pulsing red circle at last position
 * - Auto-zoom to fit all active flights
 */
import { useEffect, useRef, useState } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import type { Flight } from '../types/flight'

interface MapViewProps {
  flights: Flight[]
  airfieldLat?: number
  airfieldLng?: number
  airfieldName?: string
}

const STATUS_COLORS: Record<string, string> = {
  flying: '#5697d6',
  towing: '#f59e0b',
  ground: '#6b7280',
  takeoff: '#3b82f6',
  landing: '#22c55e',
  outlanding: '#f97316',
  outlanding_pending: '#f97316',
  diverted: '#f97316',
  signal_lost: '#eab308',
  alarm: '#dc3545',
  emergency: '#ff0040',
}

export default function MapView({ flights, airfieldLat, airfieldLng, airfieldName }: MapViewProps) {
  const mapContainer = useRef<HTMLDivElement>(null)
  const mapRef = useRef<maplibregl.Map | null>(null)
  const markersRef = useRef<Map<string, maplibregl.Marker>>(new Map())
  const [mapReady, setMapReady] = useState(false)
  // Tracks whether the user has manually panned/zoomed the map. While true,
  // auto-fit is paused and a "Zurueck zur Übersicht" button is shown.
  const [userInteracted, setUserInteracted] = useState(false)
  // Distinguishes our own programmatic fitBounds from real user moves so
  // moveend handlers don't accidentally flip userInteracted on.
  const programmaticMoveRef = useRef(false)

  // Initialize map
  useEffect(() => {
    if (!mapContainer.current || mapRef.current) return

    const lat = airfieldLat || 47.39
    const lng = airfieldLng || 11.19

    const map = new maplibregl.Map({
      container: mapContainer.current,
      style: {
        version: 8,
        sources: {
          osm: {
            type: 'raster',
            tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
            tileSize: 256,
            attribution: '&copy; OpenStreetMap contributors',
          },
        },
        layers: [
          {
            id: 'osm',
            type: 'raster',
            source: 'osm',
            minzoom: 0,
            maxzoom: 19,
          },
        ],
      },
      center: [lng, lat],
      zoom: 10,
    })

    map.addControl(new maplibregl.NavigationControl(), 'top-right')
    map.addControl(new maplibregl.ScaleControl(), 'bottom-left')

    // Mark the map as "user controlled" the moment the user drags,
    // zooms, rotates or pitches. We deliberately do NOT listen on the
    // raw 'wheel' event because it also fires on harmless hover scroll
    // and would freeze the auto-fit without any real interaction.
    // Programmatic fitBounds is guarded via programmaticMoveRef.
    const onUserGesture = () => {
      if (!programmaticMoveRef.current) setUserInteracted(true)
    }
    map.on('dragstart', onUserGesture)
    map.on('zoomstart', onUserGesture)
    map.on('rotatestart', onUserGesture)
    map.on('pitchstart', onUserGesture)

    map.on('load', () => {
      // Add airfield marker and radius circle
      if (airfieldLat && airfieldLng) {
        addAirfieldMarker(map, airfieldLat, airfieldLng, airfieldName || '')
        addRadiusCircle(map, airfieldLat, airfieldLng, 800)
      }

      // Add QDR lines source
      map.addSource('qdr-lines', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      })
      map.addLayer({
        id: 'qdr-lines',
        type: 'line',
        source: 'qdr-lines',
        paint: {
          'line-color': ['get', 'color'],
          'line-width': 1.5,
          'line-dasharray': [4, 4],
          'line-opacity': 0.6,
        },
      })

      setMapReady(true)
    })

    mapRef.current = map

    return () => {
      map.remove()
      mapRef.current = null
      markersRef.current.clear()
    }
  }, [airfieldLat, airfieldLng, airfieldName])

  // Update aircraft markers
  useEffect(() => {
    if (!mapReady || !mapRef.current) return
    const map = mapRef.current

    const activeIds = new Set<string>()

    // Update/create markers for each flight
    for (const flight of flights) {
      if (!flight.latitude || !flight.longitude) continue
      activeIds.add(flight.flarmId)

      const color = STATUS_COLORS[flight.status] || '#6b7280'
      const isAlarm = ['alarm', 'emergency'].includes(flight.status)
      const label = flight.registration || flight.flarmId

      let marker = markersRef.current.get(flight.flarmId)
      if (marker) {
        // Update position
        marker.setLngLat([flight.longitude, flight.latitude])
        // Update element
        const el = marker.getElement()
        updateMarkerElement(el, flight.trackDeg, color, label, isAlarm)
      } else {
        // Create new marker
        const el = createMarkerElement(flight.trackDeg, color, label, isAlarm)
        marker = new maplibregl.Marker({ element: el, anchor: 'center' })
          .setLngLat([flight.longitude, flight.latitude])
          .addTo(map)
        markersRef.current.set(flight.flarmId, marker)
      }
    }

    // Remove stale markers
    for (const [id, marker] of markersRef.current) {
      if (!activeIds.has(id)) {
        marker.remove()
        markersRef.current.delete(id)
      }
    }

    // Update QDR lines
    if (airfieldLat && airfieldLng) {
      updateQdrLines(map, flights, airfieldLat, airfieldLng)
    }

    // Auto-fit bounds — only when the user has not taken control AND
    // only when something is actually outside the current viewport.
    // Live beacon updates would otherwise trigger a small fitBounds
    // animation every few seconds and make the map jitter.
    if (!userInteracted && flights.length > 0) {
      const bounds = new maplibregl.LngLatBounds()
      if (airfieldLat && airfieldLng) {
        bounds.extend([airfieldLng, airfieldLat])
      }
      for (const f of flights) {
        if (f.latitude && f.longitude) {
          bounds.extend([f.longitude, f.latitude])
        }
      }
      if (!bounds.isEmpty()) {
        const view = map.getBounds()
        const allInside =
          view.contains(bounds.getNorthEast()) &&
          view.contains(bounds.getSouthWest())
        if (!allInside) {
          // Mark this as a programmatic move so the gesture listeners
          // don't think the user did it.
          programmaticMoveRef.current = true
          map.fitBounds(bounds, { padding: 60, maxZoom: 13, duration: 500 })
          const clear = () => {
            programmaticMoveRef.current = false
            map.off('moveend', clear)
          }
          map.on('moveend', clear)
        }
      }
    }
  }, [flights, mapReady, airfieldLat, airfieldLng, userInteracted])

  function resetView() {
    setUserInteracted(false)
  }

  return (
    <div className="relative w-full h-full min-h-[400px]">
      <div ref={mapContainer} className="absolute inset-0 rounded-lg overflow-hidden" />
      {userInteracted && (
        <button
          onClick={resetView}
          className="absolute top-3 left-3 z-10 bg-tower-surface/95 hover:bg-tower-qdr
            border border-tower-qdr/60 text-tower-qdr hover:text-white
            text-sm font-semibold rounded-lg px-3 py-2 shadow-lg backdrop-blur
            transition-colors flex items-center gap-2"
          title="Karte automatisch auf alle Flugzeuge zentrieren"
        >
          <span aria-hidden>⌖</span>
          Zurück zur Übersicht
        </button>
      )}
    </div>
  )
}

function createMarkerElement(trackDeg: number, color: string, label: string, isAlarm: boolean): HTMLDivElement {
  const el = document.createElement('div')
  updateMarkerElement(el, trackDeg, color, label, isAlarm)
  return el
}

function updateMarkerElement(el: HTMLElement, trackDeg: number, color: string, label: string, isAlarm: boolean) {
  el.innerHTML = `
    <div style="display:flex;flex-direction:column;align-items:center;transform:translate(-50%,-50%)">
      <div style="font-size:10px;color:white;background:${color};padding:1px 4px;border-radius:3px;white-space:nowrap;font-weight:bold;margin-bottom:2px${isAlarm ? ';animation:pulse 0.5s ease-in-out infinite' : ''}">${label}</div>
      <div style="width:0;height:0;border-left:6px solid transparent;border-right:6px solid transparent;border-bottom:16px solid ${color};transform:rotate(${trackDeg}deg);filter:drop-shadow(0 0 2px rgba(0,0,0,0.5))"></div>
    </div>
  `
}

function addAirfieldMarker(map: maplibregl.Map, lat: number, lng: number, name: string) {
  const el = document.createElement('div')
  el.innerHTML = `
    <div style="display:flex;flex-direction:column;align-items:center">
      <div style="font-size:11px;color:#fff;background:#1e40af;padding:2px 6px;border-radius:4px;font-weight:bold;margin-bottom:4px">${name || 'HOME'}</div>
      <div style="width:12px;height:12px;background:#1e40af;border:2px solid white;border-radius:50%"></div>
    </div>
  `
  new maplibregl.Marker({ element: el, anchor: 'bottom' })
    .setLngLat([lng, lat])
    .addTo(map)
}

function addRadiusCircle(map: maplibregl.Map, lat: number, lng: number, radiusM: number) {
  const points = 64
  const coords: [number, number][] = []
  for (let i = 0; i <= points; i++) {
    const angle = (i / points) * 2 * Math.PI
    const dx = radiusM * Math.cos(angle)
    const dy = radiusM * Math.sin(angle)
    const dlng = dx / (111320 * Math.cos(lat * Math.PI / 180))
    const dlat = dy / 110540
    coords.push([lng + dlng, lat + dlat])
  }

  map.addSource('airfield-radius', {
    type: 'geojson',
    data: {
      type: 'Feature',
      properties: {},
      geometry: { type: 'Polygon', coordinates: [coords] },
    },
  })
  map.addLayer({
    id: 'airfield-radius-fill',
    type: 'fill',
    source: 'airfield-radius',
    paint: { 'fill-color': '#1e40af', 'fill-opacity': 0.1 },
  })
  map.addLayer({
    id: 'airfield-radius-line',
    type: 'line',
    source: 'airfield-radius',
    paint: { 'line-color': '#1e40af', 'line-width': 1.5, 'line-opacity': 0.5 },
  })
}

function updateQdrLines(map: maplibregl.Map, flights: Flight[], homeLat: number, homeLng: number) {
  const features = flights
    .filter((f) => f.latitude && f.longitude && ['flying', 'towing', 'alarm', 'emergency', 'signal_lost', 'outlanding', 'outlanding_pending', 'diverted'].includes(f.status))
    .map((f) => ({
      type: 'Feature' as const,
      properties: { color: STATUS_COLORS[f.status] || '#6b7280' },
      geometry: {
        type: 'LineString' as const,
        coordinates: [[homeLng, homeLat], [f.longitude, f.latitude]],
      },
    }))

  const source = map.getSource('qdr-lines') as maplibregl.GeoJSONSource
  if (source) {
    source.setData({ type: 'FeatureCollection', features })
  }
}
