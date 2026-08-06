<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  ChatDotRound,
  Check,
  CircleClose,
  CircleCheck,
  Close,
  Connection,
  DataAnalysis,
  Delete,
  DocumentAdd,
  Files,
  FolderOpened,
  Grid,
  Loading,
  MoreFilled,
  Picture,
  Plus,
  Promotion,
  Refresh,
  Setting,
  Tickets,
  UploadFilled,
  Warning,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import ApiKeyDialog from '../shared/ApiKeyDialog.vue'
import { apiFetch, consumeSse, type SseMessage } from '../shared/api'
import { hasAppRole } from '../shared/auth'
import { formatBytes, formatNodeName, formatTime } from '../shared/format'
import {
  getApiKey,
  getOrCreateSessionId,
  applicationPageUrl,
  replaceSessionId,
  saveApiKey,
  siblingServiceUrl,
} from '../shared/storage'

type Role = 'user' | 'assistant'
type MessageStatus = 'ready' | 'streaming' | 'error'
type ResolutionStatus = 'solved' | 'partial' | 'unsolved'

interface VersionChoice {
  scope_id: string
  label: string
  device_model?: string
  equipment_version?: string
  software_version?: string
  firmware_version?: string
  hardware_revision?: string
  site_id?: string
  asset_ids?: string
  item_names?: string[]
}

interface VersionScopeGroup {
  document_id: string
  options: string[]
  choices: VersionChoice[]
}

interface ChatMessage {
  id: string
  role: Role
  text: string
  imageUrls: string[]
  imageRefs?: string[]
  traceId?: string
  feedback?: 0 | 1 | null
  resolutionStatus?: ResolutionStatus | null
  resolutionSubmitting?: boolean
  time?: number | string
  status: MessageStatus
  doneList?: string[]
  runningList?: string[]
  sources?: AnswerSource[]
  requiresHumanReview?: boolean
  reviewReason?: string
  versionScopeOptions?: VersionScopeGroup[]
  selectedVersionContext?: VersionChoice[]
  workflowCaseId?: string
  workflowSubmitting?: boolean
}

interface AnswerSource {
  index: number
  source: string
  chunk_id: string
  document_id: string
  revision_id: string
  version_label: string
  title: string
  section: string
  part?: number | string | null
  page_numbers: number[]
  url: string
  snippet: string
  score?: number | null
  device_model: string
  equipment_version: string
  software_version: string
  firmware_version: string
  hardware_revision: string
  site_id: string
  trust_level: string
  trust_label: string
  authoritative: boolean
}

interface PendingImage {
  id: string
  file: File
  previewUrl: string
}

interface AttachmentConfig {
  max_files: number
  max_bytes: number
  allowed_extensions: string[]
  allowed_content_types: string[]
}

interface AttachmentUploadResponse {
  attachments: Array<{ object_ref: string; preview_url: string }>
}

marked.setOptions({
  breaks: true,
  gfm: true,
})

const markdownTags = [
  'p', 'br', 'strong', 'em', 'del', 'blockquote', 'ul', 'ol', 'li',
  'h1', 'h2', 'h3', 'h4', 'code', 'pre', 'a', 'hr', 'table', 'thead',
  'tbody', 'tr', 'th', 'td',
]

function renderAssistantMarkdown(value: string): string {
  const html = marked.parse(String(value || ''), { async: false }) as string
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: markdownTags,
    ALLOWED_ATTR: ['href', 'title'],
  })
}

interface HistoryResponse {
  items: Array<{
    _id: string
    role: Role
    text: string
    image_urls?: string[]
    image_refs?: string[]
    item_names?: string[]
    trace_id?: string
    feedback_value?: 0 | 1 | null
    resolution_status?: ResolutionStatus | null
    sources?: AnswerSource[]
    requires_human_review?: boolean
    review_reason?: string
    version_scope_options?: VersionScopeGroup[]
    selected_version_context?: VersionChoice[]
    ts?: number | string
  }>
}

const defaultAttachmentConfig: AttachmentConfig = {
  max_files: 3,
  max_bytes: 10 * 1024 * 1024,
  allowed_extensions: ['.jpg', '.jpeg', '.png', '.webp'],
  allowed_content_types: ['image/jpeg', 'image/png', 'image/webp'],
}

