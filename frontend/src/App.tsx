import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import LoginPage from './pages/LoginPage'
import RegisterPage from './pages/RegisterPage'
import DashboardPage from './pages/DashboardPage'
import AirfieldConfigPage from './pages/AirfieldConfigPage'
import AircraftManagePage from './pages/AircraftManagePage'
import ExclusionListPage from './pages/ExclusionListPage'
import FlightLogPage from './pages/FlightLogPage'
import VfSyncPage from './pages/VfSyncPage'
import MonitorPage from './pages/MonitorPage'
import MonitorErrorBoundary from './components/MonitorErrorBoundary'
import Layout from './components/Layout'
import ProtectedRoute from './components/ProtectedRoute'

function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-tower-bg">
        <Routes>
          {/* Public routes */}
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />
          <Route
            path="/monitor/:slug"
            element={<MonitorErrorBoundary><MonitorPage /></MonitorErrorBoundary>}
          />

          {/* Protected routes with dashboard layout */}
          <Route element={<ProtectedRoute />}>
            <Route element={<Layout />}>
              <Route path="/dashboard" element={<DashboardPage />} />
              <Route path="/dashboard/airfield" element={<AirfieldConfigPage />} />
              <Route path="/dashboard/aircraft" element={<AircraftManagePage />} />
              <Route path="/dashboard/exclusion" element={<ExclusionListPage />} />
              <Route path="/dashboard/log" element={<FlightLogPage />} />
              <Route path="/dashboard/vfsync" element={<VfSyncPage />} />
            </Route>
          </Route>

          {/* Default redirect */}
          <Route path="/" element={<Navigate to="/login" replace />} />
          <Route path="*" element={<Navigate to="/login" replace />} />
        </Routes>
      </div>
    </BrowserRouter>
  )
}

export default App
