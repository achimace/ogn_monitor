/**
 * HomePolygonEditor - draw / edit the airfield "home area" polygon on a
 * MapLibre + OpenStreetMap map.
 *
 * Modes:
 *   view  - just show the polygon (if any) plus the circular fallback
 *   draw  - click to add vertices, double-click or "Schliessen" to finish
 *   edit  - drag existing vertices, click between two to insert,
 *           click a vertex with shift to remove
 *
 * Polygon is exchanged as a GeoJSON Polygon (lon/lat order, closed ring).
 */
import { useEffect, useRef, useState } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'

export type GeoJsonPolygon = {
  type: 'Polygon'
  coordinates: number[][][]
}

interface Props {
  latitude: number
  longitude: number
  radiusM: number
  polygon: GeoJsonPolygon | null
  onChange: (polygon: GeoJsonPolygon | null) => void
}

type Mode = 'view' | 'draw' | 'edit'

const OSM_STYLE = {
  version: 8,
  sources: {
    osm: {
      type: 'raster',
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      attribution: '© OpenStreetMap contributors',
    },
  },
  layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
} as const

function ringFromVertices(verts: [number, number][]): GeoJsonPolygon | null {
  if (verts.length < 3) return null
  const ring = [...verts, verts[0]!] as [number, number][]
  return { type: 'Polygon', coordinates: [ring] }
}

function verticesFromPolygon(p: GeoJsonPolygon | null): [number, number][] {
  if (!p) return []
  const ring = p.coordinates[0] || []
  // drop closing duplicate
  return ring.slice(0, -1).map(c => [c[0]!, c[1]!] as [number, number])
}

// Generate a metres-circle as polygon (for the radius-fallback display)
function circlePolygon(lat: number, lon: number, radiusM: number): GeoJSON.Feature {
  const points = 64
  const coords: number[][] = []
  const earth = 6378137
  for (let i = 0; i <= points; i++) {
    const a = (i / points) * 2 * Math.PI
    const dx = (radiusM * Math.cos(a)) / (earth * Math.cos((lat * Math.PI) / 180)) * (180 / Math.PI)
    const dy = (radiusM * Math.sin(a)) / earth * (180 / Math.PI)
    coords.push([lon + dx, lat + dy])
  }
  return {
    type: 'Feature',
    geometry: { type: 'Polygon', coordinates: [coords] },
    properties: {},
  }
}

