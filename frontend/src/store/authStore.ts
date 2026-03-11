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

  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string, tenantName: string, disclaimerAccepted: boolean) => Promise<void>
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
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'Login fehlgeschlagen'
      set({ loading: false, error: msg })
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
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'Registrierung fehlgeschlagen'
      set({ loading: false, error: msg })
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
    } catch {
      // Token invalid - logout
      localStorage.removeItem('token')
      set({ token: null, user: null })
    }
  },

  clearError: () => set({ error: null }),
}))
