<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  ChatDotRound,
  Check,
  CircleClose,
  Connection,
  DocumentAdd,
  Files,
  FolderOpened,
  Loading,
  Lock,
  Refresh,
  Search,
  Setting,
  SwitchButton,
  UploadFilled,
  View,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import ApiKeyDialog from '../shared/ApiKeyDialog.vue'
import { apiFetch } from '../shared/api'
import { formatBytes } from '../shared/format'
import { getApiKey, saveApiKey, siblingServiceUrl } from '../shared/storage'

type DocumentStatus = 'draft' | 'active' | 'disabled'
type VersionStatus = 'importing' | 'draft' | 'active' | 'archived' | 'failed'

interface KnowledgeDocument {
  document_id: string
  title: string
  status: DocumentStatus
  active_revision_id: string
  active_revision_ids?: string[]
  version_count: number
  item_names: string[]
  created_at: string
  updated_at: string
  versions?: KnowledgeVersion[]
}

interface KnowledgeVersion {
  revision_id: string
  document_id: string
  version_label: string
  filename: string
  status: VersionStatus
  import_status: string
  chunk_count: number
  image_count: number
  file_size: number
  item_names: string[]
  error?: string
  created_at: string
  published_at?: string
  device_model: string
  equipment_version: string
  software_version: string
  firmware_version: string
  hardware_revision: string
  site_id: string
  asset_ids: string[]
  trust_level: string
}

interface AuditLog {
  audit_id: string
  action: string
  actor: string
  revision_id: string
  created_at: string
}

const apiKey = ref(getApiKey())
const settingsVisible = ref(false)
const documents = ref<KnowledgeDocument[]>([])
const loading = ref(false)
const query = ref('')
const statusFilter = ref('')
const total = ref(0)
const selected = ref<KnowledgeDocument | null>(null)
const detailVisible = ref(false)
const detailLoading = ref(false)
const auditVisible = ref(false)
const auditLogs = ref<AuditLog[]>([])
const uploadVisible = ref(false)
const uploading = ref(false)
const registeringLegacy = ref(false)
const uploadFile = ref<File | null>(null)
const emptyUploadForm = () => ({
  documentId: '', title: '', versionLabel: '', deviceModel: '', softwareVersion: '',
  equipmentVersion: '', firmwareVersion: '', hardwareRevision: '', siteId: '', assetIds: '', trustLevel: 'manufacturer_manual', publishAfterImport: false,
})
const uploadForm = ref(emptyUploadForm())
const fileInput = ref<HTMLInputElement | null>(null)

const chatUrl = siblingServiceUrl('8001', '/chat.html')
const activeCount = computed(() => documents.value.filter((item) => item.status === 'active').length)
const draftCount = computed(() => documents.value.filter((item) => item.status === 'draft').length)
const disabledCount = computed(() => documents.value.filter((item) => item.status === 'disabled').length)

const statusLabels: Record<string, string> = {
  draft: '草稿', active: '生效', disabled: '已停用', importing: '导入中', archived: '历史版本', failed: '失败',
}
const trustLabels: Record<string, string> = {
  enterprise_sop: '企业批准 SOP', manufacturer_manual: '厂商手册', internal_reference: '内部参考', external_web: '外部网页',
}

const auditLabels: Record<string, string> = {
  register_import: '登记导入', import_completed: '导入完成', import_failed: '导入失败',
  publish: '发布版本', rollback: '回滚版本', disable: '停用文档', enable: '启用文档',
  register_legacy: '登记旧知识库',
}

function errorText(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  if (/missing api key|invalid api key|401|403/i.test(message)) settingsVisible.value = true
  return message
}

function formatDate(value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false })
}

