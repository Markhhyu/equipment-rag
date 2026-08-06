import { reactive } from 'vue'
import { apiFetch } from './api'
import { getApiKey } from './storage'

export type AppRole = 'admin' | 'query' | 'import' | 'workflow'

export interface CurrentPrincipal {
  key_id: string
  tenant_id: string
  roles: AppRole[]
  authenticated: boolean
}

export const authState = reactive<{
  principal: CurrentPrincipal | null
  loading: boolean
  error: string
}>({
  principal: null,
  loading: true,
  error: '',
})

let refreshSequence = 0

export function hasAppRole(role: AppRole, principal = authState.principal): boolean {
  return Boolean(principal && (principal.roles.includes('admin') || principal.roles.includes(role)))
}

export async function refreshCurrentPrincipal(): Promise<CurrentPrincipal | null> {
  const sequence = ++refreshSequence
  authState.loading = true
  authState.error = ''
  try {
    const response = await apiFetch('/auth/me', getApiKey())
    const principal = await response.json() as CurrentPrincipal
    if (sequence === refreshSequence) authState.principal = principal
    return principal
  } catch (error) {
    if (sequence === refreshSequence) {
      authState.principal = null
      authState.error = error instanceof Error ? error.message : String(error)
    }
    return null
  } finally {
    if (sequence === refreshSequence) authState.loading = false
  }
}