export default function HomePolygonEditor({
  latitude, longitude, radiusM, polygon, onChange,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<maplibregl.Map | null>(null)
  const [mode, setMode] = useState<Mode>('view')
  const [vertices, setVertices] = useState<[number, number][]>(verticesFromPolygon(polygon))
  const [mapReady, setMapReady] = useState(false)
  // Stable refs so async map handlers always see the latest state
  const modeRef = useRef(mode)
  const vertsRef = useRef(vertices)
  modeRef.current = mode
  vertsRef.current = vertices

  // Sync external polygon changes (e.g. when switching airfields)
  useEffect(() => {
    setVertices(verticesFromPolygon(polygon))
  }, [polygon])

  // Initialize map once
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: OSM_STYLE as any,
      center: [longitude || 11.18, latitude || 47.39],
      zoom: 14,
    })
    mapRef.current = map
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right')

    map.on('load', () => {
      // Airfield centre marker
      new maplibregl.Marker({ color: '#22d3ee' })
        .setLngLat([longitude, latitude])
        .addTo(map)

      // Circular fallback (dashed reference)
      map.addSource('circle', { type: 'geojson', data: circlePolygon(latitude, longitude, radiusM) })
      map.addLayer({
        id: 'circle-line',
        type: 'line',
        source: 'circle',
        paint: {
          'line-color': '#22d3ee',
          'line-width': 1.5,
          'line-dasharray': [2, 2],
          'line-opacity': 0.7,
        },
      })

      // Polygon fill + outline
      map.addSource('polygon', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      })
      map.addLayer({
        id: 'polygon-fill',
        type: 'fill',
        source: 'polygon',
        paint: { 'fill-color': '#22d3ee', 'fill-opacity': 0.18 },
      })
      map.addLayer({
        id: 'polygon-outline',
        type: 'line',
        source: 'polygon',
        paint: { 'line-color': '#22d3ee', 'line-width': 2 },
      })

      // Vertex circles (for edit/draw)
      map.addSource('vertices', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      })
      map.addLayer({
        id: 'vertices-circle',
        type: 'circle',
        source: 'vertices',
        paint: {
          'circle-radius': 6,
          'circle-color': '#fff',
          'circle-stroke-color': '#22d3ee',
          'circle-stroke-width': 2,
        },
      })

      setMapReady(true)
    })

    // Click: add vertex (draw mode) or remove vertex (edit + shift)
    map.on('click', (e) => {
      if (modeRef.current !== 'draw') return
      const next: [number, number][] = [...vertsRef.current, [e.lngLat.lng, e.lngLat.lat]]
      setVertices(next)
    })

    // Double-click in draw mode: finish polygon
    map.on('dblclick', (e) => {
      if (modeRef.current !== 'draw') return
      e.preventDefault()
      finishDraw()
    })

    // Click on vertex in edit mode: shift-click removes
    map.on('click', 'vertices-circle', (e) => {
      if (modeRef.current !== 'edit') return
      const oe = e.originalEvent as MouseEvent
      if (!oe.shiftKey) return
      const idx = (e.features?.[0]?.properties as any)?.idx
      if (typeof idx !== 'number') return
      const next = vertsRef.current.filter((_, i) => i !== idx)
      setVertices(next)
    })

    // Drag vertices in edit mode
    let draggingIdx: number | null = null
    map.on('mousedown', 'vertices-circle', (e) => {
      if (modeRef.current !== 'edit') return
      const oe = e.originalEvent as MouseEvent
      if (oe.shiftKey) return
      e.preventDefault()
      draggingIdx = (e.features?.[0]?.properties as any)?.idx ?? null
      map.getCanvas().style.cursor = 'grabbing'
    })
    map.on('mousemove', (e) => {
      if (draggingIdx === null) return
      const next = [...vertsRef.current]
      next[draggingIdx] = [e.lngLat.lng, e.lngLat.lat]
      setVertices(next)
    })
    map.on('mouseup', () => {
      if (draggingIdx !== null) {
        draggingIdx = null
        map.getCanvas().style.cursor = ''
        // commit current vertices to parent
        const ring = ringFromVertices(vertsRef.current)
        if (ring) onChange(ring)
      }
    })

    return () => { map.remove(); mapRef.current = null }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Re-center when airfield coordinates change
  useEffect(() => {
    const map = mapRef.current
    if (!map || !latitude || !longitude) return
    map.setCenter([longitude, latitude])
    if (map.getSource('circle')) {
      ;(map.getSource('circle') as maplibregl.GeoJSONSource).setData(
        circlePolygon(latitude, longitude, radiusM) as any
      )
    }
  }, [latitude, longitude, radiusM])

  // Redraw whenever vertices change OR the map becomes ready for the first time
  useEffect(() => { redraw() }, [vertices, mapReady])

  // Once the map is ready, fit the view to an existing polygon if any
  useEffect(() => {
    if (!mapReady) return
    const map = mapRef.current
    if (!map) return
    const verts = vertsRef.current
    if (verts.length < 3) return
    const lngs = verts.map(v => v[0])
    const lats = verts.map(v => v[1])
    const bounds: [[number, number], [number, number]] = [
      [Math.min(...lngs), Math.min(...lats)],
      [Math.max(...lngs), Math.max(...lats)],
    ]
    map.fitBounds(bounds, { padding: 60, animate: false, maxZoom: 16 })
  }, [mapReady])

  function redraw() {
    const map = mapRef.current
    if (!map || !mapReady) return
    const polySrc = map.getSource('polygon') as maplibregl.GeoJSONSource | undefined
    const vertSrc = map.getSource('vertices') as maplibregl.GeoJSONSource | undefined
    if (!polySrc || !vertSrc) return

    // Always read the latest vertices via ref to avoid stale closures.
    const verts = vertsRef.current
    const ring = ringFromVertices(verts)
    polySrc.setData({
      type: 'FeatureCollection',
      features: ring
        ? [{ type: 'Feature', geometry: ring, properties: {} }]
        : [],
    })
    vertSrc.setData({
      type: 'FeatureCollection',
      features: verts.map((c, idx) => ({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: c },
        properties: { idx },
      })),
    })
  }

  function startDraw() {
    setVertices([])
    setMode('draw')
    onChange(null)
  }

  function finishDraw() {
    const ring = ringFromVertices(vertsRef.current)
    if (!ring) {
      alert('Polygon braucht mindestens 3 Punkte.')
      return
    }
    onChange(ring)
    setMode('edit')
  }

  function startEdit() {
    if (vertices.length < 3) {
      startDraw()
      return
    }
    setMode('edit')
  }

  function clearPolygon() {
    if (!confirm('Polygon entfernen? Es gilt dann wieder der Kreisradius.')) return
    setVertices([])
    onChange(null)
    setMode('view')
  }

  // Quick rectangle: 4 corners around airfield centre, default ~1500m wide
  function startRectangle() {
    const half = 750 // metres
    const earth = 6378137
    const dLat = (half / earth) * (180 / Math.PI)
    const dLon = (half / (earth * Math.cos((latitude * Math.PI) / 180))) * (180 / Math.PI)
    const v: [number, number][] = [
      [longitude - dLon, latitude - dLat],
      [longitude + dLon, latitude - dLat],
      [longitude + dLon, latitude + dLat],
      [longitude - dLon, latitude + dLat],
    ]
    setVertices(v)
    onChange(ringFromVertices(v))
    setMode('edit')
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {mode === 'view' && (
          <>
            <button type="button" onClick={startDraw}
              className="bg-tower-qdr hover:bg-cyan-500 text-white text-sm font-semibold rounded-lg px-3 py-1.5">
              Polygon zeichnen
            </button>
            <button type="button" onClick={startRectangle}
              className="bg-tower-surface border border-tower-border hover:border-tower-qdr text-gray-300 text-sm font-medium rounded-lg px-3 py-1.5">
              Rechteck einsetzen
            </button>
            {vertices.length >= 3 && (
              <>
                <button type="button" onClick={startEdit}
                  className="bg-tower-surface border border-tower-border hover:border-tower-qdr text-gray-300 text-sm font-medium rounded-lg px-3 py-1.5">
                  Bearbeiten
                </button>
                <button type="button" onClick={clearPolygon}
                  className="text-red-400 hover:text-red-300 text-sm font-medium rounded-lg px-3 py-1.5">
                  Polygon entfernen
                </button>
              </>
            )}
          </>
        )}
        {mode === 'draw' && (
          <>
            <span className="text-sm text-gray-300">
              Klicke in die Karte, um Stuetzpunkte zu setzen. Doppelklick beendet das Polygon.
            </span>
            <button type="button" onClick={finishDraw}
              className="bg-tower-qdr hover:bg-cyan-500 text-white text-sm font-semibold rounded-lg px-3 py-1.5 ml-auto">
              Schliessen
            </button>
            <button type="button" onClick={() => { setVertices([]); setMode('view') }}
              className="text-gray-400 hover:text-white text-sm rounded-lg px-3 py-1.5">
              Abbrechen
            </button>
          </>
        )}
        {mode === 'edit' && (
          <>
            <span className="text-sm text-gray-300">
              Stuetzpunkte ziehen zum Verschieben. Shift-Klick auf einen Punkt loescht ihn.
            </span>
            <button type="button" onClick={() => setMode('view')}
              className="bg-tower-qdr hover:bg-cyan-500 text-white text-sm font-semibold rounded-lg px-3 py-1.5 ml-auto">
              Fertig
            </button>
            <button type="button" onClick={clearPolygon}
              className="text-red-400 hover:text-red-300 text-sm font-medium rounded-lg px-3 py-1.5">
              Polygon entfernen
            </button>
          </>
        )}
      </div>
      <div ref={containerRef} className="w-full h-[420px] rounded-lg border border-tower-border overflow-hidden" />
      <p className="text-xs text-gray-500">
        Der gestrichelte Kreis zeigt den aktuellen Heimatradius als Fallback.
        Wenn ein Polygon definiert ist, wird es fuer die Start-/Landeerkennung verwendet.
      </p>
    </div>
  )
}
