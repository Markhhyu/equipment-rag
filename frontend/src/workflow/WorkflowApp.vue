<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  ArrowRight,
  CircleCheck,
  Connection,
  Grid,
  Loading,
  Refresh,
  Search,
  Setting,
  Tickets,
  Timer,
  UserFilled,
  Warning,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import ApiKeyDialog from '../shared/ApiKeyDialog.vue'
import FeishuConfigDialog from './FeishuConfigDialog.vue'
import { apiFetch } from '../shared/api'
import { hasAppRole } from '../shared/auth'
import { applicationPageUrl, directServiceUrl, getApiKey, saveApiKey } from '../shared/storage'

type CaseStatus = 'pending' | 'assigned' | 'in_review' | 'resolved' | 'rejected' | 'cancelled'
type DataRecord = Record<string, unknown>

interface WorkflowCase {
  case_id: string
  case_type: string
  status: CaseStatus
  subject: DataRecord
  context: DataRecord
  result: DataRecord
  external_workflows?: Array<{
    connector_type: string
    instance_id: string
    status: string
    created_at: string
  }>
  assignee: string
  created_by?: string
  created_at: string
  updated_at: string
}

const apiKey = ref(getApiKey())
const settingsVisible = ref(false)
const feishuSettingsVisible = ref(false)
const loading = ref(false)
const detailLoading = ref(false)
const detailVisible = ref(false)
const cases = ref<WorkflowCase[]>([])
const selected = ref<WorkflowCase | null>(null)
const query = ref('')
const statusFilter = ref('')

const appsUrl = applicationPageUrl('/apps', '8001', '/apps.html')
const docsUrl = directServiceUrl('8002', '/docs')
const statusLabels: Record<CaseStatus, string> = {
  pending: '待分派',
  assigned: '已分派',
  in_review: '处理中',
  resolved: '已解决',
  rejected: '已驳回',
  cancelled: '已取消',
}
const typeLabels: Record<string, string> = {
  answer_review: '回答复核',
  equipment_issue: '设备问题',
}
const connectorLabels: Record<string, string> = {
  feishu_approval: '飞书审批',
}
const fieldLabels: Record<string, string> = {
  question: '问题描述', title: '标题', summary: '摘要', trace_id: '问答 Trace', session_id: '会话 ID',
  device_model: '设备型号', device_models: '设备型号', device_name: '设备名称', equipment_version: '设备版本', software_version: '软件版本',
  version_labels: '关联版本', image_refs: '现场图片引用', resolution_status: '问答处理状态',
  firmware_version: '固件版本', hardware_revision: '硬件修订', reason: '转人工原因', review_reason: '复核原因',
  answer: '助手回答', resolution: '处理结论', root_cause: '问题原因', solution: '处理方案', verification: '验证结果',
}

const filteredCases = computed(() => {
  const keyword = query.value.trim().toLocaleLowerCase()
  return cases.value.filter((item) => {
    if (statusFilter.value && item.status !== statusFilter.value) return false
    if (!keyword) return true
    return JSON.stringify(item).toLocaleLowerCase().includes(keyword)
  })
})
const pendingCount = computed(() => cases.value.filter((item) => ['pending', 'assigned', 'in_review'].includes(item.status)).length)
const resolvedCount = computed(() => cases.value.filter((item) => item.status === 'resolved').length)
const closedCount = computed(() => cases.value.filter((item) => ['rejected', 'cancelled'].includes(item.status)).length)

function errorText(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  if (/missing api key|invalid api key|401|403/i.test(message)) settingsVisible.value = true
  return message
}

function formatDate(value: string): string {
  if (!value) return '--'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '--' : date.toLocaleString('zh-CN', { hour12: false })
}

function shortId(value: string): string {
  return value ? value.slice(0, 10) : '--'
}

function caseTitle(item: WorkflowCase): string {
  return String(item.subject.question || item.subject.title || item.subject.summary || `${typeLabels[item.case_type] ?? item.case_type}工单`)
}

function caseDevice(item: WorkflowCase): string {
  return formatValue(
    item.subject.device_models || item.subject.device_model || item.subject.device_name || item.context.device_model || '',
  )
}

