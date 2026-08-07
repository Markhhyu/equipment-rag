<script setup lang="ts">
import {
  ChatDotRound,
  Coin,
  Connection,
  DataAnalysis,
  Document,
  DocumentAdd,
  Files,
  Grid,
  Histogram,
  Monitor,
  Operation,
  TrendCharts,
  Tickets,
  User,
  SwitchButton,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, ref, type Component } from 'vue'
import ApiKeyDialog from '../shared/ApiKeyDialog.vue'
import { authState, hasAppRole, logoutCurrentPrincipal, type AppRole } from '../shared/auth'
import { applicationPageUrl, directServiceUrl, getApiKey, saveApiKey } from '../shared/storage'

interface AppLink {
  name: string
  description: string
  address: string
  href: string
  icon: Component
  tone: string
  external?: boolean
  requiredRole: AppRole
}

const businessApps: AppLink[] = [
  { name: '智能问答', description: '设备咨询', address: '/chat', href: applicationPageUrl('/chat', '8001', '/chat.html'), icon: ChatDotRound, tone: 'blue', requiredRole: 'query' },
  { name: '知识库治理', description: '文档与版本', address: '/knowledge', href: applicationPageUrl('/knowledge', '8000', '/knowledge.html'), icon: Files, tone: 'green', requiredRole: 'admin' },
  { name: '资料导入', description: '文件处理任务', address: '/import', href: applicationPageUrl('/import', '8000', '/import.html'), icon: DocumentAdd, tone: 'amber', requiredRole: 'import' },
  { name: '问答运营', description: '问答结果统计', address: '/analytics', href: applicationPageUrl('/analytics', '8001', '/analytics.html'), icon: DataAnalysis, tone: 'red', requiredRole: 'query' },
  { name: '人工处理', description: '工单与处理记录', address: '/workflow', href: applicationPageUrl('/workflow', '8002', '/workflow.html'), icon: Tickets, tone: 'neutral', requiredRole: 'workflow' },
]

const componentApps: AppLink[] = [
  { name: 'Attu', description: 'Milvus 管理', address: '默认端口 3002', href: directServiceUrl('3002'), icon: Coin, tone: 'green', external: true, requiredRole: 'admin' },
  { name: 'Langfuse', description: 'LLM 链路追踪', address: '默认端口 3000', href: directServiceUrl('3000'), icon: Operation, tone: 'blue', external: true, requiredRole: 'admin' },
  { name: 'MinIO', description: '业务对象存储', address: '默认端口 9001', href: directServiceUrl('9001'), icon: Files, tone: 'amber', external: true, requiredRole: 'admin' },
  { name: 'Grafana', description: '指标仪表盘', address: '默认端口 3001', href: directServiceUrl('3001'), icon: Histogram, tone: 'red', external: true, requiredRole: 'admin' },
  { name: 'Prometheus', description: '指标查询', address: '默认端口 9090', href: directServiceUrl('9090'), icon: TrendCharts, tone: 'blue', external: true, requiredRole: 'admin' },
  { name: 'Langfuse MinIO', description: '观测数据存储', address: '默认端口 9191', href: directServiceUrl('9191'), icon: Files, tone: 'neutral', external: true, requiredRole: 'admin' },
]

const apiApps: AppLink[] = [
  { name: '查询 API', description: '接口文档', address: '默认端口 8001', href: directServiceUrl('8001', '/docs'), icon: Document, tone: 'blue', external: true, requiredRole: 'admin' },
  { name: '导入 API', description: '接口文档', address: '默认端口 8000', href: directServiceUrl('8000', '/docs'), icon: Document, tone: 'amber', external: true, requiredRole: 'admin' },
  { name: '工作流 API', description: '接口文档', address: '默认端口 8002', href: directServiceUrl('8002', '/docs'), icon: Document, tone: 'green', external: true, requiredRole: 'admin' },
]

const visibleBusinessApps = computed(() => businessApps.filter((item) => hasAppRole(item.requiredRole)))
const visibleComponentApps = computed(() => componentApps.filter((item) => hasAppRole(item.requiredRole)))
const visibleApiApps = computed(() => apiApps.filter((item) => hasAppRole(item.requiredRole)))
const visibleEntryCount = computed(() => (
  visibleBusinessApps.value.length + visibleComponentApps.value.length + visibleApiApps.value.length
))
const homeUrl = computed(() => visibleBusinessApps.value[0]?.href || '#')
const settingsVisible = ref(false)
const apiKey = ref(getApiKey())
const identityName = computed(() => authState.principal?.authenticated
  ? (authState.principal.email || authState.principal.key_id)
  : '本地开发')
