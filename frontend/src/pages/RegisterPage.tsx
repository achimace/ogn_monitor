import { useState, type FormEvent } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'
import { useAuthStore } from '../store/authStore'

const DISCLAIMER_TEXT = `WICHTIGER HINWEIS:

FlightMonitor ist ein REINES ASSISTENZSYSTEM zur Unterstuetzung
des Flugbetriebs. Es ersetzt NICHT:

- Flugleiter-Pflichten gemaess NfL II-81/13
- Eigenstaendige Luftraumbeobachtung
- Vorgeschriebene Kommunikationsverfahren
- Eigenverantwortung der Luftfahrzeugfuehrer

SYSTEMEINSCHRAENKUNGEN:

- Die Notfallerkennung basiert auf Flugprofilanalyse
  und ist NICHT fehlerfrei. Tote Zonen, OGN-Empfangsluecken
  und fehlerhafte FLARM-Daten koennen zu Fehlalarmen oder
  ausbleibenden Warnungen fuehren.

- Ein AUSBLEIBENDER Alarm bedeutet NICHT, dass kein
  Notfall vorliegt.

- Startart-Erkennung und F-Schlepp-Hoehenabrechnungen sind
  Schaetzungen auf Basis von GPS-Daten und koennen von der
  tatsaechlichen Hoehe abweichen.

Der Betreiber uebernimmt keine Haftung fuer:
- Nicht erkannte Notfaelle (False Negatives)
- Fehlalarme (False Positives)
- Unvollstaendige oder fehlerhafte Flugdaten
- Ausfaelle des Systems oder der OGN-Infrastruktur`

export default function RegisterPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [passwordConfirm, setPasswordConfirm] = useState('')
  const [tenantName, setTenantName] = useState('')
  const [disclaimerAccepted, setDisclaimerAccepted] = useState(false)
  const [validationError, setValidationError] = useState('')
  const { register, loading, error, clearError } = useAuth()
  const navigate = useNavigate()

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setValidationError('')

    if (password !== passwordConfirm) {
      setValidationError('Passwoerter stimmen nicht ueberein')
      return
    }
    if (password.length < 8) {
      setValidationError('Passwort muss mindestens 8 Zeichen lang sein')
      return
    }
    if (!disclaimerAccepted) {
      setValidationError('Bitte akzeptieren Sie die Nutzungsbedingungen')
      return
    }

    await register(email, password, tenantName, disclaimerAccepted)
    // Check store directly (not via hook) after async register
    if (useAuthStore.getState().token) {
      navigate('/dashboard')
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4 py-8">
      <div className="bg-tower-surface border border-tower-border rounded-xl p-8 w-full max-w-lg">
        <h1 className="text-2xl font-bold text-white mb-2">OGN FlightMonitor</h1>
        <p className="text-gray-400 mb-6">Neuen Flugplatz registrieren</p>

        {(error || validationError) && (
          <div className="bg-red-900/50 border border-red-500 text-red-200 rounded-lg p-3 mb-4 text-sm">
            {validationError || error}
            <button onClick={() => { clearError(); setValidationError('') }} className="float-right text-red-400 hover:text-red-200">&times;</button>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-400 mb-1">Flugplatz-Name</label>
            <input
              type="text"
              value={tenantName}
              onChange={(e) => setTenantName(e.target.value)}
              required
              className="w-full bg-tower-bg border border-tower-border rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-tower-qdr"
              placeholder="Segelflugverein Ohlstadt"
            />
          </div>

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
              minLength={8}
              className="w-full bg-tower-bg border border-tower-border rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-tower-qdr"
            />
          </div>

          <div>
            <label className="block text-sm text-gray-400 mb-1">Passwort bestaetigen</label>
            <input
              type="password"
              value={passwordConfirm}
              onChange={(e) => setPasswordConfirm(e.target.value)}
              required
              className="w-full bg-tower-bg border border-tower-border rounded-lg px-4 py-2.5 text-white focus:outline-none focus:border-tower-qdr"
            />
          </div>

          {/* Disclaimer */}
          <div className="border border-tower-border rounded-lg p-4 mt-4">
            <h3 className="text-sm font-semibold text-yellow-400 mb-2">
              NUTZUNGSBEDINGUNGEN ALARMSYSTEM
            </h3>
            <pre className="text-xs text-gray-400 whitespace-pre-wrap max-h-48 overflow-y-auto mb-3 font-sans leading-relaxed">
              {DISCLAIMER_TEXT}
            </pre>
            <label className="flex items-start gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={disclaimerAccepted}
                onChange={(e) => setDisclaimerAccepted(e.target.checked)}
                className="mt-1 w-4 h-4 accent-tower-qdr"
              />
              <span className="text-sm text-gray-300">
                Ich habe die Nutzungsbedingungen gelesen und akzeptiere sie.
                Ich verstehe, dass FlightMonitor ein Assistenzsystem ist und
                die Pflichten des Flugleiters nicht ersetzt.
              </span>
            </label>
          </div>

          <button
            type="submit"
            disabled={loading || !disclaimerAccepted}
            className="w-full bg-tower-qdr hover:bg-cyan-500 text-white font-semibold rounded-lg px-4 py-2.5 transition-colors disabled:opacity-50"
          >
            {loading ? 'Wird registriert...' : 'Registrieren'}
          </button>
        </form>

        <p className="text-gray-500 text-sm mt-6 text-center">
          Bereits registriert?{' '}
          <Link to="/login" className="text-tower-qdr hover:underline">
            Anmelden
          </Link>
        </p>
      </div>
    </div>
  )
}
