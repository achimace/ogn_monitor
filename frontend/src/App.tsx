import { BrowserRouter, Routes, Route } from 'react-router-dom'

// TODO Phase 4: Import pages
// import LoginPage from './pages/LoginPage'
// import RegisterPage from './pages/RegisterPage'
// import DashboardPage from './pages/DashboardPage'
// import MonitorPage from './pages/MonitorPage'

function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-tower-bg">
        <Routes>
          <Route path="/" element={<HomePage />} />
          {/* TODO Phase 4: Add routes */}
          {/* <Route path="/login" element={<LoginPage />} /> */}
          {/* <Route path="/register" element={<RegisterPage />} /> */}
          {/* <Route path="/dashboard" element={<DashboardPage />} /> */}
          {/* <Route path="/monitor/:slug" element={<MonitorPage />} /> */}
        </Routes>
      </div>
    </BrowserRouter>
  )
}

function HomePage() {
  return (
    <div className="flex items-center justify-center min-h-screen">
      <div className="text-center space-y-4">
        <h1 className="text-4xl font-bold text-white">
          OGN FlightMonitor
        </h1>
        <p className="text-gray-400 text-lg">
          Echtzeit-Flugmonitoring fuer Segelflugplaetze
        </p>
        <div className="flex gap-4 justify-center mt-8">
          <div className="bg-tower-surface border border-tower-border rounded-lg p-6 w-48">
            <div className="text-tower-qdr text-tower-2xl font-mono font-bold">195&deg;</div>
            <div className="text-gray-500 text-sm mt-1">QDR SSW</div>
          </div>
          <div className="bg-tower-surface border border-tower-border rounded-lg p-6 w-48">
            <div className="text-tower-altitude text-tower-2xl font-mono font-bold">2.100m</div>
            <div className="text-gray-500 text-sm mt-1">Hoehe MSL</div>
          </div>
          <div className="bg-tower-surface border border-tower-border rounded-lg p-6 w-48">
            <div className="text-tower-distance text-tower-2xl font-mono font-bold">32,5km</div>
            <div className="text-gray-500 text-sm mt-1">Distanz</div>
          </div>
        </div>
        <p className="text-gray-600 text-xs mt-8">
          Assistenzsystem - ersetzt nicht die Pflichten des Flugleiters
        </p>
      </div>
    </div>
  )
}

export default App
