import { reactive } from 'vue'
import { apiFetch } from './api'
import { getApiKey } from './storage'

export type AppRole = 'admin' | 'query' | 'import' | 'workflow'

export interface CurrentPrincipal {
  key_id: string
  tenant_id: string
  roles: AppRole[]
  authenticated: boolean
  email: string
  auth_type: 'development' | 'api_key' | 'password'
}

export interface AuthConfig {
  auth_mode: 'disabled' | 'api_key' | 'password'
  password_login_enabled: boolean
  registration_enabled: boolean
  email_verification_required: boolean
}

export interface PendingEmailVerification {
  verification_required: true
  email: string
  expires_in: number
}

export interface AuthenticatedRegistration extends CurrentPrincipal {
  verification_required: false
}

export type RegistrationResult = AuthenticatedRegistration | PendingEmailVerification

export const authState = reactive<{
  principal: CurrentPrincipal | null
  loading: boolean
  error: string
  config: AuthConfig | null
}>({
  principal: null,
  loading: true,
  error: '',
  config: null,
})

let refreshSequence = 0

export function hasAppRole(role: AppRole, principal = authState.principal): boolean {
  return Boolean(principal && (principal.roles.includes('admin') || principal.roles.includes(role)))
}

export async function loadAuthConfig(): Promise<AuthConfig> {
  const response = await apiFetch('/auth/config', '')
  const config = await response.json() as AuthConfig
  authState.config = config
  return config
}

export async function loginWithPassword(email: string, password: string): Promise<CurrentPrincipal> {
  const response = await apiFetch('/auth/login', '', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  }, true)
  const principal = await response.json() as CurrentPrincipal
  authState.principal = principal
  authState.error = ''
  return principal
}

export async function registerWithPassword(email: string, password: string): Promise<RegistrationResult> {
  const response = await apiFetch('/auth/register', '', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  }, true)
  const result = await response.json() as RegistrationResult
  if (!result.verification_required) authState.principal = result
  authState.error = ''
  return result
}

export async function verifyEmail(token: string): Promise<CurrentPrincipal> {
  const response = await apiFetch('/auth/verify-email', '', {
    method: 'POST',
    body: JSON.stringify({ token }),
  }, true)
  const principal = await response.json() as CurrentPrincipal
  authState.principal = principal
  authState.error = ''
  return principal
}

export async function resendVerificationEmail(email: string): Promise<void> {
  await apiFetch('/auth/resend-verification', '', {
    method: 'POST',
    body: JSON.stringify({ email }),
  }, true)
}

export async function logoutCurrentPrincipal(): Promise<void> {
  await apiFetch('/auth/logout', '', { method: 'POST' })
  authState.principal = null
}

export async function refreshCurrentPrincipal(): Promise<CurrentPrincipal | null> {
  const sequence = ++refreshSequence
  authState.loading = true
  authState.error = ''
  try {
    if (!authState.config) await loadAuthConfig()
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
