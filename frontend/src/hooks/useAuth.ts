/**
 * Auth hook - wraps Zustand auth store for component use.
 */
import { useEffect } from 'react'
import { useAuthStore } from '../store/authStore'

export function useAuth() {
  const store = useAuthStore()

  // Load user on mount if token exists but user not loaded
  useEffect(() => {
    if (store.token && !store.user) {
      store.loadUser()
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  return {
    user: store.user,
    token: store.token,
    isAuthenticated: !!store.token,
    loading: store.loading,
    error: store.error,
    login: store.login,
    register: store.register,
    logout: store.logout,
    clearError: store.clearError,
  }
}