async function loadDocuments(): Promise<void> {
  loading.value = true
  try {
    const params = new URLSearchParams({ limit: '100' })
    if (query.value.trim()) params.set('query', query.value.trim())
    if (statusFilter.value) params.set('status', statusFilter.value)
    const response = await apiFetch(`/knowledge/documents?${params}`, apiKey.value)
    const payload = await response.json() as { items: KnowledgeDocument[]; total: number }
    documents.value = payload.items
    total.value = payload.total
  } catch (error) {
    ElMessage.error(`文档列表加载失败：${errorText(error)}`)
  } finally {
    loading.value = false
  }
}

async function openDetail(document: KnowledgeDocument): Promise<void> {
  detailVisible.value = true
  detailLoading.value = true
  try {
    const response = await apiFetch(`/knowledge/documents/${encodeURIComponent(document.document_id)}`, apiKey.value)
    selected.value = await response.json() as KnowledgeDocument
  } catch (error) {
    ElMessage.error(`版本详情加载失败：${errorText(error)}`)
  } finally {
    detailLoading.value = false
  }
}

function openNewDocument(): void {
  uploadForm.value = emptyUploadForm()
  uploadFile.value = null
  uploadVisible.value = true
}

function openNewVersion(document: KnowledgeDocument, baseVersion?: KnowledgeVersion): void {
  uploadForm.value = {
    documentId: document.document_id,
    title: document.title,
    versionLabel: '',
    deviceModel: baseVersion?.device_model ?? '',
    equipmentVersion: baseVersion?.equipment_version ?? '',
    softwareVersion: baseVersion?.software_version ?? '',
    firmwareVersion: baseVersion?.firmware_version ?? '',
    hardwareRevision: baseVersion?.hardware_revision ?? '',
    siteId: baseVersion?.site_id ?? '',
    assetIds: baseVersion?.asset_ids?.join('，') ?? '',
    trustLevel: baseVersion?.trust_level ?? 'manufacturer_manual',
    publishAfterImport: false,
  }
  uploadFile.value = null
  uploadVisible.value = true
}

function chooseFile(files: FileList | null): void {
  const file = files?.[0]
  if (!file) return
  const extension = `.${file.name.split('.').pop()?.toLowerCase() ?? ''}`
  if (!['.pdf', '.md', '.markdown'].includes(extension)) {
    ElMessage.warning('只支持 PDF 或 Markdown 文件')
    return
  }
  uploadFile.value = file
  if (!uploadForm.value.title) uploadForm.value.title = file.name.replace(/\.(pdf|md|markdown)$/i, '')
  if (fileInput.value) fileInput.value.value = ''
}

async function submitUpload(): Promise<void> {
  if (!uploadFile.value) {
    ElMessage.warning('请先选择文档文件')
    return
  }
  uploading.value = true
  const form = new FormData()
  form.append('file', uploadFile.value)
  form.append('title', uploadForm.value.title.trim())
  form.append('version_label', uploadForm.value.versionLabel.trim())
  form.append('device_model', uploadForm.value.deviceModel.trim())
  form.append('equipment_version', uploadForm.value.equipmentVersion.trim())
  form.append('software_version', uploadForm.value.softwareVersion.trim())
  form.append('firmware_version', uploadForm.value.firmwareVersion.trim())
  form.append('hardware_revision', uploadForm.value.hardwareRevision.trim())
  form.append('site_id', uploadForm.value.siteId.trim())
  form.append('asset_ids', uploadForm.value.assetIds.trim())
  form.append('trust_level', uploadForm.value.trustLevel)
  form.append('publish_after_import', String(uploadForm.value.publishAfterImport))
  if (uploadForm.value.documentId) form.append('document_id', uploadForm.value.documentId)
  try {
    const response = await apiFetch('/knowledge/documents/import', apiKey.value, { method: 'POST', body: form })
    const payload = await response.json() as { task_id: string; document_id: string }
    uploadVisible.value = false
    ElMessage.success('版本已登记，导入完成前不会参与正式查询')
    await loadDocuments()
    void pollImport(payload.task_id, payload.document_id)
  } catch (error) {
    ElMessage.error(`版本导入失败：${errorText(error)}`)
  } finally {
    uploading.value = false
  }
}

