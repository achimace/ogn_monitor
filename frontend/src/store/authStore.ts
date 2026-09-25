/**
 * Zustand auth store - JWT token management.
 */
import { create } from 'zustand'
import { api, ApiError } from '../api/client'

interface User {
  id: number
  email: string
  tenantId: number
  tenantName: string
  role: string
}

interface AuthState {
  token: string | null
  user: User | null
  loading: boolean
  error: string | null

  /** Resolves to true only if THIS call obtained a token. */
  login: (email: string, password: string) => Promise<boolean>
  /** Resolves to true only if THIS call obtained a token. */
  register: (email: string, password: string, tenantName: string, disclaimerAccepted: boolean) => Promise<boolean>
  logout: () => void
  loadUser: () => Promise<void>
  clearError: () => void
}

export const useAuthStore = create<AuthState>((set) => ({
  token: localStorage.getItem('token'),
  user: null,
  loading: false,
  error: null,

  login: async (email, password) => {
    set({ loading: true, error: null })
    try {
      const data = await api.post<{ access_token: string }>('/auth/login', { email, password })
      localStorage.setItem('token', data.access_token)
      set({ token: data.access_token, loading: false })
      // Load user profile after login
      useAuthStore.getState().loadUser()
      return true
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'Login fehlgeschlagen'
      set({ loading: false, error: msg })
      return false
    }
  },

  register: async (email, password, tenantName, _disclaimerAccepted) => {
    set({ loading: true, error: null })
    try {
      // Generate slug from tenant name
      const slug = tenantName.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
      const data = await api.post<{ access_token: string }>('/auth/register', {
        email,
        password,
        name: tenantName,
        slug,
      })
      // Registration returns a token directly
      localStorage.setItem('token', data.access_token)
      set({ token: data.access_token, loading: false })
      useAuthStore.getState().loadUser()
      return true
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'Registrierung fehlgeschlagen'
      set({ loading: false, error: msg })
      return false
    }
  },

  logout: () => {
    localStorage.removeItem('token')
    set({ token: null, user: null })
  },

  loadUser: async () => {
    try {
      const user = await api.get<User>('/auth/me')
      set({ user })
    } catch (e) {
      // Only a rejected token (401/403) means logout. Aborted fetches
      // (page reload), network errors or 5xx must keep the session.
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
        localStorage.removeItem('token')
        set({ token: null, user: null })
        return
      }
      console.warn('loadUser: profile fetch failed, keeping session', e)
    }
  },

  clearError: () => set({ error: null }),
}))