function formatValue(value: unknown): string {
  if (Array.isArray(value)) return value.map((item) => formatValue(item)).join('、')
  if (value && typeof value === 'object') return JSON.stringify(value, null, 2)
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value ?? '')
}

function recordEntries(record: DataRecord): Array<{ key: string; label: string; value: string }> {
  return Object.entries(record || {})
    .map(([key, value]) => ({ key, label: fieldLabels[key] ?? key, value: formatValue(value) }))
    .filter((item) => item.value.trim() !== '')
}

async function loadCases(): Promise<void> {
  loading.value = true
  try {
    const response = await apiFetch('/workflow/cases?limit=500', apiKey.value)
    const payload = await response.json() as { items: WorkflowCase[] }
    cases.value = payload.items
  } catch (error) {
    ElMessage.error(`工单列表加载失败：${errorText(error)}`)
  } finally {
    loading.value = false
  }
}

async function openDetail(item: WorkflowCase): Promise<void> {
  await openDetailById(item.case_id, item)
}

async function openDetailById(caseId: string, initial?: WorkflowCase): Promise<void> {
  detailVisible.value = true
  detailLoading.value = true
  selected.value = initial ?? cases.value.find((item) => item.case_id === caseId) ?? null
  try {
    const response = await apiFetch(`/workflow/cases/${encodeURIComponent(caseId)}`, apiKey.value)
    selected.value = await response.json() as WorkflowCase
  } catch (error) {
    ElMessage.error(`工单详情加载失败：${errorText(error)}`)
  } finally {
    detailLoading.value = false
  }
}

async function saveSettings(value: string): Promise<void> {
  saveApiKey(value)
  apiKey.value = value
  await loadCases()
  ElMessage.success('连接设置已保存')
}

onMounted(async () => {
  await loadCases()
  const caseId = new URLSearchParams(window.location.search).get('case_id')?.trim()
  if (caseId) await openDetailById(caseId)
})
</script>

