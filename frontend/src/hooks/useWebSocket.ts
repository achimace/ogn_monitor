/**
 * WebSocket hook with auto-reconnect and delta merge into Zustand store.
 *
 * Connects to ws://host/ws/monitor/{slug}
 * Handles: full_state, flight_update, flight_added, flight_removed, alarm, ping/pong
 */
import { useEffect, useRef, useCallback } from 'react'
import { useMonitorStore } from '../store/monitorStore'
import type { WsMessage } from '../types/flight'

const RECONNECT_BASE_MS = 1000
const RECONNECT_MAX_MS = 30000
const PONG_TIMEOUT_MS = 35000

export function useWebSocket(slug: string | null) {
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectAttempt = useRef(0)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const pongTimer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const mountedRef = useRef(true)

  const {
    setFullState,
    applyDelta,
    addFlight,
    removeFlight,
    addAlarm,
    clearAlarm,
    setConnected,
  } = useMonitorStore()

  const connect = useCallback(() => {
    if (!slug || !mountedRef.current) return

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${protocol}//${window.location.host}/ws/monitor/${slug}`

    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      reconnectAttempt.current = 0
      setConnected(true)
    }

    ws.onmessage = (event) => {
      try {
        const msg: WsMessage = JSON.parse(event.data)
        handleMessage(msg)
      } catch {
        // Ignore malformed messages
      }
    }

    ws.onclose = () => {
      setConnected(false)
      wsRef.current = null
      scheduleReconnect()
    }

    ws.onerror = () => {
      // onclose will fire after onerror
    }
  }, [slug, setConnected]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleMessage = useCallback((msg: WsMessage) => {
    // Reset pong timer on any server message
    clearTimeout(pongTimer.current)
    pongTimer.current = setTimeout(() => {
      // No messages for 35s - force reconnect
      wsRef.current?.close()
    }, PONG_TIMEOUT_MS)

    switch (msg.type) {
      case 'full_state':
        setFullState(msg.flights, msg.stats)
        break

      case 'flight_update':
        applyDelta(msg.flarmId, msg.d)
        break

      case 'flight_added':
        addFlight(msg.flight)
        break

      case 'flight_removed':
        removeFlight(msg.flarmId)
        // Clear any alarm for this flight
        clearAlarm(msg.flarmId)
        break

      case 'alarm':
        addAlarm({
          flarmId: msg.flarmId,
          severity: msg.severity,
          scenario: msg.scenario,
          registration: msg.registration,
          message: msg.message,
          lastPosition: msg.lastPosition,
          timestamp: new Date().toISOString(),
        })
        break

      case 'ping':
        // Respond with pong
        if (wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send(JSON.stringify({ type: 'pong' }))
        }
        break
    }
  }, [setFullState, applyDelta, addFlight, removeFlight, addAlarm, clearAlarm])

  const scheduleReconnect = useCallback(() => {
    if (!mountedRef.current) return

    const delay = Math.min(
      RECONNECT_BASE_MS * Math.pow(2, reconnectAttempt.current),
      RECONNECT_MAX_MS
    )
    reconnectAttempt.current++

    reconnectTimer.current = setTimeout(() => {
      if (mountedRef.current) connect()
    }, delay)
  }, [connect])

  useEffect(() => {
    mountedRef.current = true
    connect()

    return () => {
      mountedRef.current = false
      clearTimeout(reconnectTimer.current)
      clearTimeout(pongTimer.current)
      if (wsRef.current) {
        wsRef.current.onclose = null // Prevent reconnect on unmount
        wsRef.current.close()
      }
      setConnected(false)
    }
  }, [slug]) // eslint-disable-line react-hooks/exhaustive-deps
}