async function pollImport(taskId: string, documentId: string): Promise<void> {
  for (let attempt = 0; attempt < 180; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 2000))
    try {
      const response = await apiFetch(`/status/${encodeURIComponent(taskId)}`, apiKey.value)
      const status = await response.json() as { status: string }
      if (['completed', 'failed'].includes(status.status)) {
        await loadDocuments()
        if (detailVisible.value && selected.value?.document_id === documentId) {
          await openDetail(selected.value)
        }
        if (status.status === 'completed') {
          ElMessage.success('版本导入完成，可以审核后发布')
        } else {
          ElMessage.error('版本导入失败，请查看错误详情')
        }
        return
      }
    } catch {
      return
    }
  }
}

async function lifecycleAction(path: string, success: string): Promise<void> {
  try {
    await apiFetch(path, apiKey.value, { method: 'POST' })
    ElMessage.success(success)
    await loadDocuments()
    if (selected.value) await openDetail(selected.value)
  } catch (error) {
    ElMessage.error(`操作失败：${errorText(error)}`)
  }
}

async function publishVersion(version: KnowledgeVersion): Promise<void> {
  if (!selected.value) return
  try {
    await ElMessageBox.confirm(
      `发布 ${version.version_label} 后，仅相同适用范围的旧版本会自动归档；其他软件/固件版本继续生效。`,
      '发布知识版本',
      { confirmButtonText: '确认发布', cancelButtonText: '取消', type: 'warning' },
    )
  } catch { return }
  await lifecycleAction(
    `/knowledge/documents/${encodeURIComponent(selected.value.document_id)}/versions/${encodeURIComponent(version.revision_id)}/publish`,
    `${version.version_label} 已发布`,
  )
}

