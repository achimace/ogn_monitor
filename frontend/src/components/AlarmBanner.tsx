/**
 * Alarm banner with audio alert and acknowledgment.
 */
import { useRef, useEffect } from 'react'
import { useMonitorStore, type AlarmEvent } from '../store/monitorStore'

export default function AlarmBanner() {
  const alarms = useMonitorStore((s) => s.alarms)
  const acknowledgeAlarm = useMonitorStore((s) => s.acknowledgeAlarm)
  const clearAlarm = useMonitorStore((s) => s.clearAlarm)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  // Unacknowledged alarms trigger audio
  const unacked = alarms.filter((a) => !a.acknowledged)

  useEffect(() => {
    if (unacked.length > 0) {
      // Play alarm sound (use browser beep as fallback)
      try {
        const ctx = new AudioContext()
        const osc = ctx.createOscillator()
        const gain = ctx.createGain()
        osc.connect(gain)
        gain.connect(ctx.destination)
        osc.frequency.value = 880
        gain.gain.value = 0.3
        osc.start()
        setTimeout(() => { osc.stop(); ctx.close() }, 500)
      } catch {
        // Audio not available
      }
    }
  }, [unacked.length])

  if (alarms.length === 0) return null

  return (
    <div className="space-y-2 mb-4">
      {alarms.map((alarm) => (
        <AlarmRow
          key={alarm.flarmId}
          alarm={alarm}
          onAcknowledge={() => acknowledgeAlarm(alarm.flarmId)}
          onClear={() => clearAlarm(alarm.flarmId)}
        />
      ))}
      <audio ref={audioRef} />
    </div>
  )
}

function AlarmRow({ alarm, onAcknowledge, onClear }: {
  alarm: AlarmEvent; onAcknowledge: () => void; onClear: () => void
}) {
  const isEmergency = alarm.severity === 'CRITICAL'
  const blinkClass = !alarm.acknowledged
    ? isEmergency ? 'animate-[pulse_0.5s_ease-in-out_infinite]' : 'animate-pulse'
    : ''

  return (
    <div className={`rounded-xl border p-4 ${
      isEmergency
        ? `bg-red-900/60 border-red-500 ${blinkClass}`
        : `bg-red-900/30 border-red-700 ${blinkClass}`
    }`}>
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-3 mb-1">
            <span className="text-red-300 font-bold text-lg">
              {alarm.scenario === 'EMERGENCY' ? '!!! NOTFALL !!!' : alarm.scenario}
            </span>
            <span className="text-white font-bold">{alarm.registration}</span>
            {alarm.competitionSign && (
              <span className="text-gray-400 text-sm">{alarm.competitionSign}</span>
            )}
          </div>
          <p className="text-red-200 text-sm mb-2">{alarm.message}</p>
          <div className="text-sm text-gray-300 font-mono">
            QDR {alarm.lastPosition.qdrDeg}&deg; ({alarm.lastPosition.bearingText})
            {' '}{alarm.lastPosition.distanceKm.toFixed(1)} km
            {' '}{alarm.lastPosition.altitudeM} m
          </div>
          {alarm.lastPosition.elapsedMinutes > 0 && (
            <div className="text-xs text-red-300 mt-1">
              Letztes Signal vor {alarm.lastPosition.elapsedMinutes} min
            </div>
          )}
        </div>

        <div className="flex flex-col gap-2 ml-4 shrink-0">
          {!alarm.acknowledged && (
            <button
              onClick={onAcknowledge}
              className="bg-yellow-600 hover:bg-yellow-500 text-white text-xs font-semibold rounded px-3 py-1.5 transition-colors"
            >
              Quittieren
            </button>
          )}
          <button
            onClick={onClear}
            className="bg-green-700 hover:bg-green-600 text-white text-xs font-semibold rounded px-3 py-1.5 transition-colors"
          >
            Entwarnung
          </button>
        </div>
      </div>
    </div>
  )
}
