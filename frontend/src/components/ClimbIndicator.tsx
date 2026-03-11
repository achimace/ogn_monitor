/**
 * Climb/Sink indicator arrows.
 *
 * ^^ Green Bold: Strong climb (> +2.0 m/s)
 * ^  Green: Climbing (> +0.3 m/s)
 * -  Gray: Level flight
 * v  Orange: Descending (< -0.3 m/s)
 * vv Red: Strong descent (< -3.0 m/s)
 */

interface ClimbIndicatorProps {
  verticalSpeedMs: number
  className?: string
}

export default function ClimbIndicator({ verticalSpeedMs, className = '' }: ClimbIndicatorProps) {
  const vs = verticalSpeedMs

  if (vs > 2.0) {
    return <span className={`text-green-400 font-bold ${className}`} title={`${vs.toFixed(1)} m/s`}>{'\u25B2\u25B2'}</span>
  }
  if (vs > 0.3) {
    return <span className={`text-green-400 ${className}`} title={`+${vs.toFixed(1)} m/s`}>{'\u25B2'}</span>
  }
  if (vs < -3.0) {
    return <span className={`text-red-500 font-bold ${className}`} title={`${vs.toFixed(1)} m/s`}>{'\u25BC\u25BC'}</span>
  }
  if (vs < -0.3) {
    return <span className={`text-orange-400 ${className}`} title={`${vs.toFixed(1)} m/s`}>{'\u25BC'}</span>
  }
  return <span className={`text-gray-500 ${className}`} title={`${vs.toFixed(1)} m/s`}>{'\u2014'}</span>
}