const tenantName = computed(() => authState.principal?.tenant_id || '未连接')
const identityTitle = computed(() => {
  const principal = authState.principal
  if (!principal) return '未连接'
  return `${principal.tenant_id} / ${principal.key_id} / ${principal.roles.join(', ')}`
})

function saveSettings(value: string): void {
  saveApiKey(value)
  apiKey.value = value.trim()
}

async function logout(): Promise<void> {
  try {
    await logoutCurrentPrincipal()
    window.location.href = '/login'
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  }
}
</script>

<template>
  <div class="apps-page">
    <header class="apps-header">
      <a class="apps-brand" :href="homeUrl">
        <span class="brand-mark">EA</span>
        <span class="brand-copy"><strong>设备知识助手</strong><span>应用与组件中心</span></span>
      </a>
      <div class="apps-actions">
        <span class="identity-chip" :title="identityTitle">
          <el-icon><User /></el-icon>
          <span class="identity-copy"><strong>{{ identityName }}</strong><small>{{ tenantName }}</small></span>
        </span>
        <a v-if="hasAppRole('query')" class="top-button" :href="applicationPageUrl('/chat', '8001', '/chat.html')">
          <el-icon><ChatDotRound /></el-icon><span class="desktop-label">进入问答</span>
        </a>
        <button v-if="authState.principal?.auth_type !== 'password'" class="top-button" type="button" title="连接设置" aria-label="连接设置" @click="settingsVisible = true">
          <el-icon><Connection /></el-icon><span class="desktop-label">连接设置</span>
        </button>
        <button v-else class="top-button" type="button" title="退出登录" aria-label="退出登录" @click="logout">
          <el-icon><SwitchButton /></el-icon><span class="desktop-label">退出</span>
        </button>
      </div>
    </header>

    <main class="apps-main">
      <section class="apps-titlebar">
        <div>
          <div class="eyebrow">Application Hub</div>
          <h1>应用与组件</h1>
        </div>
        <div class="hub-summary"><el-icon><Grid /></el-icon><span>{{ visibleEntryCount }} 个入口</span></div>
      </section>

      <section class="link-section">
        <div class="section-heading"><div><h2>业务应用</h2><p>设备问答与知识库</p></div><span>核心服务</span></div>
        <div class="business-grid">
          <a v-for="item in visibleBusinessApps" :key="item.name" class="business-link" :href="item.href">
            <span class="link-icon" :class="item.tone"><el-icon><component :is="item.icon" /></el-icon></span>
            <span class="link-copy"><strong>{{ item.name }}</strong><small>{{ item.description }}</small></span>
            <code>{{ item.address }}</code>
          </a>
        </div>
      </section>

      <section v-if="visibleComponentApps.length" class="link-section">
        <div class="section-heading"><div><h2>系统组件</h2><p>存储、检索与可观测性</p></div><span>独立服务</span></div>
        <div class="component-grid">
          <a
            v-for="item in visibleComponentApps"
            :key="item.name"
            class="component-link"
            :href="item.href"
            target="_blank"
            rel="noreferrer"
          >
            <span class="link-icon" :class="item.tone"><el-icon><component :is="item.icon" /></el-icon></span>
            <span class="link-copy"><strong>{{ item.name }}</strong><small>{{ item.description }}</small></span>
            <span class="address">{{ item.address }}</span>
            <el-icon class="open-icon"><Monitor /></el-icon>
          </a>
        </div>
      </section>

      <section v-if="visibleApiApps.length" class="link-section api-section">
        <div class="section-heading"><div><h2>API 文档</h2><p>服务接口与调试</p></div><span>Swagger UI</span></div>
        <div class="api-links">
          <a v-for="item in visibleApiApps" :key="item.name" :href="item.href" target="_blank" rel="noreferrer">
            <span><el-icon><component :is="item.icon" /></el-icon><strong>{{ item.name }}</strong></span>
            <small>{{ item.address }}</small>
          </a>
        </div>
      </section>
    </main>
    <ApiKeyDialog v-model="settingsVisible" :api-key="apiKey" @save="saveSettings" />
  </div>
</template>