async function rollbackVersion(version: KnowledgeVersion): Promise<void> {
  if (!selected.value) return
  try {
    await ElMessageBox.confirm(`确定回滚到 ${version.version_label} 吗？`, '回滚版本', {
      confirmButtonText: '确认回滚', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  await lifecycleAction(
    `/knowledge/documents/${encodeURIComponent(selected.value.document_id)}/versions/${encodeURIComponent(version.revision_id)}/rollback`,
    `已回滚到 ${version.version_label}`,
  )
}

async function toggleDocument(document: KnowledgeDocument): Promise<void> {
  const enabling = document.status === 'disabled'
  try {
    await ElMessageBox.confirm(
      enabling ? '启用后，当前生效版本会重新参与查询。' : '停用后会立即退出查询，但不会删除文件、图片或向量。',
      enabling ? '启用文档' : '停用文档',
      { confirmButtonText: enabling ? '确认启用' : '确认停用', cancelButtonText: '取消', type: enabling ? 'info' : 'warning' },
    )
  } catch { return }
  await lifecycleAction(
    `/knowledge/documents/${encodeURIComponent(document.document_id)}/${enabling ? 'enable' : 'disable'}`,
    enabling ? '文档已启用' : '文档已停用',
  )
}

async function openAudit(): Promise<void> {
  auditVisible.value = true
  try {
    const response = await apiFetch('/knowledge/audit?limit=200', apiKey.value)
    auditLogs.value = (await response.json() as { items: AuditLog[] }).items
  } catch (error) {
    ElMessage.error(`审计记录加载失败：${errorText(error)}`)
  }
}

async function registerLegacyKnowledge(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '系统会为旧 Milvus 切片补齐强制版本元数据，校验数量一致后登记为 legacy-v1。正文和向量保持不变，Milvus 会重新分配切片主键。',
      '迁移旧知识库',
      { confirmButtonText: '开始迁移', cancelButtonText: '取消', type: 'warning' },
    )
  } catch { return }
  registeringLegacy.value = true
  try {
    const response = await apiFetch('/knowledge/legacy/register', apiKey.value, { method: 'POST' })
    const payload = await response.json() as {
      discovered: number
      registered: number
      migrated: number
      migrated_chunks: number
      unchanged: number
      skipped: number
    }
    ElMessage.success(
      `扫描 ${payload.discovered} 份，迁移 ${payload.migrated} 份/${payload.migrated_chunks} 个切片，新增登记 ${payload.registered} 份，已完成 ${payload.unchanged} 份`,
    )
    await loadDocuments()
  } catch (error) {
    ElMessage.error(`旧知识库登记失败：${errorText(error)}`)
  } finally {
    registeringLegacy.value = false
  }
}

async function saveSettings(value: string): Promise<void> {
  saveApiKey(value)
  apiKey.value = value
  await loadDocuments()
}

onMounted(loadDocuments)
</script>

<template>
  <div class="app-frame governance-layout">
    <header class="governance-header">
      <div class="governance-brand">
        <div class="brand-mark">EA</div>
        <div class="brand-copy"><strong>知识库治理</strong><span>版本、发布与审计中心</span></div>
      </div>
      <nav>
        <a href="/import.html" class="top-button"><el-icon><UploadFilled /></el-icon><span class="desktop-label">快速导入</span></a>
        <a :href="chatUrl" class="top-button"><el-icon><ChatDotRound /></el-icon><span class="desktop-label">返回问答</span></a>
        <button class="top-button" @click="settingsVisible = true"><el-icon><Connection /></el-icon><span class="desktop-label">API 设置</span></button>
      </nav>
    </header>

    <main class="governance-content">
      <section class="governance-heading">
        <div><div class="eyebrow">Knowledge Governance</div><h1>文档与版本</h1><p>只有已发布且未停用的版本可以进入问答检索。</p></div>
        <div class="heading-actions">
          <button class="audit-button" :disabled="registeringLegacy" @click="registerLegacyKnowledge"><el-icon :class="{ 'is-loading': registeringLegacy }"><Refresh /></el-icon>迁移旧知识库</button>
          <button class="audit-button" @click="openAudit"><el-icon><View /></el-icon>操作审计</button>
          <button class="primary-action" @click="openNewDocument"><el-icon><DocumentAdd /></el-icon>导入新文档</button>
        </div>
      </section>

      <section class="governance-stats">
        <div><span class="stat-icon active"><el-icon><Check /></el-icon></span><p>生效文档<strong>{{ activeCount }}</strong></p></div>
        <div><span class="stat-icon draft"><el-icon><Files /></el-icon></span><p>待发布草稿<strong>{{ draftCount }}</strong></p></div>
        <div><span class="stat-icon disabled"><el-icon><Lock /></el-icon></span><p>已停用<strong>{{ disabledCount }}</strong></p></div>
        <div><span class="stat-icon total"><el-icon><FolderOpened /></el-icon></span><p>当前结果<strong>{{ total }}</strong></p></div>
      </section>

      <section class="governance-panel">
        <div class="filter-bar">
          <div class="search-box"><el-icon><Search /></el-icon><input v-model="query" placeholder="搜索文档名称或设备型号" @keyup.enter="loadDocuments" /></div>
          <select v-model="statusFilter" @change="loadDocuments"><option value="">全部状态</option><option value="active">生效</option><option value="draft">草稿</option><option value="disabled">已停用</option></select>
          <button class="refresh-button" :disabled="loading" @click="loadDocuments"><el-icon :class="{ 'is-loading': loading }"><Refresh /></el-icon>刷新</button>
        </div>

        <div v-if="loading" class="governance-empty"><el-icon class="is-loading"><Loading /></el-icon><span>正在读取治理数据</span></div>
        <div v-else-if="!documents.length" class="governance-empty"><el-icon><Files /></el-icon><strong>还没有受治理文档</strong><span>点击“导入新文档”创建第一个草稿版本。</span></div>
        <div v-else class="document-table-wrap">
          <table class="document-table">
            <thead><tr><th>文档</th><th>设备</th><th>当前版本</th><th>状态</th><th>版本数</th><th>最近更新</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="document in documents" :key="document.document_id">
                <td><button class="document-title" @click="openDetail(document)"><span><el-icon><Files /></el-icon></span><div><strong>{{ document.title }}</strong><small>{{ document.document_id.slice(0, 12) }}</small></div></button></td>
                <td><div class="item-tags"><span v-for="name in document.item_names.slice(0, 2)" :key="name">{{ name }}</span><i v-if="!document.item_names.length">待识别</i></div></td>
                <td><code>{{ document.active_revision_ids?.length ? `${document.active_revision_ids.length} 个适用版本` : document.active_revision_id ? '1 个适用版本' : '未发布' }}</code></td>
                <td><span class="status-pill" :class="document.status">{{ statusLabels[document.status] }}</span></td>
                <td>{{ document.version_count }}</td><td>{{ formatDate(document.updated_at) }}</td>
                <td><div class="row-actions"><button @click="openDetail(document)">版本</button><button @click="openNewVersion(document)">新适用版本</button><button :class="{ danger: document.status !== 'disabled' }" @click="toggleDocument(document)">{{ document.status === 'disabled' ? '启用' : '停用' }}</button></div></td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </main>

    <el-drawer v-model="detailVisible" size="min(720px, 92vw)" title="文档版本详情">
      <div v-if="detailLoading" class="governance-empty"><el-icon class="is-loading"><Loading /></el-icon></div>
      <div v-else-if="selected" class="version-detail">
        <div class="detail-head"><div><span class="status-pill" :class="selected.status">{{ statusLabels[selected.status] }}</span><h2>{{ selected.title }}</h2><p>{{ selected.document_id }}</p></div><button class="primary-action small" @click="openNewVersion(selected)"><el-icon><DocumentAdd /></el-icon>导入新版本</button></div>
        <div class="version-list">
          <article v-for="version in selected.versions" :key="version.revision_id" class="version-card" :class="version.status">
            <div class="version-timeline"><i /></div>
            <div class="version-main">
              <div class="version-head"><div><strong>{{ version.version_label }}</strong><span class="status-pill" :class="version.status">{{ statusLabels[version.status] }}</span></div><small>{{ formatDate(version.created_at) }}</small></div>
              <p>{{ version.filename }} · {{ formatBytes(version.file_size || 0) }}</p>
              <div class="item-tags">
                <span v-if="version.device_model">型号 {{ version.device_model }}</span>
                <span v-if="version.equipment_version">设备版本 {{ version.equipment_version }}</span>
                <span v-if="version.software_version">软件 {{ version.software_version }}</span>
                <span v-if="version.firmware_version">固件 {{ version.firmware_version }}</span>
                <span v-if="version.hardware_revision">硬件 {{ version.hardware_revision }}</span>
                <span v-if="version.site_id">厂区 {{ version.site_id }}</span>
                <i v-if="!version.device_model && !version.equipment_version && !version.software_version && !version.firmware_version && !version.hardware_revision && !version.site_id">通用适用范围</i>
              </div>
              <div class="version-metrics"><span>可信等级 <b>{{ trustLabels[version.trust_level] ?? '厂商手册' }}</b></span><span>切片 <b>{{ version.chunk_count }}</b></span><span>图片 <b>{{ version.image_count }}</b></span><span>Revision <code>{{ version.revision_id.slice(0, 12) }}</code></span></div>
              <p v-if="version.error" class="version-error">{{ version.error }}</p>
              <div class="version-actions">
                <button v-if="version.import_status === 'completed' && version.status === 'draft'" class="publish" @click="publishVersion(version)"><el-icon><Check /></el-icon>发布</button>
                <button v-if="version.import_status === 'completed' && version.status === 'archived'" @click="rollbackVersion(version)"><el-icon><Refresh /></el-icon>回滚到此版本</button>
                <button v-if="version.import_status === 'completed'" @click="openNewVersion(selected, version)"><el-icon><DocumentAdd /></el-icon>基于此范围导入</button>
                <span v-if="version.status === 'active'"><el-icon><Check /></el-icon>当前生效版本</span>
                <span v-if="version.status === 'importing'"><el-icon class="is-loading"><Loading /></el-icon>正在导入</span>
              </div>
            </div>
          </article>
        </div>
      </div>
    </el-drawer>

    <el-dialog v-model="uploadVisible" width="min(560px, calc(100vw - 28px))" :title="uploadForm.documentId ? '导入新版本' : '导入新文档'">
      <div class="upload-form">
        <label>文档名称<input v-model="uploadForm.title" placeholder="例如：LJ2268 操作手册" /></label>
        <label>业务版本<input v-model="uploadForm.versionLabel" placeholder="例如：V2.1、2026版；留空自动生成" /></label>
        <label>来源可信等级<select v-model="uploadForm.trustLevel" class="trust-select"><option value="enterprise_sop">企业批准 SOP</option><option value="manufacturer_manual">厂商手册</option><option value="internal_reference">内部参考</option></select></label>
        <label>设备型号<input v-model="uploadForm.deviceModel" placeholder="例如：LJ2268" /></label>
        <label>设备版本 / 代次<input v-model="uploadForm.equipmentVersion" placeholder="例如：A版、第二代、2025款" /></label>
        <label>软件版本<input v-model="uploadForm.softwareVersion" placeholder="例如：Control Suite 3.2" /></label>
        <label>固件版本<input v-model="uploadForm.firmwareVersion" placeholder="例如：FW 1.8.4" /></label>
        <label>硬件修订版<input v-model="uploadForm.hardwareRevision" placeholder="例如：Rev C" /></label>
        <label>厂区 / 站点（可选）<input v-model="uploadForm.siteId" placeholder="例如：SZ-01" /></label>
        <label>设备编号（可选）<input v-model="uploadForm.assetIds" placeholder="多个编号用逗号分隔" /></label>
        <p class="form-hint">同一适用范围只能有一个生效版本；同型号的不同软件、固件或硬件版本可以同时生效。</p>
        <button class="file-picker" @click="fileInput?.click()"><input ref="fileInput" hidden type="file" accept=".pdf,.md,.markdown" @change="chooseFile(($event.target as HTMLInputElement).files)" /><el-icon><UploadFilled /></el-icon><span v-if="uploadFile"><strong>{{ uploadFile.name }}</strong><small>{{ formatBytes(uploadFile.size) }}</small></span><span v-else><strong>选择 PDF 或 Markdown</strong><small>新版本导入完成后默认为草稿</small></span></button>
        <label class="publish-option"><input v-model="uploadForm.publishAfterImport" type="checkbox" /><span><strong>导入完成后自动发布</strong><small>生产资料建议关闭，审核后手动发布。</small></span></label>
      </div>
      <template #footer><el-button @click="uploadVisible = false">取消</el-button><el-button type="primary" :loading="uploading" @click="submitUpload">开始导入</el-button></template>
    </el-dialog>

    <el-drawer v-model="auditVisible" title="治理操作审计" size="min(520px, 92vw)">
      <div class="audit-list"><article v-for="log in auditLogs" :key="log.audit_id"><span><el-icon><Setting /></el-icon></span><div><strong>{{ auditLabels[log.action] ?? log.action }}</strong><p>{{ log.actor }} · {{ log.revision_id ? log.revision_id.slice(0, 10) : '文档级操作' }}</p><small>{{ formatDate(log.created_at) }}</small></div></article><div v-if="!auditLogs.length" class="governance-empty">暂无审计记录</div></div>
    </el-drawer>

    <ApiKeyDialog v-model="settingsVisible" :api-key="apiKey" @save="saveSettings" />
  </div>
</template>

<style scoped>
.trust-select {
  height: 39px;
  padding: 0 11px;
  border: 1px solid var(--line);
  border-radius: 9px;
  outline: 0;
  background: #fff;
}
</style>
