import { useState, type FormEvent } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'
import { useAuthStore } from '../store/authStore'

export default function LoginPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const { login, loading, error, clearError } = useAuth()
  const navigate = useNavigate()

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    await login(email, password)
    // Check store directly (not via hook) after async login
    if (useAuthStore.getState().token) {
      navigate('/dashboard')
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="bg-tower-surface border border-tower-border rounded-xl p-8 w-full max-w-md">
        <h1 className="text-2xl font-bold text-white mb-2">OGN FlightMonitor</h1>
        <p className="text-gray-400 mb-6">Anmelden</p>

        {error && (
          <div className="bg-red-900/50 border border-red-500 text-red-200 rounded-lg p-3 mb-4 text-sm">
            {error}
            <button onClick={clearError} className="float-right text-red-400 hover:text-red-200">&times;</button>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-400 mb-1">E-Mail</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              className="w-full bg-tower-bg border border-tower-border rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-tower-qdr"
              placeholder="admin@flugplatz.de"
            />
          </div>

          <div>
            <label className="block text-sm text-gray-400 mb-1">Passwort</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              className="w-full bg-tower-bg border border-tower-border rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-tower-qdr"
            />
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full bg-tower-qdr hover:bg-cyan-500 text-white font-semibold rounded-lg px-4 py-2.5 transition-colors disabled:opacity-50"
          >
            {loading ? 'Wird angemeldet...' : 'Anmelden'}
          </button>
        </form>

        <p className="text-gray-500 text-sm mt-6 text-center">
          Noch kein Konto?{' '}
          <Link to="/register" className="text-tower-qdr hover:underline">
            Registrieren
          </Link>
        </p>
      </div>
    </div>
  )
}