const apiKey = ref(getApiKey())
const settingsVisible = ref(false)
const sessionId = ref(getOrCreateSessionId())
const messages = ref<ChatMessage[]>([])
const pendingImages = ref<PendingImage[]>([])
const question = ref('')
const sending = ref(false)
const loadingHistory = ref(false)
const dragActive = ref(false)
const attachmentConfig = ref(defaultAttachmentConfig)
const messageList = ref<HTMLElement | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
let activeStream: AbortController | null = null

const importUrl = applicationPageUrl('/import', '8000', '/import.html')
const knowledgeUrl = applicationPageUrl('/knowledge', '8000', '/knowledge.html')
const analyticsUrl = applicationPageUrl('/analytics', '8001', '/analytics.html')
const appsUrl = applicationPageUrl('/apps', '8001', '/apps.html')
const workflowCasesUrl = siblingServiceUrl('8002', '/workflow/cases')
const workflowPageUrl = applicationPageUrl('/workflow', '8002', '/workflow.html')
const shortSessionId = computed(() => sessionId.value.slice(0, 8))
const canSend = computed(() => !sending.value && (!!question.value.trim() || pendingImages.value.length > 0))
const attachmentHint = computed(() => `每轮最多 ${attachmentConfig.value.max_files} 张，单张不超过 ${formatBytes(attachmentConfig.value.max_bytes)}`)
const currentVersionContext = computed(() => {
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    const message = messages.value[index]
    if (message.versionScopeOptions?.length) return []
    if (message.selectedVersionContext?.length) return message.selectedVersionContext
  }
  return []
})

function authErrorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  if (/missing api key|invalid api key|401/i.test(message)) settingsVisible.value = true
  return message
}