<template>
  <div class="workflow-page">
    <header class="workflow-header">
      <a class="workflow-brand" :href="appsUrl">
        <span class="brand-mark">EA</span>
        <span class="brand-copy"><strong>设备知识助手</strong><span>人工处理中心</span></span>
      </a>
      <nav>
        <a class="top-button" :href="appsUrl"><el-icon><Grid /></el-icon><span class="desktop-label">应用中心</span></a>
        <a v-if="hasAppRole('admin')" class="top-button" :href="docsUrl" target="_blank" rel="noreferrer"><el-icon><Tickets /></el-icon><span class="desktop-label">API 文档</span></a>
        <button
          v-if="hasAppRole('admin')"
          class="top-button"
          type="button"
          aria-label="飞书设置"
          title="飞书设置"
          @click="feishuSettingsVisible = true"
        >
          <el-icon><Setting /></el-icon><span class="desktop-label">飞书设置</span>
        </button>
        <button class="top-button" @click="settingsVisible = true"><el-icon><Connection /></el-icon><span class="desktop-label">API 设置</span></button>
      </nav>
    </header>

    <main class="workflow-main" v-loading="loading">
      <section class="workflow-titlebar">
        <div><div class="eyebrow">Human Resolution</div><h1>人工处理工单</h1><p>工程师、供应商与人工复核记录</p></div>
        <button class="refresh-command" title="刷新工单" @click="loadCases"><el-icon><Refresh /></el-icon>刷新</button>
      </section>

      <section class="workflow-stats">
        <div><span class="stat-icon total"><el-icon><Tickets /></el-icon></span><p>全部工单<strong>{{ cases.length }}</strong></p></div>
        <div><span class="stat-icon pending"><el-icon><Timer /></el-icon></span><p>处理中<strong>{{ pendingCount }}</strong></p></div>
        <div><span class="stat-icon resolved"><el-icon><CircleCheck /></el-icon></span><p>已解决<strong>{{ resolvedCount }}</strong></p></div>
        <div><span class="stat-icon closed"><el-icon><Warning /></el-icon></span><p>其他关闭<strong>{{ closedCount }}</strong></p></div>
      </section>

      <section class="case-panel">
        <div class="case-filters">
          <label class="case-search"><el-icon><Search /></el-icon><input v-model="query" placeholder="搜索问题、设备、Trace 或工单编号" /></label>
          <select v-model="statusFilter" aria-label="工单状态">
            <option value="">全部状态</option>
            <option v-for="(label, value) in statusLabels" :key="value" :value="value">{{ label }}</option>
          </select>
          <span>{{ filteredCases.length }} 条</span>
        </div>

        <div v-if="filteredCases.length" class="case-list">
          <div class="case-list-head"><span>问题</span><span>状态</span><span>处理人</span><span>更新时间</span><i /></div>
          <button v-for="item in filteredCases" :key="item.case_id" class="case-row" @click="openDetail(item)">
            <span class="case-subject"><strong>{{ caseTitle(item) }}</strong><small><code>{{ shortId(item.case_id) }}</code><template v-if="caseDevice(item)"> · {{ caseDevice(item) }}</template> · {{ typeLabels[item.case_type] ?? item.case_type }}</small></span>
            <span><i class="status-pill" :class="item.status">{{ statusLabels[item.status] }}</i></span>
            <span class="assignee"><el-icon><UserFilled /></el-icon>{{ item.assignee || '未分派' }}</span>
            <span class="updated-at">{{ formatDate(item.updated_at) }}</span>
            <el-icon class="row-arrow"><ArrowRight /></el-icon>
          </button>
        </div>
        <div v-else class="case-empty"><el-icon><Tickets /></el-icon><strong>暂无匹配工单</strong><span>新的人工处理请求会显示在这里</span></div>
      </section>
    </main>

    <el-drawer v-model="detailVisible" size="min(620px, 94vw)" title="工单详情">
      <div v-if="selected" class="case-detail" v-loading="detailLoading">
        <div class="detail-title"><span class="status-pill" :class="selected.status">{{ statusLabels[selected.status] }}</span><h2>{{ caseTitle(selected) }}</h2><code>{{ selected.case_id }}</code></div>
        <dl class="detail-meta">
          <div><dt>工单类型</dt><dd>{{ typeLabels[selected.case_type] ?? selected.case_type }}</dd></div>
          <div><dt>处理人</dt><dd>{{ selected.assignee || '未分派' }}</dd></div>
          <div><dt>创建时间</dt><dd>{{ formatDate(selected.created_at) }}</dd></div>
          <div><dt>更新时间</dt><dd>{{ formatDate(selected.updated_at) }}</dd></div>
        </dl>
        <section class="detail-section"><h3>问题信息</h3><dl v-if="recordEntries(selected.subject).length" class="detail-record"><div v-for="item in recordEntries(selected.subject)" :key="item.key"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></div></dl><p v-else>暂无问题信息</p></section>
        <section class="detail-section"><h3>处理上下文</h3><dl v-if="recordEntries(selected.context).length" class="detail-record"><div v-for="item in recordEntries(selected.context)" :key="item.key"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></div></dl><p v-else>暂无处理上下文</p></section>
        <section class="detail-section"><h3>处理结果</h3><dl v-if="recordEntries(selected.result).length" class="detail-record"><div v-for="item in recordEntries(selected.result)" :key="item.key"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></div></dl><p v-else>工单尚未形成处理结果</p></section>
        <section class="detail-section"><h3>外部流程</h3><dl v-if="selected.external_workflows?.length" class="detail-record"><div v-for="item in selected.external_workflows" :key="`${item.connector_type}-${item.instance_id}`"><dt>{{ connectorLabels[item.connector_type] ?? item.connector_type }}</dt><dd><code>{{ item.instance_id }}</code> · {{ item.status }}</dd></div></dl><p v-else>尚未关联外部流程</p></section>
      </div>
    </el-drawer>

    <ApiKeyDialog v-model="settingsVisible" :api-key="apiKey" @save="saveSettings" />
    <FeishuConfigDialog v-if="hasAppRole('admin')" v-model="feishuSettingsVisible" :api-key="apiKey" />
  </div>
</template>
