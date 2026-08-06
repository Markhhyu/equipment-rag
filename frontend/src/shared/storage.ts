const API_KEY_STORAGE_KEY = 'equipment-rag-agent.api-key'
const SESSION_STORAGE_KEY = 'equipment-rag-agent.chat-session-id'
export const API_KEY_CHANGED_EVENT = 'equipment-rag-agent:api-key-changed'
const LEGACY_SERVICE_PORTS = new Set(['8000', '8001', '8002'])

export function getApiKey(): string {
  return localStorage.getItem(API_KEY_STORAGE_KEY) ?? ''
}

export function saveApiKey(value: string): void {
  const normalized = value.trim()
  if (normalized) localStorage.setItem(API_KEY_STORAGE_KEY, normalized)
  else localStorage.removeItem(API_KEY_STORAGE_KEY)
  window.dispatchEvent(new Event(API_KEY_CHANGED_EVENT))
}

export function createSessionId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `session-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export function getOrCreateSessionId(): string {
  const stored = localStorage.getItem(SESSION_STORAGE_KEY)
  if (stored) return stored
  const sessionId = createSessionId()
  localStorage.setItem(SESSION_STORAGE_KEY, sessionId)
  return sessionId
}

export function replaceSessionId(): string {
  const sessionId = createSessionId()
  localStorage.setItem(SESSION_STORAGE_KEY, sessionId)
  return sessionId
}

export function siblingServiceUrl(targetPort: string, page: string): string {
  const url = new URL(window.location.href)
  if (['localhost', '127.0.0.1'].includes(url.hostname) && ['8000', '8001', '8002'].includes(url.port)) {
    url.port = targetPort
  }
  url.pathname = page
  url.search = ''
  url.hash = ''
  return url.toString()
}

export function applicationPageUrl(routePath: string, targetPort: string, legacyPage: string): string {
  const url = new URL(window.location.href)
  if (LEGACY_SERVICE_PORTS.has(url.port)) {
    return siblingServiceUrl(targetPort, legacyPage)
  }
  url.pathname = routePath
  url.search = ''
  url.hash = ''
  return url.toString()
}
