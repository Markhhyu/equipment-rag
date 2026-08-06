<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import {
  ChatDotRound,
  Check,
  CircleCheck,
  Close,
  Connection,
  Document,
  Files,
  Grid,
  Management,
  Loading,
  Picture,
  Refresh,
  Setting,
  UploadFilled,
  Warning,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import ApiKeyDialog from '../shared/ApiKeyDialog.vue'
import { apiFetch } from '../shared/api'
import { hasAppRole } from '../shared/auth'
import { formatBytes, formatNodeName } from '../shared/format'
import { getApiKey, saveApiKey, siblingServiceUrl } from '../shared/storage'

type TaskState = 'queued' | 'uploading' | 'processing' | 'completed' | 'failed'

interface ImportTask {
  localId: string
  file: File
  taskId?: string
  state: TaskState
  error?: string
  statusText: string
  doneList: string[]
  runningList: string[]
  knowledgeBaseReady: boolean
  imageEnrichment: {
    total: number
    finished: number
    pending: number
    processing: number
    failed: number
    is_finished: boolean
    available?: boolean
  }
}

interface StatusResponse {
  status: string
  done_list?: string[]
  running_list?: string[]
  knowledge_base_ready?: boolean
  image_enrichment?: ImportTask['imageEnrichment']
}

const apiKey = ref(getApiKey())
const settingsVisible = ref(false)
const tasks = ref<ImportTask[]>([])
const dragActive = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const pollTimers = new Map<string, number>()
const chatUrl = siblingServiceUrl('8001', '/chat.html')
const knowledgeUrl = '/knowledge.html'
const appsUrl = '/apps.html'

const activeCount = computed(() => tasks.value.filter((task) =>
  ['queued', 'uploading', 'processing'].includes(task.state)
  || (task.state === 'completed' && task.imageEnrichment.total > 0 && !task.imageEnrichment.is_finished),
).length)
const completedCount = computed(() => tasks.value.filter((task) => task.state === 'completed').length)
const failedCount = computed(() => tasks.value.filter((task) => task.state === 'failed').length)

function newTask(file: File): ImportTask {
  return {
    localId: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    file,
    state: 'queued',
    statusText: '等待上传',
    doneList: [],
    runningList: [],
    knowledgeBaseReady: false,
    imageEnrichment: { total: 0, finished: 0, pending: 0, processing: 0, failed: 0, is_finished: false },
  }
}

function addFiles(fileList: FileList | File[]): void {
  const accepted: File[] = []
  for (const file of Array.from(fileList)) {
    const extension = `.${file.name.split('.').pop()?.toLowerCase() ?? ''}`
    if (!['.pdf', '.md', '.markdown'].includes(extension)) {
      ElMessage.warning(`${file.name} 不是支持的 PDF 或 Markdown 文件`)
      continue
    }
    accepted.push(file)
  }
  const newTasks = accepted.map(newTask)
  tasks.value.unshift(...newTasks)
  for (const task of newTasks) void uploadTask(task)
  if (fileInput.value) fileInput.value.value = ''
}

function onDrop(event: DragEvent): void {
  dragActive.value = false
  if (event.dataTransfer?.files.length) addFiles(event.dataTransfer.files)
}

async function uploadTask(task: ImportTask): Promise<void> {
  task.state = 'uploading'
  task.statusText = '正在上传文件'
  task.error = undefined
  const formData = new FormData()
  formData.append('files', task.file)

  try {
    const response = await apiFetch('/upload', apiKey.value, { method: 'POST', body: formData })
    const payload = await response.json() as { task_ids: string[] }
    task.taskId = payload.task_ids[0]
    task.state = 'processing'
    task.statusText = '正在构建文本知识库'
    if (task.taskId) schedulePoll(task, 350)
  } catch (error) {
    task.state = 'failed'
    task.statusText = '上传失败'
    task.error = error instanceof Error ? error.message : String(error)
    if (/missing api key|invalid api key|401/i.test(task.error)) settingsVisible.value = true
  }
}

function schedulePoll(task: ImportTask, delay = 1800): void {
  if (!task.taskId) return
  const oldTimer = pollTimers.get(task.localId)
  if (oldTimer) window.clearTimeout(oldTimer)
  pollTimers.set(task.localId, window.setTimeout(() => void pollTask(task), delay))
}

async function pollTask(task: ImportTask): Promise<void> {
  if (!task.taskId) return
  try {
    const response = await apiFetch(`/status/${encodeURIComponent(task.taskId)}`, apiKey.value)
    const payload = await response.json() as StatusResponse
    task.doneList = payload.done_list ?? []
    task.runningList = payload.running_list ?? []
    task.knowledgeBaseReady = !!payload.knowledge_base_ready
    if (payload.image_enrichment) task.imageEnrichment = payload.image_enrichment

    if (payload.status === 'failed') {
      task.state = 'failed'
      task.statusText = '导入失败'
      return
    }
    if (payload.status === 'completed') {
      task.state = 'completed'
      const imagesStillRunning = task.imageEnrichment.total > 0 && !task.imageEnrichment.is_finished
      task.statusText = imagesStillRunning ? '文本可用，图片增强中' : '导入完成'
      if (imagesStillRunning) schedulePoll(task, 2500)
      return
    }
    task.state = 'processing'
    task.statusText = '正在构建文本知识库'
    schedulePoll(task)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    if (/404/.test(message) && task.state === 'processing') {
      schedulePoll(task, 2200)
      return
    }
    task.state = 'failed'
    task.statusText = '状态查询失败'
    task.error = message
  }
}

function retryTask(task: ImportTask): void {
  const timer = pollTimers.get(task.localId)
  if (timer) window.clearTimeout(timer)
  pollTimers.delete(task.localId)
  task.taskId = undefined
  task.doneList = []
  task.runningList = []
  task.knowledgeBaseReady = false
  task.imageEnrichment = { total: 0, finished: 0, pending: 0, processing: 0, failed: 0, is_finished: false }
  void uploadTask(task)
}

function removeTask(task: ImportTask): void {
  const timer = pollTimers.get(task.localId)
  if (timer) window.clearTimeout(timer)
  pollTimers.delete(task.localId)
  tasks.value = tasks.value.filter((item) => item.localId !== task.localId)
}

function saveSettings(value: string): void {
  saveApiKey(value)
  apiKey.value = value
  ElMessage.success('连接设置已保存')
}

function textProgress(task: ImportTask): number {
  if (task.knowledgeBaseReady || task.state === 'completed') return 100
  if (task.state === 'uploading') return 18
  if (task.state === 'processing') return Math.min(92, 28 + task.doneList.length * 11)
  if (task.state === 'failed') return 100
  return 4
}

function imageProgress(task: ImportTask): number {
  if (!task.imageEnrichment.total) return task.knowledgeBaseReady ? 100 : 0
  return Math.round((task.imageEnrichment.finished / task.imageEnrichment.total) * 100)
}

onBeforeUnmount(() => {
  for (const timer of pollTimers.values()) window.clearTimeout(timer)
  pollTimers.clear()
})
</script>

<template>
  <div class="app-frame import-layout">
    <header class="import-header">
      <div class="import-brand">
        <div class="brand-mark">EA</div>
        <div class="brand-copy"><strong>设备知识助手</strong><span>知识库管理中心</span></div>
      </div>
      <nav>
        <a :href="appsUrl" class="top-button"><el-icon><Grid /></el-icon><span class="desktop-label">应用中心</span></a>
        <a v-if="hasAppRole('admin')" :href="knowledgeUrl" class="top-button"><el-icon><Management /></el-icon><span class="desktop-label">知识库治理</span></a>
        <a v-if="hasAppRole('query')" :href="chatUrl" class="top-button"><el-icon><ChatDotRound /></el-icon><span class="desktop-label">返回问答</span></a>
        <button class="top-button" @click="settingsVisible = true"><el-icon><Connection /></el-icon><span class="desktop-label">API 设置</span></button>
      </nav>
    </header>

    <main class="import-content">
      <section class="import-heading">
        <div>
          <div class="eyebrow">Knowledge Base</div>
          <h1>导入设备资料</h1>
          <p>上传设备手册和维护文档，系统会自动完成解析、切片、向量化与图片增强。</p>
        </div>
        <div class="summary-cards">
          <div><span>进行中</span><strong>{{ activeCount }}</strong></div>
          <div><span>已完成</span><strong class="success">{{ completedCount }}</strong></div>
          <div><span>失败</span><strong :class="{ danger: failedCount }">{{ failedCount }}</strong></div>
        </div>
      </section>

      <section
        class="upload-zone"
        :class="{ active: dragActive }"
        @click="fileInput?.click()"
        @dragenter.prevent="dragActive = true"
        @dragover.prevent="dragActive = true"
        @dragleave.prevent="dragActive = false"
        @drop.prevent.stop="onDrop"
      >
        <input ref="fileInput" hidden type="file" multiple accept=".pdf,.md,.markdown,application/pdf,text/markdown" @change="addFiles(($event.target as HTMLInputElement).files ?? [])" />
        <div class="upload-illustration"><el-icon><UploadFilled /></el-icon></div>
        <div>
          <h2>拖拽资料到这里，或点击选择文件</h2>
          <p>支持 PDF、Markdown，可一次选择多份文档</p>
        </div>
        <button type="button">选择文件</button>
      </section>

      <section class="scope-note">
        <el-icon><Picture /></el-icon>
        <div><strong>这里导入的是长期知识库资料</strong><span>聊天页上传的现场图片属于会话附件，不会出现在这里，也不会参与后续知识库检索。</span></div>
      </section>

      <section v-if="tasks.length" class="task-section">
        <div class="section-title"><div><h2>导入任务</h2><span>{{ tasks.length }} 个文件</span></div></div>
        <div class="task-list">
          <article v-for="task in tasks" :key="task.localId" class="task-card" :class="task.state">
            <div class="file-type" :class="task.file.name.toLowerCase().endsWith('.pdf') ? 'pdf' : 'md'">
              <el-icon><Document /></el-icon><span>{{ task.file.name.toLowerCase().endsWith('.pdf') ? 'PDF' : 'MD' }}</span>
            </div>
            <div class="task-body">
              <div class="task-head">
                <div><strong>{{ task.file.name }}</strong><span>{{ formatBytes(task.file.size) }}<template v-if="task.taskId"> · {{ task.taskId.slice(0, 8) }}</template></span></div>
                <div class="task-status" :class="task.state">
                  <el-icon v-if="task.state === 'completed'"><CircleCheck /></el-icon>
                  <el-icon v-else-if="task.state === 'failed'"><Warning /></el-icon>
                  <el-icon v-else class="is-loading"><Loading /></el-icon>
                  {{ task.statusText }}
                </div>
              </div>

              <div class="pipeline-row">
                <div class="pipeline-label"><span>文本知识库</span><strong>{{ textProgress(task) }}%</strong></div>
                <el-progress :percentage="textProgress(task)" :show-text="false" :stroke-width="6" :status="task.state === 'failed' ? 'exception' : task.knowledgeBaseReady ? 'success' : undefined" />
              </div>
              <div v-if="task.knowledgeBaseReady" class="ready-line"><el-icon><Check /></el-icon>正文、切片和向量已可用于问答</div>

              <div v-if="task.knowledgeBaseReady && task.imageEnrichment.total > 0" class="pipeline-row image-pipeline">
                <div class="pipeline-label"><span>图片增强 {{ task.imageEnrichment.finished }}/{{ task.imageEnrichment.total }}</span><strong>{{ imageProgress(task) }}%</strong></div>
                <el-progress :percentage="imageProgress(task)" :show-text="false" :stroke-width="5" :status="task.imageEnrichment.is_finished ? 'success' : undefined" />
              </div>

              <details v-if="task.doneList.length || task.runningList.length || task.error" class="task-log">
                <summary>查看处理详情</summary>
                <p v-if="task.error" class="error-copy">{{ task.error }}</p>
                <div v-for="node in task.doneList" :key="`done-${node}`"><el-icon><Check /></el-icon>{{ formatNodeName(node) }}</div>
                <div v-for="node in task.runningList" :key="`running-${node}`" class="running"><el-icon class="is-loading"><Loading /></el-icon>{{ formatNodeName(node) }}</div>
              </details>
            </div>
            <div class="task-actions">
              <button v-if="task.state === 'failed'" title="重新上传" @click="retryTask(task)"><el-icon><Refresh /></el-icon></button>
              <button v-if="!['uploading', 'processing'].includes(task.state)" title="移除任务" @click="removeTask(task)"><el-icon><Close /></el-icon></button>
            </div>
          </article>
        </div>
      </section>

      <section v-else class="empty-tasks">
        <el-icon><Files /></el-icon><strong>还没有导入任务</strong><span>选择上方文件后，处理进度会显示在这里。</span>
      </section>
    </main>

    <ApiKeyDialog v-model="settingsVisible" :api-key="apiKey" @save="saveSettings" />
  </div>
</template>
