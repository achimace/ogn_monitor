/**
 * Dashboard overview page.
 */
import { useAuth } from '../hooks/useAuth'

export default function DashboardPage() {
  const { user } = useAuth()

  return (
    <div className="p-6">
      <h2 className="text-xl font-bold text-white mb-6">Dashboard</h2>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
        <DashCard title="Flugplatz" value={user?.tenantName || '-'} sub="Konfiguration" />
        <DashCard title="Status" value="Aktiv" sub="OGN verbunden" color="text-green-400" />
        <DashCard title="Rolle" value={user?.role || '-'} sub={user?.email || ''} />
      </div>

      <div className="bg-tower-surface border border-tower-border rounded-xl p-6">
        <h3 className="text-lg font-semibold text-white mb-3">Schnellstart</h3>
        <div className="space-y-3 text-sm text-gray-400">
          <p>1. Flugplatz-Koordinaten unter <strong className="text-gray-200">Flugplatz</strong> konfigurieren</p>
          <p>2. Vereinsflugzeuge unter <strong className="text-gray-200">Flugzeuge</strong> eintragen</p>
          <p>3. <strong className="text-gray-200">Tower-Monitor</strong> im Seitenbalken oeffnen</p>
        </div>
      </div>
    </div>
  )
}

function DashCard({ title, value, sub, color }: { title: string; value: string; sub: string; color?: string }) {
  return (
    <div className="bg-tower-surface border border-tower-border rounded-xl p-5">
      <div className="text-xs text-gray-500 uppercase tracking-wider mb-1">{title}</div>
      <div className={`text-xl font-bold ${color || 'text-white'}`}>{value}</div>
      <div className="text-xs text-gray-500 mt-1">{sub}</div>
    </div>
  )
}
