/**
 * WebSocket connection status indicator.
 */
import { useMonitorStore } from '../store/monitorStore'

export default function ConnectionStatus() {
  const connected = useMonitorStore((s) => s.connected)
  const flights = useMonitorStore((s) => s.flights)

  return (
    <div className="flex items-center gap-3 text-sm">
      <div className="flex items-center gap-1.5">
        <span
          className={`w-2 h-2 rounded-full ${
            connected ? 'bg-green-400 animate-pulse' : 'bg-red-500'
          }`}
        />
        <span className={connected ? 'text-green-400' : 'text-red-400'}>
          {connected ? 'Verbunden' : 'Getrennt'}
        </span>
      </div>
      <span className="text-gray-500">|</span>
      <span className="text-gray-400">{flights.size} verfolgt</span>
    </div>
  )
}
