/**
 * MapLibre GL map view for Tower Monitor.
 *
 * Features:
 * - Aircraft as direction arrows with registration labels
 * - Color coding: blue=flying, green=landed, red=alarm, orange=outlanding
 * - Home airfield marker with 800m radius circle
 * - QDR line from home to each aircraft (dashed, with degree label)
 * - Focus on one aircraft (row click or marker click): center + zoom, follow while it moves
 * - Flight track of the focused aircraft since its last takeoff (stored points + live beacons)
 * - Alarm: pulsing red circle at last position
 * - Auto-zoom to fit all active flights
 */
import { useEffect, useRef, useState } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import type { Flight, TrackPoint } from '../types/flight'
import { fetchTrack } from '../api/monitor'

interface MapViewProps {
  flights: Flight[]
  airfieldLat?: number
  airfieldLng?: number
  airfieldName?: string
  /** Needed to load the stored track of the focused aircraft. */
  airfieldSlug?: string
  /** FLARM ID of the aircraft to center on and follow. */
  focusFlarmId?: string | null
  /** Called when the user leaves focus mode via "Zurück zur Übersicht". */
  onClearFocus?: () => void
  /** Called when the user clicks an aircraft marker on the map. */
  onFocus?: (flarmId: string) => void
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

/** Minimum zoom when focusing an aircraft. */
const FOCUS_ZOOM = 13
/**
 * Maximum hours of stored track to load for the focused aircraft; also the
 * fallback window when the takeoff time is unknown (backend limit is 24 h).
 */
const TRACK_HOURS = 24
/** Smallest look-back window the backend accepts. */
const TRACK_MIN_HOURS = 0.1
/**
 * Margin before the takeoff time that is still part of the track, so the
 * ground roll is drawn (the takeoff is detected a few seconds after the
 * aircraft started moving).
 */
const TRACK_TAKEOFF_MARGIN_MS = 5 * 60 * 1000
/** Consecutive track points further apart than this start a new line segment. */
const TRACK_GAP_MS = 10 * 60 * 1000
/**
 * Minimum spacing between live points appended to the track. The backend
 * stream is thinned to one point per 5 s; beacons arrive every 1-4 s, so
 * without this the live part would be denser than the stored one and the
 * whole GeoJSON would be re-serialised on every beacon.
 */
const TRACK_LIVE_MIN_INTERVAL_MS = 5000
/** Age limit for in-memory track points, mirrors TRACK_HOURS. */
const TRACK_MAX_AGE_MS = TRACK_HOURS * 60 * 60 * 1000
/** Fallback track color when the focused flight has no status color. */
const TRACK_FALLBACK_COLOR = '#38bdf8'

/**
 * Airspace / airfield overlay (optional raster layer).
 *
 * The tiles are rendered server-side by SkyLines (skylines.aero) from
 * DAeC OpenAir data for Germany (file of 2020-04-28), Austro Control 2015 and
 * Welt2000 airports. They serve as orientation only and are NOT authoritative
 * – no current airspace status. SkyLines is a volunteer service without SLA,
 * so the overlay must degrade gracefully when tiles are unavailable.
 */
const AIRSPACE_TILE_URL =
  'https://skylines.aero/mapproxy/tiles/1.0.0/airspace+airports/EPSG3857/{z}/{x}/{y}.png'
const AIRSPACE_ATTRIBUTION =
  'Lufträume/Flugplätze: <a href="https://skylines.aero">SkyLines</a> (DE-Luftraumstand 04/2020)'
/** Source and layer id of the overlay in the MapLibre style. */
const AIRSPACE_LAYER_ID = 'skylines-airspace'
/** Below this zoom the tiles are too cluttered to be useful. */
const AIRSPACE_MIN_ZOOM = 6
/** localStorage key for the on/off choice of the overlay. */
const AIRSPACE_STORAGE_KEY = 'monitor.airspaceOverlay'

/** Read the persisted overlay choice; default on when unreadable. */
function readAirspacePreference(): boolean {
  try {
    const stored = localStorage.getItem(AIRSPACE_STORAGE_KEY)
    return stored === null ? true : stored === 'true'
  } catch {
    return true
  }
}

/** Persist the overlay choice; failures (private mode, quota) are ignored. */
function writeAirspacePreference(on: boolean) {
  try {
    localStorage.setItem(AIRSPACE_STORAGE_KEY, on ? 'true' : 'false')
  } catch {
    // ignore
  }
}

/** Apply the overlay visibility if the map style already has the layer. */
function applyAirspaceVisibility(map: maplibregl.Map, on: boolean) {
  if (map.getLayer(AIRSPACE_LAYER_ID)) {
    map.setLayoutProperty(AIRSPACE_LAYER_ID, 'visibility', on ? 'visible' : 'none')
  }
}

type TrackInfo =
  | { state: 'loading' }
  /** `takeoffMs` is set when the track starts at the flight's takeoff, else `hours` applies. */
  | { state: 'loaded'; count: number; hours: number; takeoffMs: number | null }
  | { state: 'error' }

export default function MapView({
  flights, airfieldLat, airfieldLng, airfieldName, airfieldSlug, focusFlarmId, onClearFocus, onFocus,
}: MapViewProps) {
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

  // --- Focus / track state (only used while focusFlarmId is set) ---
  // Latest flights list, readable from the focus effect without making it
  // re-run on every beacon update.
  const flightsRef = useRef<Flight[]>(flights)
  flightsRef.current = flights
  // Latest onFocus callback for the marker click listeners. The listener is
  // attached once per marker at creation; going through the ref means beacon
  // updates never have to re-bind it and a changed callback is still honoured.
  const onFocusRef = useRef(onFocus)
  onFocusRef.current = onFocus
  // Takeoff time of the focused flight as a primitive dependency: the focus
  // effect must refetch when a new flight of the same aircraft starts (new
  // takeoff time after a restart) but not on every beacon.
  const focusedFlight = focusFlarmId ? flights.find((f) => f.flarmId === focusFlarmId) : undefined
  const focusTakeoffIso = focusedFlight?.takeoffTime ?? ''
  // In-memory track of the focused aircraft: stored points from the API plus
  // live positions appended from the flights prop.
  const trackPointsRef = useRef<TrackPoint[]>([])
  // Live points collected while the stored track is still loading.
  const pendingLiveRef = useRef<TrackPoint[]>([])
  const trackLoadedRef = useRef(false)
  // Last live position we appended – avoids duplicate points on non-position deltas.
  const lastLivePosRef = useRef<{ lat: number; lon: number } | null>(null)
  const trackAbortRef = useRef<AbortController | null>(null)
  const [trackInfo, setTrackInfo] = useState<TrackInfo | null>(null)

  // --- Airspace overlay ---
  // Read once on mount; the ref lets the map 'load' handler apply the stored
  // choice right after addLayer without re-creating the map on toggle.
  const [airspaceOn, setAirspaceOn] = useState<boolean>(readAirspacePreference)
  const airspaceOnRef = useRef(airspaceOn)
  airspaceOnRef.current = airspaceOn

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
      // Airspace / airfield overlay – added first so the radius circle, QDR
      // lines and track render on top of it. Visibility follows the stored
      // user choice; 'load' may fire before any toggle, hence the ref.
      map.addSource(AIRSPACE_LAYER_ID, {
        type: 'raster',
        tiles: [AIRSPACE_TILE_URL],
        tileSize: 256,
        attribution: AIRSPACE_ATTRIBUTION,
      })
      map.addLayer({
        id: AIRSPACE_LAYER_ID,
        type: 'raster',
        source: AIRSPACE_LAYER_ID,
        minzoom: AIRSPACE_MIN_ZOOM,
        layout: { visibility: airspaceOnRef.current ? 'visible' : 'none' },
        paint: { 'raster-opacity': 0.75 },
      })

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

      // Track of the focused aircraft. Aircraft markers are HTML markers and
      // therefore always render above style layers – the line stays below them.
      map.addSource('track-line', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      })
      map.addLayer({
        id: 'track-line',
        type: 'line',
        source: 'track-line',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': ['get', 'color'],
          'line-width': 2.5,
          'line-opacity': 0.85,
        },
      })

      setMapReady(true)
    })

    mapRef.current = map

    // MapLibre only listens to window resizes; the container also changes
    // size when the layout switches between "Karte" and "Split".
    const observer = new ResizeObserver(() => map.resize())
    observer.observe(mapContainer.current)
    // markersRef always holds the same Map object; capture it so the cleanup
    // does not read the ref after unmount (react-hooks/exhaustive-deps).
    const markers = markersRef.current

    return () => {
      observer.disconnect()
      trackAbortRef.current?.abort()
      map.remove()
      mapRef.current = null
      markers.clear()
    }
  }, [airfieldLat, airfieldLng, airfieldName])

  /** Run a camera move that must not count as a user gesture. */
  function moveProgrammatically(map: maplibregl.Map, move: () => void) {
    programmaticMoveRef.current = true
    const clear = () => {
      programmaticMoveRef.current = false
      map.off('moveend', clear)
    }
    map.on('moveend', clear)
    move()
  }

  // Focus changed (aircraft or its takeoff time): fly to the aircraft, load
  // its stored track from the last takeoff up to now.
  useEffect(() => {
    if (!mapReady || !mapRef.current) return
    const map = mapRef.current

    // Always start clean – also covers unfocus and aircraft change.
    trackAbortRef.current?.abort()
    trackAbortRef.current = null
    trackPointsRef.current = []
    pendingLiveRef.current = []
    trackLoadedRef.current = false
    lastLivePosRef.current = null
    setTrackSource(map, [], TRACK_FALLBACK_COLOR)
    setTrackInfo(null)

    if (!focusFlarmId) return

    const flight = flightsRef.current.find((f) => f.flarmId === focusFlarmId)
    if (flight && flight.latitude && flight.longitude) {
      // Take over the camera: auto-fit must not fight the focus.
      setUserInteracted(true)
      moveProgrammatically(map, () =>
        map.flyTo({
          center: [flight.longitude, flight.latitude],
          zoom: Math.max(map.getZoom(), FOCUS_ZOOM),
          duration: 800,
        })
      )
    }

    if (!airfieldSlug) return

    const controller = new AbortController()
    trackAbortRef.current = controller
    setTrackInfo({ state: 'loading' })

    // Window: from the takeoff (minus the ground-roll margin) up to now; the
    // full 24 h only when the takeoff time is unknown or unparseable.
    const takeoffMs = parseTakeoff(focusTakeoffIso)
    const hours = trackWindowHours(takeoffMs)

    fetchTrack(airfieldSlug, focusFlarmId, hours, controller.signal)
      .then((track) => {
        if (controller.signal.aborted) return
        // The window is rounded up, so an earlier flight of the same aircraft
        // may still be inside it – cut everything before this takeoff. The gap
        // splitting in trackToGeoJson remains as a safety net.
        const stored = takeoffMs === null
          ? track.points
          : track.points.filter((p) => {
            const t = Date.parse(p.t)
            return !isFinite(t) || t >= takeoffMs - TRACK_TAKEOFF_MARGIN_MS
          })
        // Merge: stored points first, then live points newer than the last stored one.
        const lastStored = stored.length > 0 ? stored[stored.length - 1] : undefined
        const lastStoredT = lastStored ? Date.parse(lastStored.t) : -Infinity
        const live = pendingLiveRef.current.filter((p) => Date.parse(p.t) > lastStoredT)
        trackPointsRef.current = [...stored, ...live]
        pendingLiveRef.current = []
        trackLoadedRef.current = true
        const current = flightsRef.current.find((f) => f.flarmId === focusFlarmId)
        setTrackSource(map, trackPointsRef.current, trackColor(current))
        setTrackInfo({
          state: 'loaded',
          count: stored.length,
          hours: trackHoursLabel(track.since),
          takeoffMs,
        })
        // No live position (e.g. archived flight of today): show the stored
        // track instead, otherwise the focus would change nothing visible.
        const hasLivePos = !!(current && current.latitude && current.longitude)
        if (!hasLivePos && stored.length > 0) {
          const bounds = new maplibregl.LngLatBounds()
          for (const p of stored) {
            if (isFinite(p.lat) && isFinite(p.lon)) bounds.extend([p.lon, p.lat])
          }
          if (!bounds.isEmpty()) {
            setUserInteracted(true)
            moveProgrammatically(map, () =>
              map.fitBounds(bounds, { padding: 60, maxZoom: FOCUS_ZOOM, duration: 800 })
            )
          }
        }
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        // Keep the live part of the track even if the stored one failed.
        trackLoadedRef.current = true
        trackPointsRef.current = pendingLiveRef.current
        pendingLiveRef.current = []
        setTrackInfo({ state: 'error' })
        console.warn('Track konnte nicht geladen werden', err)
      })

    return () => {
      controller.abort()
    }
  }, [focusFlarmId, focusTakeoffIso, mapReady, airfieldSlug])

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
      const isFocused = flight.flarmId === focusFlarmId

      let marker = markersRef.current.get(flight.flarmId)
      if (marker) {
        // Update position
        marker.setLngLat([flight.longitude, flight.latitude])
        // Update element
        const el = marker.getElement()
        updateMarkerElement(el, flight.trackDeg, color, label, isAlarm, isFocused)
      } else {
        // Create new marker
        const el = createMarkerElement(flight.trackDeg, color, label, isAlarm, isFocused)
        // Click focuses the aircraft (same as a row click in the table). The
        // FLARM id is fixed per marker; the callback is read from the ref so
        // this listener never needs re-binding. stopPropagation keeps the
        // click away from the map's own handlers.
        const flarmId = flight.flarmId
        el.style.cursor = 'pointer'
        el.addEventListener('click', (e) => {
          e.stopPropagation()
          const f = flightsRef.current.find((x) => x.flarmId === flarmId)
          if (!f || !f.latitude || !f.longitude) return
          onFocusRef.current?.(flarmId)
        })
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

    // Focus mode: grow the track with live positions and keep the aircraft
    // in view – but only re-center when it actually left the viewport, a
    // camera move on every beacon would jitter.
    if (focusFlarmId) {
      const focused = flights.find((f) => f.flarmId === focusFlarmId)
      if (focused && focused.latitude && focused.longitude) {
        const last = lastLivePosRef.current
        const moved = !last || last.lat !== focused.latitude || last.lon !== focused.longitude
        if (moved) {
          lastLivePosRef.current = { lat: focused.latitude, lon: focused.longitude }
          const point = liveTrackPoint(focused)
          if (trackLoadedRef.current) {
            if (appendLivePoint(trackPointsRef.current, point)) {
              setTrackSource(map, trackPointsRef.current, trackColor(focused))
            }
          } else {
            appendLivePoint(pendingLiveRef.current, point)
          }

          const pos: [number, number] = [focused.longitude, focused.latitude]
          if (!map.getBounds().contains(pos)) {
            moveProgrammatically(map, () => map.easeTo({ center: pos, duration: 600 }))
          }
        }
      }
      return
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
          moveProgrammatically(map, () =>
            map.fitBounds(bounds, { padding: 60, maxZoom: 13, duration: 500 })
          )
        }
      }
    }
  }, [flights, mapReady, airfieldLat, airfieldLng, userInteracted, focusFlarmId])

  function resetView() {
    setUserInteracted(false)
    // Leaving focus mode also clears the track (see focus effect).
    onClearFocus?.()
  }

  function toggleAirspace() {
    const next = !airspaceOn
    setAirspaceOn(next)
    writeAirspacePreference(next)
    if (mapRef.current) applyAirspaceVisibility(mapRef.current, next)
  }

  // Keep the layer in sync with the state, also after the style finished
  // loading (mapReady) or when the map was re-created with new coordinates.
  useEffect(() => {
    if (!mapReady || !mapRef.current) return
    applyAirspaceVisibility(mapRef.current, airspaceOn)
  }, [airspaceOn, mapReady])

  const showReset = userInteracted || !!focusFlarmId

  return (
    <div className="relative w-full h-full min-h-[400px]">
      <div ref={mapContainer} className="absolute inset-0 rounded-lg overflow-hidden" />
      <button
        type="button"
        onClick={toggleAirspace}
        aria-pressed={airspaceOn}
        className={`absolute top-3 right-14 z-10 text-sm font-semibold rounded-lg
          px-3 py-2 shadow-lg backdrop-blur border transition-colors
          flex items-center gap-2 ${
          airspaceOn
            ? 'bg-tower-qdr text-white border-tower-qdr'
            : 'bg-tower-surface/95 text-gray-300 border-tower-border hover:text-white'
        }`}
        title="Lufträume und Flugplätze (SkyLines, Orientierung – kein aktueller Luftraumstand)"
      >
        <span aria-hidden>{airspaceOn ? '◉' : '○'}</span>
        Lufträume
      </button>
      {showReset && (
        <div className="absolute top-3 left-3 z-10 flex flex-col items-start gap-1.5">
          <button
            onClick={resetView}
            className="bg-tower-surface/95 hover:bg-tower-qdr
              border border-tower-qdr/60 text-tower-qdr hover:text-white
              text-sm font-semibold rounded-lg px-3 py-2 shadow-lg backdrop-blur
              transition-colors flex items-center gap-2"
            title="Karte automatisch auf alle Flugzeuge zentrieren"
          >
            <span aria-hidden>⌖</span>
            Zurück zur Übersicht
          </button>
          {focusFlarmId && trackInfo && (
            <div className="bg-tower-surface/90 border border-tower-border text-gray-400
              text-xs rounded-md px-2 py-1 shadow backdrop-blur">
              {trackInfo.state === 'loading' && 'Spur wird geladen …'}
              {trackInfo.state === 'loaded' && (trackInfo.count > 0
                ? (trackInfo.takeoffMs !== null
                  ? `Spur seit Start ${formatUtcHm(trackInfo.takeoffMs)} UTC · ${trackInfo.count} Punkte`
                  : `Spur: letzte ${trackInfo.hours} h · ${trackInfo.count} Punkte`)
                : 'keine Spur gespeichert')}
              {trackInfo.state === 'error' && 'Spur nicht verfügbar'}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function trackColor(flight: Flight | undefined): string {
  return (flight && STATUS_COLORS[flight.status]) || TRACK_FALLBACK_COLOR
}

/** Takeoff time in epoch ms, or null when missing/unparseable. */
function parseTakeoff(iso: string): number | null {
  if (!iso) return null
  const t = Date.parse(iso)
  return isFinite(t) ? t : null
}

/**
 * Look-back window in hours for the track request: from the takeoff (plus the
 * ground-roll margin) up to now, clamped to the backend's limits. Without a
 * takeoff time the full TRACK_HOURS are requested.
 */
function trackWindowHours(takeoffMs: number | null): number {
  if (takeoffMs === null) return TRACK_HOURS
  const hours = (Date.now() - takeoffMs + TRACK_TAKEOFF_MARGIN_MS) / (60 * 60 * 1000)
  const clamped = Math.min(TRACK_HOURS, Math.max(TRACK_MIN_HOURS, hours))
  // Three decimals are enough (≈4 s) and keep the query string short.
  return Math.round(clamped * 1000) / 1000
}

/** "HH:MM" in UTC for the track legend. */
function formatUtcHm(ms: number): string {
  return new Date(ms).toLocaleTimeString('de-DE', {
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'UTC',
  })
}

/**
 * Hours covered by a track response, derived from its `since` timestamp so
 * the legend matches what the backend actually returned. Falls back to
 * TRACK_HOURS when `since` is missing or unparseable (older backend).
 */
function trackHoursLabel(since: string): number {
  const sinceT = Date.parse(since)
  if (!isFinite(sinceT)) return TRACK_HOURS
  const hours = Math.round((Date.now() - sinceT) / (60 * 60 * 1000))
  return hours > 0 ? hours : TRACK_HOURS
}

/**
 * Append a live point to a track array in place, matching the backend's
 * 5 s thinning, and drop points older than TRACK_HOURS from the front so the
 * array stays bounded during a long tower session.
 *
 * Returns true when the array changed (caller should re-render the source).
 */
function appendLivePoint(points: TrackPoint[], point: TrackPoint): boolean {
  const lastPoint = points.length > 0 ? points[points.length - 1] : undefined
  if (lastPoint) {
    const dt = Date.parse(point.t) - Date.parse(lastPoint.t)
    if (isFinite(dt) && dt < TRACK_LIVE_MIN_INTERVAL_MS) return false
  }
  points.push(point)

  const cutoff = Date.now() - TRACK_MAX_AGE_MS
  let first = points[0]
  while (first !== undefined) {
    const t = Date.parse(first.t)
    if (!isFinite(t) || t >= cutoff) break
    points.shift()
    first = points[0]
  }
  return true
}

/** Build a track point from the live hot-state position of a flight. */
function liveTrackPoint(f: Flight): TrackPoint {
  const t = f.lastSeen && !isNaN(Date.parse(f.lastSeen)) ? f.lastSeen : new Date().toISOString()
  return {
    t,
    lat: f.latitude,
    lon: f.longitude,
    alt: f.altitudeM,
    agl: f.altitudeAgl,
    speed: f.speedKmh,
    vs: f.verticalSpeedMs,
    track: f.trackDeg,
  }
}

/**
 * Convert track points to one LineString per continuous segment. A gap of
 * more than TRACK_GAP_MS between consecutive points starts a new segment so
 * separate flights of the same aircraft are not connected.
 */
function trackToGeoJson(points: TrackPoint[], color: string): GeoJSON.FeatureCollection {
  const segments: [number, number][][] = []
  let current: [number, number][] = []
  let prevT: number | null = null

  for (const p of points) {
    if (!isFinite(p.lat) || !isFinite(p.lon)) continue
    const t = Date.parse(p.t)
    if (prevT !== null && isFinite(t) && t - prevT > TRACK_GAP_MS && current.length > 0) {
      segments.push(current)
      current = []
    }
    current.push([p.lon, p.lat])
    if (isFinite(t)) prevT = t
  }
  if (current.length > 0) segments.push(current)

  return {
    type: 'FeatureCollection',
    features: segments
      .filter((seg) => seg.length >= 2)
      .map((seg) => ({
        type: 'Feature' as const,
        properties: { color },
        geometry: { type: 'LineString' as const, coordinates: seg },
      })),
  }
}

function setTrackSource(map: maplibregl.Map, points: TrackPoint[], color: string) {
  const source = map.getSource('track-line') as maplibregl.GeoJSONSource | undefined
  if (source) {
    source.setData(trackToGeoJson(points, color))
  }
}

function createMarkerElement(trackDeg: number, color: string, label: string, isAlarm: boolean, isFocused: boolean): HTMLDivElement {
  const el = document.createElement('div')
  updateMarkerElement(el, trackDeg, color, label, isAlarm, isFocused)
  return el
}

/**
 * Build the static marker DOM once. Data-derived values (label, colour,
 * heading) are applied in updateMarkerElement via textContent / style
 * properties, never via innerHTML, so a registration or FLARM id from the
 * DDB / tenant forms cannot inject markup.
 */
function buildMarkerDom(el: HTMLElement): { labelEl: HTMLDivElement; arrowEl: HTMLDivElement } {
  // The wrapper is exactly the arrow's box (12x16 px) and nothing else, so
  // MapLibre's anchor:'center' puts the aircraft symbol on the coordinate.
  // The label hangs above it with absolute positioning and therefore does
  // not shift the anchor. (Before, the label+arrow column was centred on
  // the coordinate and additionally translated by -50%/-50%: a fixed pixel
  // offset that became a growing geographic error when zooming out.)
  const wrapper = document.createElement('div')
  wrapper.style.cssText = 'position:relative;width:12px;height:16px'

  const labelEl = document.createElement('div')
  labelEl.style.cssText = 'position:absolute;bottom:100%;left:50%;transform:translateX(-50%);margin-bottom:2px;color:white;padding:1px 4px;border-radius:3px;white-space:nowrap;font-weight:bold'

  const arrowEl = document.createElement('div')
  arrowEl.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;border-left:6px solid transparent;border-right:6px solid transparent;transform-origin:50% 50%;filter:drop-shadow(0 0 2px rgba(0,0,0,0.5))'

  wrapper.appendChild(labelEl)
  wrapper.appendChild(arrowEl)
  el.replaceChildren(wrapper)
  return { labelEl, arrowEl }
}

function updateMarkerElement(el: HTMLElement, trackDeg: number, color: string, label: string, isAlarm: boolean, isFocused: boolean) {
  // Reuse the existing structure on every beacon; build it only once.
  const wrapper = el.firstElementChild
  let labelEl = wrapper?.firstElementChild as HTMLDivElement | null | undefined
  let arrowEl = wrapper?.lastElementChild as HTMLDivElement | null | undefined
  if (!labelEl || !arrowEl || labelEl === arrowEl) {
    ({ labelEl, arrowEl } = buildMarkerDom(el))
  }

  // Focused aircraft: slightly larger label with a white ring so it stands out.
  labelEl.textContent = label
  labelEl.style.fontSize = isFocused ? '12px' : '10px'
  labelEl.style.background = color
  labelEl.style.boxShadow = isFocused ? '0 0 0 2px #fff, 0 0 6px rgba(0,0,0,0.6)' : ''
  labelEl.style.animation = isAlarm ? 'pulse 0.5s ease-in-out infinite' : ''

  const heading = Number.isFinite(trackDeg) ? trackDeg : 0
  arrowEl.style.borderBottom = `16px solid ${color}`
  arrowEl.style.transform = `rotate(${heading}deg)`
}

function addAirfieldMarker(map: maplibregl.Map, lat: number, lng: number, name: string) {
  const el = document.createElement('div')

  // Same anchoring rule as the aircraft markers: the wrapper is the dot's
  // box, the label floats above it, anchor 'center' = dot on the coordinate.
  const wrapper = document.createElement('div')
  wrapper.style.cssText = 'position:relative;width:12px;height:12px'

  const labelEl = document.createElement('div')
  labelEl.style.cssText = 'position:absolute;bottom:100%;left:50%;transform:translateX(-50%);margin-bottom:4px;font-size:11px;color:#fff;background:#1e40af;padding:2px 6px;border-radius:4px;font-weight:bold;white-space:nowrap'
  // Tenant-provided airfield name: textContent only, never markup.
  labelEl.textContent = name || 'HOME'

  const dotEl = document.createElement('div')
  dotEl.style.cssText = 'position:absolute;left:0;top:0;width:12px;height:12px;box-sizing:border-box;background:#1e40af;border:2px solid white;border-radius:50%'

  wrapper.appendChild(labelEl)
  wrapper.appendChild(dotEl)
  el.appendChild(wrapper)

  new maplibregl.Marker({ element: el, anchor: 'center' })
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
