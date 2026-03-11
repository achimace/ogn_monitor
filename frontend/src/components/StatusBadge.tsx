/**
 * Status badge component for flight status display.
 */
import type { FlightStatus } from '../types/flight'

const STATUS_CONFIG: Record<string, { label: string; bg: string; text: string; blink?: boolean }> = {
  flying: { label: 'OK', bg: 'bg-green-600/30', text: 'text-green-300' },
  towing: { label: 'SCHLEPP', bg: 'bg-yellow-600/30', text: 'text-yellow-300' },
  ground: { label: 'BODEN', bg: 'bg-gray-600/30', text: 'text-gray-400' },
  takeoff: { label: 'START', bg: 'bg-blue-600/30', text: 'text-blue-300' },
  landing: { label: 'LDG', bg: 'bg-green-800/30', text: 'text-green-400' },
  outlanding: { label: 'OUT', bg: 'bg-orange-600/30', text: 'text-orange-300' },
  outlanding_pending: { label: 'OUT?', bg: 'bg-orange-600/30', text: 'text-orange-300' },
  diverted: { label: 'DIVERTED', bg: 'bg-orange-600/30', text: 'text-orange-300' },
  signal_lost: { label: 'LOST', bg: 'bg-yellow-600/30', text: 'text-yellow-300' },
  alarm: { label: 'ALARM', bg: 'bg-red-600/40', text: 'text-red-300', blink: true },
  emergency: { label: 'NOTFALL', bg: 'bg-red-600/50', text: 'text-red-200', blink: true },
}

interface StatusBadgeProps {
  status: FlightStatus
  elapsedMinutes?: number
}

export default function StatusBadge({ status, elapsedMinutes }: StatusBadgeProps) {
  const config = STATUS_CONFIG[status] ?? STATUS_CONFIG['ground']!

  return (
    <div className="flex flex-col items-center gap-1">
      <span
        className={`px-2.5 py-1 rounded text-xs font-bold uppercase ${config.bg} ${config.text} ${
          config.blink ? 'animate-pulse' : ''
        }`}
      >
        {config.label}
      </span>
      {elapsedMinutes !== undefined && elapsedMinutes > 0 && (
        <span className="text-[10px] text-gray-500">{elapsedMinutes} min</span>
      )}
    </div>
  )
}