function uniqueId(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function previousUserMessage(message: ChatMessage): ChatMessage | undefined {
  const messageIndex = messages.value.indexOf(message)
  for (let index = messageIndex - 1; index >= 0; index -= 1) {
    if (messages.value[index].role === 'user') return messages.value[index]
  }
  return undefined
}

function workflowDetailUrl(caseId: string): string {
  const url = new URL(workflowPageUrl)
  url.searchParams.set('case_id', caseId)
  return url.toString()
}

function workflowSources(message: ChatMessage): Array<Record<string, unknown>> {
  return (message.sources ?? []).map((source) => ({
    chunk_id: source.chunk_id,
    document_id: source.document_id,
    revision_id: source.revision_id,
    version_label: source.version_label,
    title: source.title,
    section: source.section,
    page_numbers: source.page_numbers,
    url: source.url,
    snippet: source.snippet,
    device_model: source.device_model,
    equipment_version: source.equipment_version,
    software_version: source.software_version,
    firmware_version: source.firmware_version,
    hardware_revision: source.hardware_revision,
    trust_level: source.trust_level,
  }))
}

async function openOrCreateWorkflowCase(message: ChatMessage): Promise<void> {
  if (!message.traceId || message.workflowSubmitting) return
  if (message.workflowCaseId) {
    window.location.href = workflowDetailUrl(message.workflowCaseId)
    return
  }

  message.workflowSubmitting = true
  try {
    const userMessage = previousUserMessage(message)
    const deviceModels = Array.from(new Set([
      ...(message.selectedVersionContext ?? []).map((item) => item.device_model || ''),
      ...(message.sources ?? []).map((item) => item.device_model || ''),
    ].filter(Boolean)))
    const versionLabels = Array.from(new Set(
      (message.selectedVersionContext ?? []).map((item) => item.label).filter(Boolean),
    ))
    const response = await apiFetch(workflowCasesUrl, apiKey.value, {
      method: 'POST',
      body: JSON.stringify({
        case_type: 'equipment_issue',
        subject: {
          question: userMessage?.text || '现场图片问题',
          trace_id: message.traceId,
          session_id: sessionId.value,
          device_models: deviceModels,
          version_labels: versionLabels,
        },
        context: {
          answer: message.text,
          requires_human_review: Boolean(message.requiresHumanReview),
          review_reason: message.reviewReason || '',
          resolution_status: message.resolutionStatus || 'unsolved',
          selected_version_context: message.selectedVersionContext ?? [],
          sources: workflowSources(message),
          image_refs: userMessage?.imageRefs ?? [],
        },
        idempotency_key: `qa-${message.traceId}`,
      }),
    }, true)
    const workflowCase = await response.json() as { case_id: string }
    message.workflowCaseId = workflowCase.case_id
    window.location.href = workflowDetailUrl(workflowCase.case_id)
  } catch (error) {
    ElMessage.error(`人工处理发起失败：${authErrorMessage(error)}`)
  } finally {
    message.workflowSubmitting = false
  }
}

function sanitizeAssistantText(text: string): string {
  return text
    .replace(/(?:\n|^)\s*【图片】\s*(?:\n\s*(?:<https?:\/\/[^>]+>|https?:\/\/\S+)\s*)+/gi, '\n')
    .replace(/<?https?:\/\/(?:www\.)?example\.com\/[^\s>]*>?/gi, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

async function scrollToBottom(): Promise<void> {
  await nextTick()
  if (messageList.value) messageList.value.scrollTop = messageList.value.scrollHeight
}

async function loadAttachmentConfig(): Promise<void> {
  try {
    const response = await apiFetch('/attachments/config', apiKey.value)
    attachmentConfig.value = await response.json() as AttachmentConfig
  } catch (error) {
    authErrorMessage(error)
  }
}

async function loadHistory(): Promise<void> {
  loadingHistory.value = true
  try {
    const response = await apiFetch(`/history/${encodeURIComponent(sessionId.value)}?limit=100`, apiKey.value)
    const payload = await response.json() as HistoryResponse
    messages.value = payload.items.map((item) => ({
      id: item._id || uniqueId(item.role),
      role: item.role,
      text: item.role === 'assistant' ? sanitizeAssistantText(item.text) : item.text,
      imageUrls: item.image_urls ?? [],
      imageRefs: item.image_refs ?? [],
      traceId: item.trace_id,
      feedback: item.feedback_value,
      resolutionStatus: item.resolution_status,
      sources: item.sources ?? [],
      requiresHumanReview: item.requires_human_review ?? false,
      reviewReason: item.review_reason ?? '',
      versionScopeOptions: item.version_scope_options ?? [],
      selectedVersionContext: item.selected_version_context ?? [],
      time: item.ts,
      status: 'ready',
    }))
    await scrollToBottom()
  } catch (error) {
    const message = authErrorMessage(error)
    if (!/missing api key|invalid api key/i.test(message)) ElMessage.error(`历史记录加载失败：${message}`)
  } finally {
    loadingHistory.value = false
  }
}

function openFilePicker(): void {
  if (!sending.value) fileInput.value?.click()
}

function selectImages(fileList: FileList | File[]): void {
  const selected = Array.from(fileList)
  const remaining = attachmentConfig.value.max_files - pendingImages.value.length
  if (remaining <= 0) {
    ElMessage.warning(`每轮最多选择 ${attachmentConfig.value.max_files} 张图片`)
    return
  }

  for (const file of selected.slice(0, remaining)) {
    const extension = `.${file.name.split('.').pop()?.toLowerCase() ?? ''}`
    const validType = attachmentConfig.value.allowed_content_types.includes(file.type)
      || attachmentConfig.value.allowed_extensions.includes(extension)
    if (!validType) {
      ElMessage.warning(`${file.name} 不是支持的 JPG、PNG 或 WebP 图片`)
      continue
    }
    if (file.size > attachmentConfig.value.max_bytes) {
      ElMessage.warning(`${file.name} 超过 ${formatBytes(attachmentConfig.value.max_bytes)}`)
      continue
    }
    pendingImages.value.push({ id: uniqueId('image'), file, previewUrl: URL.createObjectURL(file) })
  }
  if (selected.length > remaining) ElMessage.warning(`已保留前 ${remaining} 张图片`)
  if (fileInput.value) fileInput.value.value = ''
}

function removePendingImage(id: string): void {
  const image = pendingImages.value.find((item) => item.id === id)
  if (image) URL.revokeObjectURL(image.previewUrl)
  pendingImages.value = pendingImages.value.filter((item) => item.id !== id)
}

function onDrop(event: DragEvent): void {
  dragActive.value = false
  if (event.dataTransfer?.files.length) selectImages(event.dataTransfer.files)
}

async function uploadImages(images: PendingImage[]): Promise<AttachmentUploadResponse['attachments']> {
  if (!images.length) return []
  const formData = new FormData()
  for (const image of images) formData.append('files', image.file)
  const response = await apiFetch(
    `/attachments/${encodeURIComponent(sessionId.value)}`,
    apiKey.value,
    { method: 'POST', body: formData },
  )
  const payload = await response.json() as AttachmentUploadResponse
  return payload.attachments
}

function applySseMessage(message: SseMessage, assistant: ChatMessage): void {
  const data = message.data as Record<string, unknown>
  if (message.event === 'delta') {
    assistant.text += String(data.delta ?? '')
  } else if (message.event === 'progress') {
    assistant.doneList = (data.done_list as string[] | undefined) ?? assistant.doneList
    assistant.runningList = (data.running_list as string[] | undefined) ?? assistant.runningList
  } else if (message.event === 'final') {
    assistant.text = sanitizeAssistantText(String(data.answer ?? assistant.text))
    assistant.imageUrls = (data.image_urls as string[] | undefined) ?? []
    assistant.traceId = String(data.trace_id ?? assistant.traceId ?? '')
    assistant.doneList = (data.done_list as string[] | undefined) ?? assistant.doneList
    assistant.runningList = (data.running_list as string[] | undefined) ?? []
    assistant.sources = (data.sources as AnswerSource[] | undefined) ?? []
    assistant.requiresHumanReview = Boolean(data.requires_human_review)
    assistant.reviewReason = String(data.review_reason ?? '')
    assistant.versionScopeOptions = (data.version_scope_options as VersionScopeGroup[] | undefined) ?? []
    assistant.selectedVersionContext = (data.selected_version_context as VersionChoice[] | undefined) ?? []
    assistant.status = 'ready'
  } else if (message.event === 'error') {
    assistant.status = 'error'
    assistant.text ||= `处理失败：${String(data.error ?? '未知错误')}`
  }
  void scrollToBottom()
}

async function sendMessage(versionChoice?: VersionChoice, resetVersionContext = false): Promise<void> {
  if (!canSend.value) return
  const text = question.value.trim()
  const selectedImages = [...pendingImages.value]
  const userMessage: ChatMessage = {
    id: uniqueId('user'), role: 'user', text, imageUrls: selectedImages.map((item) => item.previewUrl), imageRefs: [], status: 'ready', time: Date.now() / 1000,
  }
  const assistant: ChatMessage = {
    id: uniqueId('assistant'), role: 'assistant', text: '', imageUrls: [], status: 'streaming', doneList: [], runningList: [], sources: [],
  }
  messages.value.push(userMessage, assistant)
  question.value = ''
  pendingImages.value = []
  sending.value = true
  await scrollToBottom()

  try {
    const uploadedAttachments = await uploadImages(selectedImages)
    const imageRefs = uploadedAttachments.map((attachment) => attachment.object_ref)
    userMessage.imageRefs = imageRefs
    if (uploadedAttachments.length) {
      // 本地blob预览只用于上传阶段；上传后切换为MinIO短期签名地址，避免释放blob后消息图片失效。
      userMessage.imageUrls = uploadedAttachments.map((attachment) => attachment.preview_url)
    }
    const response = await apiFetch('/query', apiKey.value, {
      method: 'POST',
      body: JSON.stringify({
        query: text,
        session_id: sessionId.value,
        is_stream: true,
        image_refs: imageRefs,
        version_scope_id: versionChoice?.scope_id ?? '',
        reset_version_context: resetVersionContext,
      }),
    }, true)
    const submitted = await response.json() as { trace_id?: string }
    assistant.traceId = submitted.trace_id

    activeStream = new AbortController()
    await consumeSse(
      `/stream/${encodeURIComponent(sessionId.value)}`,
      apiKey.value,
      (message) => applySseMessage(message, assistant),
      activeStream.signal,
    )
    if (assistant.status === 'streaming') assistant.status = 'ready'
  } catch (error) {
    if ((error as Error)?.name !== 'AbortError') {
      assistant.status = 'error'
      assistant.text ||= `请求失败：${authErrorMessage(error)}`
      ElMessage.error(assistant.text)
    }
  } finally {
    sending.value = false
    activeStream = null
    for (const image of selectedImages) URL.revokeObjectURL(image.previewUrl)
    await scrollToBottom()
  }
}

async function selectVersion(choice: VersionChoice): Promise<void> {
  if (sending.value) return
  question.value = `使用版本：${choice.label}`
  await nextTick()
  await sendMessage(choice)
}

async function requestVersionSwitch(): Promise<void> {
  if (sending.value || !currentVersionContext.value.length) return
  const current = currentVersionContext.value[0]
  const device = current.device_model || current.item_names?.[0] || '当前设备'
  question.value = `请重新选择 ${device} 的适用版本`
  await nextTick()
  await sendMessage(undefined, true)
}

async function submitFeedback(message: ChatMessage, value: 0 | 1): Promise<void> {
  if (!message.traceId || message.status !== 'ready') return
  try {
    await apiFetch('/feedback', apiKey.value, {
      method: 'POST',
      body: JSON.stringify({ trace_id: message.traceId, value }),
    }, true)
    message.feedback = value
    ElMessage.success('感谢反馈，已用于后续效果分析')
  } catch (error) {
    ElMessage.error(`反馈提交失败：${authErrorMessage(error)}`)
  }
}

async function submitResolution(message: ChatMessage, status: ResolutionStatus): Promise<void> {
  if (!message.traceId || message.status !== 'ready' || message.resolutionSubmitting) return
  message.resolutionSubmitting = true
  try {
    await apiFetch('/resolution', apiKey.value, {
      method: 'POST',
      body: JSON.stringify({ trace_id: message.traceId, status }),
    }, true)
    message.resolutionStatus = status
    const labels: Record<ResolutionStatus, string> = {
      solved: '已记录：问题已解决',
      partial: '已记录：问题部分解决',
      unsolved: '已记录：问题尚未解决',
    }
    ElMessage.success(labels[status])
  } catch (error) {
    ElMessage.error(`解决结果提交失败：${authErrorMessage(error)}`)
  } finally {
    message.resolutionSubmitting = false
  }
}

async function clearCurrentSession(startNew = false): Promise<void> {
  const action = startNew ? '新建会话' : '清空会话'
  try {
    await ElMessageBox.confirm(
      '聊天记录和本会话上传的图片将一并删除，知识库内容不受影响。',
      action,
      { confirmButtonText: action, cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }

  activeStream?.abort()
  try {
    await apiFetch(`/history/${encodeURIComponent(sessionId.value)}`, apiKey.value, { method: 'DELETE' })
    messages.value = []
    for (const image of pendingImages.value) URL.revokeObjectURL(image.previewUrl)
    pendingImages.value = []
    question.value = ''
    if (startNew) sessionId.value = replaceSessionId()
    ElMessage.success(startNew ? '新会话已创建' : '当前会话已清空')
  } catch (error) {
    ElMessage.error(`${action}失败：${authErrorMessage(error)}`)
  }
}

async function saveSettings(value: string): Promise<void> {
  saveApiKey(value)
  apiKey.value = value
  await Promise.all([loadAttachmentConfig(), loadHistory()])
  ElMessage.success('连接设置已保存')
}

function onComposerKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void sendMessage()
  }
}

onMounted(async () => {
  await Promise.all([loadAttachmentConfig(), loadHistory()])
})

onBeforeUnmount(() => {
  activeStream?.abort()
  for (const image of pendingImages.value) URL.revokeObjectURL(image.previewUrl)
})
</script>

<template>
  <div class="app-frame chat-layout">
    <aside class="chat-sidebar">
      <div class="sidebar-brand">
        <div class="brand-mark">EA</div>
        <div class="brand-copy"><strong>设备知识助手</strong><span>Equipment Intelligence</span></div>
      </div>

      <button class="new-chat" :disabled="sending" @click="clearCurrentSession(true)">
        <el-icon><Plus /></el-icon><span>新建会话</span>
      </button>

      <div class="sidebar-section">
        <div class="sidebar-caption">当前会话</div>
        <div class="session-card active">
          <span class="session-icon"><el-icon><ChatDotRound /></el-icon></span>
          <div><strong>设备咨询</strong><span>{{ shortSessionId }}</span></div>
          <el-icon class="session-more"><MoreFilled /></el-icon>
        </div>
      </div>

      <div class="privacy-card">
        <span class="privacy-icon"><el-icon><Picture /></el-icon></span>
        <strong>图片仅用于当前会话</strong>
        <p>不会写入 Milvus、文档切片或知识库。清空会话后会同步删除。</p>
      </div>

      <div class="sidebar-footer">
        <a :href="appsUrl" class="sidebar-link"><el-icon><Grid /></el-icon>应用与组件</a>
        <a v-if="hasAppRole('import')" :href="importUrl" class="sidebar-link"><el-icon><DocumentAdd /></el-icon>知识库导入</a>
        <a v-if="hasAppRole('admin')" :href="knowledgeUrl" class="sidebar-link"><el-icon><Files /></el-icon>知识库治理</a>
        <a :href="analyticsUrl" class="sidebar-link"><el-icon><DataAnalysis /></el-icon>问答运营看板</a>
        <button class="sidebar-link" @click="settingsVisible = true"><el-icon><Setting /></el-icon>连接设置</button>
      </div>
    </aside>

    <main class="chat-main">
      <header class="chat-header">
        <div class="header-status">
          <strong>设备咨询</strong>
          <span><i class="online-dot" /> 服务已连接</span>
          <div v-if="currentVersionContext.length" class="version-lock">
            <span>{{ currentVersionContext.map((item) => item.label).join('；') }}</span>
            <button :disabled="sending" title="重新选择适用版本" @click="requestVersionSwitch"><el-icon><Refresh /></el-icon></button>
          </div>
        </div>
        <div class="header-actions">
          <a class="top-button" :href="appsUrl" title="打开应用与组件中心"><el-icon><Grid /></el-icon><span class="desktop-label">应用中心</span></a>
          <button class="top-button" @click="settingsVisible = true"><el-icon><Connection /></el-icon><span class="desktop-label">API 设置</span></button>
          <button class="top-button danger" :disabled="sending" @click="clearCurrentSession(false)"><el-icon><Delete /></el-icon><span class="desktop-label">清空会话</span></button>
        </div>
      </header>

      <section ref="messageList" class="message-list">
        <div v-if="loadingHistory" class="center-state"><el-icon class="is-loading"><Loading /></el-icon>正在读取历史会话</div>

        <div v-else-if="messages.length === 0" class="welcome-state">
          <div class="welcome-orbit"><span><el-icon><ChatDotRound /></el-icon></span></div>
          <div class="eyebrow">Equipment RAG Agent</div>
          <h1>今天想了解哪台设备？</h1>
          <p>可以询问操作步骤、故障排查和维护规范，也可以上传现场图片辅助判断。</p>
          <div class="suggestion-grid">
            <button @click="question = '请说明这台设备的标准开机步骤和安全注意事项。'">
              <span><el-icon><Promotion /></el-icon></span><div><strong>操作指导</strong><small>查询标准开机步骤</small></div>
            </button>
            <button @click="question = '设备出现异常报警时，应该如何逐步排查？'">
              <span><el-icon><Setting /></el-icon></span><div><strong>故障排查</strong><small>定位常见异常原因</small></div>
            </button>
          </div>
        </div>

        <template v-for="message in messages" :key="message.id">
          <article class="message-row" :class="message.role">
            <div v-if="message.role === 'assistant'" class="message-avatar assistant-avatar">EA</div>
            <div class="message-content">
              <div class="message-label">{{ message.role === 'assistant' ? '设备助手' : '你' }}</div>
              <div class="message-bubble" :class="{ error: message.status === 'error' }">
                <div v-if="message.imageUrls.length" class="message-images">
                  <a v-for="url in message.imageUrls" :key="url" :href="url" target="_blank" rel="noreferrer">
                    <img :src="url" alt="会话图片" />
                  </a>
                </div>
                <div
                  v-if="message.text && message.role === 'assistant'"
                  class="message-text markdown-content"
                  v-html="renderAssistantMarkdown(message.text)"
                />
                <div v-else-if="message.text" class="message-text">{{ message.text }}</div>
                <div v-if="message.status === 'streaming' && !message.text" class="thinking-line">
                  <i /><i /><i /><span>正在检索并组织回答</span>
                </div>
              </div>

              <div v-if="message.role === 'assistant' && message.requiresHumanReview" class="review-alert">
                <el-icon><CircleClose /></el-icon>
                <div><strong>需要人工复核</strong><span>{{ message.reviewReason }}</span></div>
              </div>

              <div v-if="message.role === 'assistant' && message.versionScopeOptions?.length" class="version-choice-panel">
                <strong>选择设备适用版本</strong>
                <div v-for="group in message.versionScopeOptions" :key="group.document_id" class="version-choice-list">
                  <button v-for="choice in group.choices" :key="choice.scope_id" :disabled="sending" @click="selectVersion(choice)">
                    {{ choice.label }}
                  </button>
                </div>
              </div>

              <details v-if="message.role === 'assistant' && ((message.doneList?.length ?? 0) + (message.runningList?.length ?? 0) > 0)" class="progress-panel">
                <summary>
                  <span><el-icon><Check /></el-icon>{{ message.doneList?.length ?? 0 }} 项已完成</span>
                  <span v-if="message.runningList?.length" class="running-copy"><el-icon class="is-loading"><Loading /></el-icon>处理中</span>
                </summary>
                <div class="progress-steps">
                  <div v-for="node in message.doneList" :key="`done-${node}`" class="progress-step done"><el-icon><Check /></el-icon>{{ formatNodeName(node) }}</div>
                  <div v-for="node in message.runningList" :key="`running-${node}`" class="progress-step running"><el-icon class="is-loading"><Loading /></el-icon>{{ formatNodeName(node) }}</div>
                </div>
              </details>

              <details v-if="message.role === 'assistant' && message.sources?.length" class="source-panel">
                <summary>
                  <span><el-icon><Files /></el-icon>回答依据</span>
                  <span>{{ message.sources.length }} 个知识片段</span>
                </summary>
                <div class="source-list">
                  <article v-for="source in message.sources" :key="`${message.id}-${source.index}-${source.chunk_id}`" class="source-card">
                    <div class="source-card-head">
                      <span class="source-index">资料 {{ source.index }}</span>
                      <span class="source-trust" :class="{ authoritative: source.authoritative }">{{ source.trust_label }}</span>
                      <span v-if="source.version_label" class="source-version">{{ source.version_label }}</span>
                      <span v-if="source.page_numbers?.length" class="source-pages">PDF 第 {{ source.page_numbers.join('、') }} 页</span>
                    </div>
                    <a v-if="source.url" :href="source.url" target="_blank" rel="noreferrer">{{ source.title }}</a>
                    <strong v-else>{{ source.title }}</strong>
                    <p v-if="source.section">{{ source.section }}<template v-if="source.part !== null && source.part !== undefined"> · 片段 {{ source.part }}</template></p>
                    <div class="source-scope" v-if="source.device_model || source.equipment_version || source.software_version || source.firmware_version || source.hardware_revision || source.site_id">
                      <span v-if="source.device_model">型号 {{ source.device_model }}</span>
                      <span v-if="source.equipment_version">设备版本 {{ source.equipment_version }}</span>
                      <span v-if="source.software_version">软件 {{ source.software_version }}</span>
                      <span v-if="source.firmware_version">固件 {{ source.firmware_version }}</span>
                      <span v-if="source.hardware_revision">硬件 {{ source.hardware_revision }}</span>
                      <span v-if="source.site_id">厂区 {{ source.site_id }}</span>
                    </div>
                    <blockquote>{{ source.snippet }}</blockquote>
                  </article>
                </div>
              </details>

              <div
                v-if="message.role === 'assistant' && message.traceId && message.status === 'ready' && !message.versionScopeOptions?.length"
                class="resolution-panel"
              >
                <span>本次问题处理结果</span>
                <div class="resolution-actions">
                  <button
                    :class="{ active: message.resolutionStatus === 'solved' }"
                    :disabled="message.resolutionSubmitting"
                    @click="submitResolution(message, 'solved')"
                  ><el-icon><CircleCheck /></el-icon>已解决</button>
                  <button
                    :class="{ active: message.resolutionStatus === 'partial' }"
                    :disabled="message.resolutionSubmitting"
                    @click="submitResolution(message, 'partial')"
                  ><el-icon><Warning /></el-icon>部分解决</button>
                  <button
                    :class="{ active: message.resolutionStatus === 'unsolved' }"
                    :disabled="message.resolutionSubmitting"
                    @click="submitResolution(message, 'unsolved')"
                  ><el-icon><CircleClose /></el-icon>未解决</button>
                </div>
              </div>

              <div
                v-if="hasAppRole('workflow') && message.role === 'assistant' && message.traceId && message.status === 'ready' && (message.requiresHumanReview || message.resolutionStatus === 'unsolved' || message.workflowCaseId)"
                class="workflow-escalation"
              >
                <span><el-icon><Tickets /></el-icon><span><strong>人工处理</strong><small>转交工程师或供应商继续处理</small></span></span>
                <button :disabled="message.workflowSubmitting" @click="openOrCreateWorkflowCase(message)">
                  <el-icon v-if="message.workflowSubmitting" class="is-loading"><Loading /></el-icon>
                  <el-icon v-else><Tickets /></el-icon>
                  {{ message.workflowSubmitting ? '正在创建' : message.workflowCaseId ? '查看工单' : '发起处理' }}
                </button>
              </div>

              <div class="message-meta">
                <span>{{ formatTime(message.time) }}</span>
                <template v-if="message.role === 'assistant' && message.traceId && message.status === 'ready'">
                  <button :class="{ active: message.feedback === 1 }" title="回答有帮助" @click="submitFeedback(message, 1)">👍</button>
                  <button :class="{ active: message.feedback === 0 }" title="回答需改进" @click="submitFeedback(message, 0)">👎</button>
                </template>
              </div>
            </div>
          </article>
        </template>
      </section>

      <footer class="composer-area">
        <div
          class="composer"
          :class="{ dragging: dragActive, disabled: sending }"
          @dragenter.prevent="dragActive = true"
          @dragover.prevent="dragActive = true"
          @dragleave.prevent="dragActive = false"
          @drop.prevent="onDrop"
        >
          <div v-if="pendingImages.length" class="pending-images">
            <div v-for="image in pendingImages" :key="image.id" class="pending-image">
              <img :src="image.previewUrl" :alt="image.file.name" />
              <button title="移除图片" @click="removePendingImage(image.id)"><el-icon><Close /></el-icon></button>
              <span>{{ formatBytes(image.file.size) }}</span>
            </div>
          </div>
          <textarea
            v-model="question"
            :disabled="sending"
            rows="1"
            placeholder="描述设备问题，或上传现场图片辅助分析……"
            @keydown="onComposerKeydown"
          />
          <div class="composer-toolbar">
            <div class="attachment-actions">
              <input ref="fileInput" hidden type="file" multiple accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp" @change="selectImages(($event.target as HTMLInputElement).files ?? [])" />
              <button :disabled="sending || pendingImages.length >= attachmentConfig.max_files" title="上传图片" @click="openFilePicker">
                <el-icon><UploadFilled /></el-icon>
              </button>
              <span>图片仅本会话可见 · {{ attachmentHint }}</span>
            </div>
            <button class="send-button" :disabled="!canSend" @click="sendMessage()">
              <el-icon v-if="sending" class="is-loading"><Loading /></el-icon>
              <el-icon v-else><Promotion /></el-icon>
            </button>
          </div>
          <div v-if="dragActive" class="drop-overlay"><el-icon><FolderOpened /></el-icon><strong>松开即可添加图片</strong></div>
        </div>
        <p class="composer-note">回答由知识库和模型共同生成，关键设备操作请结合原始手册复核。</p>
      </footer>
    </main>

    <ApiKeyDialog v-model="settingsVisible" :api-key="apiKey" @save="saveSettings" />
  </div>
</template>
