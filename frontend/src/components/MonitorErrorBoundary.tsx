/**
 * Error boundary around the tower monitor.
 *
 * A render crash must never leave an unattended tower display black: show
 * a short notice and reload the page automatically (a reload rebuilds the
 * state through the normalizing full-state/today parsers). The automatic
 * reload is rate-limited via sessionStorage so a persistent crash cannot
 * cause a reload storm.
 */
import { Component, type ReactNode } from 'react'

const AUTO_RELOAD_DELAY_MS = 5_000
const AUTO_RELOAD_MIN_INTERVAL_MS = 60_000
const LAST_RELOAD_KEY = 'monitorErrorReloadAt'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
}

export default class MonitorErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false }
  private reloadTimer: ReturnType<typeof setTimeout> | undefined

  static getDerivedStateFromError(): State {
    return { hasError: true }
  }

  componentDidCatch(error: unknown) {
    console.error('Monitor render crashed:', error)
    if (this.canAutoReload()) {
      this.reloadTimer = setTimeout(() => {
        try {
          sessionStorage.setItem(LAST_RELOAD_KEY, String(Date.now()))
        } catch {
          // Browser storage unavailable - reload anyway
        }
        window.location.reload()
      }, AUTO_RELOAD_DELAY_MS)
    }
  }

  componentWillUnmount() {
    clearTimeout(this.reloadTimer)
  }

  private canAutoReload(): boolean {
    try {
      const last = Number(sessionStorage.getItem(LAST_RELOAD_KEY) || 0)
      return Date.now() - last > AUTO_RELOAD_MIN_INTERVAL_MS
    } catch {
      return true
    }
  }

  render() {
    if (!this.state.hasError) return this.props.children
    return (
      <div className="h-screen bg-tower-bg text-gray-100 flex flex-col items-center justify-center gap-4 px-6 text-center">
        <div className="text-lg font-bold text-white">Anzeigefehler im Monitor</div>
        <p className="text-sm text-gray-400 max-w-md">
          Die Anzeige wird automatisch neu geladen. Falls nicht, bitte die
          Seite manuell neu laden.
        </p>
        <button
          onClick={() => window.location.reload()}
          className="bg-tower-qdr hover:bg-cyan-500 text-white text-sm font-semibold rounded-lg px-6 py-2.5 transition-colors"
        >
          Jetzt neu laden
        </button>
      </div>
    )
  }
}
