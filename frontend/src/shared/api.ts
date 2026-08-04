export interface SseMessage<T = Record<string, unknown>> {
  event: string
  data: T
}

function requestHeaders(apiKey: string, json = false): HeadersInit {
  const headers: Record<string, string> = {}
  if (apiKey) headers['X-API-Key'] = apiKey
  if (json) headers['Content-Type'] = 'application/json'
  return headers
}

export async function apiFetch(
  path: string,
  apiKey: string,
  init: RequestInit = {},
  json = false,
): Promise<Response> {
  const headers = new Headers(init.headers)
  for (const [key, value] of Object.entries(requestHeaders(apiKey, json))) headers.set(key, value)
  const response = await fetch(path, { ...init, headers })
  if (!response.ok) throw new Error(await readApiError(response))
  return response
}

export async function readApiError(response: Response): Promise<string> {
  try {
    const payload = await response.json() as { detail?: string; error?: string; message?: string }
    return payload.detail || payload.error || payload.message || `请求失败（${response.status}）`
  } catch {
    return `请求失败（${response.status} ${response.statusText}）`
  }
}

function parseSseFrame(frame: string): SseMessage | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const rawLine of frame.split(/\r?\n/)) {
    if (rawLine.startsWith('event:')) event = rawLine.slice(6).trim()
    if (rawLine.startsWith('data:')) dataLines.push(rawLine.slice(5).trimStart())
  }
  if (!dataLines.length) return null
  const rawData = dataLines.join('\n')
  try {
    return { event, data: JSON.parse(rawData) as Record<string, unknown> }
  } catch {
    return { event, data: { value: rawData } }
  }
}

export async function consumeSse(
  path: string,
  apiKey: string,
  onMessage: (message: SseMessage) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await apiFetch(path, apiKey, { signal })
  if (!response.body) throw new Error('浏览器未提供流式响应能力')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const frames = buffer.split(/\r?\n\r?\n/)
    buffer = frames.pop() ?? ''
    for (const frame of frames) {
      const parsed = parseSseFrame(frame)
      if (parsed) onMessage(parsed)
    }
    if (done) break
  }
  if (buffer.trim()) {
    const parsed = parseSseFrame(buffer)
    if (parsed) onMessage(parsed)
  }
}
