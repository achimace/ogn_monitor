/**
 * Dashboard layout with sidebar navigation.
 */
import { Link, useLocation, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'

const NAV_ITEMS = [
  { path: '/dashboard', label: 'Dashboard', icon: '\u25A3' },
  { path: '/dashboard/airfield', label: 'Flugplatz', icon: '\u2708' },
  { path: '/dashboard/aircraft', label: 'Flugzeuge', icon: '\u2693' },
  { path: '/dashboard/log', label: 'Flugbuch', icon: '\u2261' },
]

export default function Layout() {
  const { user, logout } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()

  function handleLogout() {
    logout()
    navigate('/login')
  }

  return (
    <div className="min-h-screen flex">
      {/* Sidebar */}
      <aside className="w-56 bg-tower-surface border-r border-tower-border flex flex-col">
        <div className="p-4 border-b border-tower-border">
          <h1 className="text-lg font-bold text-white">FlightMonitor</h1>
          <p className="text-xs text-gray-500 mt-1">{user?.tenantName || 'Laden...'}</p>
        </div>

        <nav className="flex-1 p-3 space-y-1">
          {NAV_ITEMS.map((item) => {
            const active = location.pathname === item.path
            return (
              <Link
                key={item.path}
                to={item.path}
                className={`flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors ${
                  active
                    ? 'bg-tower-qdr/20 text-tower-qdr'
                    : 'text-gray-400 hover:text-white hover:bg-white/5'
                }`}
              >
                <span className="text-base">{item.icon}</span>
                {item.label}
              </Link>
            )
          })}
        </nav>

        {/* Monitor link (public) */}
        <div className="p-3 border-t border-tower-border">
          {user && (
            <Link
              to={`/monitor/${user.tenantName?.toLowerCase().replace(/\s+/g, '-') || 'demo'}`}
              className="flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-green-400 hover:bg-green-500/10 transition-colors"
              target="_blank"
            >
              <span className="text-base">{'\u25C9'}</span>
              Tower-Monitor
            </Link>
          )}
        </div>

        {/* User / Logout */}
        <div className="p-3 border-t border-tower-border">
          <div className="text-xs text-gray-500 mb-2 px-3">{user?.email}</div>
          <button
            onClick={handleLogout}
            className="w-full text-left px-3 py-2 rounded-lg text-sm text-gray-400 hover:text-red-400 hover:bg-red-500/10 transition-colors"
          >
            Abmelden
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  )
}
