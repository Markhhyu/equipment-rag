<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  ChatDotRound,
  CircleCheck,
  CircleClose,
  Connection,
  DataAnalysis,
  Refresh,
  Setting,
  Timer,
  TrendCharts,
  Warning,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import ApiKeyDialog from '../shared/ApiKeyDialog.vue'
import { apiFetch } from '../shared/api'
import { getApiKey, saveApiKey } from '../shared/storage'

interface Totals {
  questions: number
  technical_succeeded: number
  technical_failed: number
  technical_in_progress: number
  solved: number
  partial: number
  unsolved: number
  pending_confirmation: number
  requires_human_review: number
  positive_feedback: number
  negative_feedback: number
}

interface TrendPoint {
  date: string
  questions: number
  technical_succeeded: number
  technical_failed: number
  solved: number
  partial: number
  unsolved: number
}

interface AttentionItem {
  trace_id: string
  session_id: string
  question: string
  technical_status: string
  resolution_status: string
  requires_human_review: boolean
  review_reason: string
  device_names: string[]
  started_at: string
}

interface AnalyticsSummary {
  generated_at: string
  range: { days: number; start_date: string; end_date: string }
  totals: Totals
  rates: {
    technical_success_rate: number
    confirmed_resolution_rate: number
    outcome_confirmation_rate: number
  }
  trend: TrendPoint[]
  top_devices: Array<{ name: string; count: number }>
  failure_reasons: Array<{ reason: string; count: number }>
  recent_attention: AttentionItem[]
}

const emptySummary: AnalyticsSummary = {
  generated_at: '',
  range: { days: 7, start_date: '', end_date: '' },
  totals: {
    questions: 0,
    technical_succeeded: 0,
    technical_failed: 0,
    technical_in_progress: 0,
    solved: 0,
    partial: 0,
    unsolved: 0,
    pending_confirmation: 0,
    requires_human_review: 0,
    positive_feedback: 0,
    negative_feedback: 0,
  },
  rates: { technical_success_rate: 0, confirmed_resolution_rate: 0, outcome_confirmation_rate: 0 },
  trend: [],
  top_devices: [],
  failure_reasons: [],
  recent_attention: [],
}

const apiKey = ref(getApiKey())
const settingsVisible = ref(false)
const loading = ref(false)
const selectedDays = ref(7)
const summary = ref<AnalyticsSummary>(emptySummary)

const maxTrendValue = computed(() => Math.max(1, ...summary.value.trend.map((item) => item.questions)))
const confirmedTotal = computed(() => summary.value.totals.solved + summary.value.totals.partial + summary.value.totals.unsolved)
const rangeLabel = computed(() => {
  if (!summary.value.range.start_date) return '暂无统计区间'
  return `${summary.value.range.start_date} 至 ${summary.value.range.end_date}`
})
const generatedLabel = computed(() => summary.value.generated_at
  ? new Date(summary.value.generated_at).toLocaleString('zh-CN', { hour12: false })
  : '--')

function percent(value: number): string {
  return `${(value * 100).toFixed(1)}%`
}

function trendHeight(value: number): string {
  if (!value) return '3px'
  return `${Math.max(8, Math.round((value / maxTrendValue.value) * 100))}%`
}

function dayLabel(value: string): string {
  const date = new Date(`${value}T00:00:00`)
  return `${date.getMonth() + 1}/${date.getDate()}`
}

function dateTime(value: string): string {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '--'
}

function statusLabel(item: AttentionItem): string {
  if (item.technical_status === 'failed') return '运行失败'
  if (item.resolution_status === 'unsolved') return '未解决'
  if (item.resolution_status === 'partial') return '部分解决'
  if (item.requires_human_review) return '人工复核'
  return '待关注'
}

function statusClass(item: AttentionItem): string {
  if (item.technical_status === 'failed' || item.resolution_status === 'unsolved') return 'danger'
  return 'warning'
}

async function loadSummary(): Promise<void> {
  loading.value = true
  try {
    const timezoneOffset = -new Date().getTimezoneOffset()
    const response = await apiFetch(
      `/analytics/summary?days=${selectedDays.value}&timezone_offset_minutes=${timezoneOffset}`,
      apiKey.value,
    )
    summary.value = await response.json() as AnalyticsSummary
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    if (/missing api key|invalid api key|401/i.test(message)) settingsVisible.value = true
    else ElMessage.error(`运营数据加载失败：${message}`)
  } finally {
    loading.value = false
  }
}

async function selectRange(days: number): Promise<void> {
  if (selectedDays.value === days) return
  selectedDays.value = days
  await loadSummary()
}

async function saveSettings(value: string): Promise<void> {
  saveApiKey(value)
  apiKey.value = value
  await loadSummary()
  ElMessage.success('连接设置已保存')
}

onMounted(loadSummary)
</script>

<template>
  <div class="app-frame analytics-page">
    <header class="analytics-header">
      <a class="analytics-brand" href="/chat.html">
        <span class="brand-mark">EA</span>
        <span class="brand-copy"><strong>设备知识助手</strong><span>问答运营</span></span>
      </a>
      <nav class="analytics-nav">
        <a class="top-button" href="/chat.html"><el-icon><ChatDotRound /></el-icon><span class="desktop-label">返回问答</span></a>
        <button class="top-button" @click="settingsVisible = true"><el-icon><Connection /></el-icon><span class="desktop-label">API 设置</span></button>
      </nav>
    </header>

    <main class="analytics-main" v-loading="loading">
      <section class="analytics-titlebar">
        <div>
          <div class="eyebrow">Operations</div>
          <h1>问答运营看板</h1>
          <p>{{ rangeLabel }} · 更新于 {{ generatedLabel }}</p>
        </div>
        <div class="analytics-filters">
          <div class="range-control" aria-label="统计时间范围">
            <button v-for="days in [7, 30, 90]" :key="days" :class="{ active: selectedDays === days }" @click="selectRange(days)">
              {{ days }} 天
            </button>
          </div>
          <button class="icon-command" title="刷新数据" @click="loadSummary"><el-icon><Refresh /></el-icon></button>
        </div>
      </section>

      <section class="kpi-grid">
        <article class="kpi-item primary">
          <span><el-icon><DataAnalysis /></el-icon>问答总量</span><strong>{{ summary.totals.questions }}</strong><small>统计区间内创建的问答</small>
        </article>
        <article class="kpi-item success">
          <span><el-icon><CircleCheck /></el-icon>技术成功</span><strong>{{ percent(summary.rates.technical_success_rate) }}</strong><small>{{ summary.totals.technical_succeeded }} 次正常完成</small>
        </article>
        <article class="kpi-item danger">
          <span><el-icon><CircleClose /></el-icon>技术失败</span><strong>{{ summary.totals.technical_failed }}</strong><small>异常或重试耗尽</small>
        </article>
        <article class="kpi-item resolved">
          <span><el-icon><TrendCharts /></el-icon>确认解决</span><strong>{{ summary.totals.solved }}</strong><small>确认结果中解决率 {{ percent(summary.rates.confirmed_resolution_rate) }}</small>
        </article>
        <article class="kpi-item warning">
          <span><el-icon><Warning /></el-icon>未解决</span><strong>{{ summary.totals.unsolved }}</strong><small>另有 {{ summary.totals.partial }} 次部分解决</small>
        </article>
        <article class="kpi-item neutral">
          <span><el-icon><Timer /></el-icon>待确认</span><strong>{{ summary.totals.pending_confirmation }}</strong><small>结果确认率 {{ percent(summary.rates.outcome_confirmation_rate) }}</small>
        </article>
      </section>

      <section class="analytics-section trend-section">
        <div class="section-heading">
          <div><h2>每日问答趋势</h2><p>问答量与用户确认结果</p></div>
          <div class="chart-legend"><span class="questions">问答</span><span class="solved">解决</span><span class="unsolved">未解决</span></div>
        </div>
        <div v-if="summary.trend.length" class="trend-scroll">
          <div class="trend-chart" :style="{ minWidth: `${Math.max(680, summary.trend.length * 34)}px` }">
            <div v-for="point in summary.trend" :key="point.date" class="trend-column">
              <div class="trend-bars">
                <span class="bar questions" :style="{ height: trendHeight(point.questions) }" :title="`${point.questions} 次问答`" />
                <span class="bar solved" :style="{ height: trendHeight(point.solved) }" :title="`${point.solved} 次解决`" />
                <span class="bar unsolved" :style="{ height: trendHeight(point.unsolved) }" :title="`${point.unsolved} 次未解决`" />
              </div>
              <strong>{{ point.questions }}</strong><small>{{ dayLabel(point.date) }}</small>
            </div>
          </div>
        </div>
        <div v-else class="empty-state">当前区间暂无问答记录</div>
      </section>

      <section class="analytics-section outcome-section">
        <div class="section-heading">
          <div><h2>确认结果分布</h2><p>{{ confirmedTotal }} 次问答已有明确结果</p></div>
          <strong>{{ percent(summary.rates.outcome_confirmation_rate) }}</strong>
        </div>
        <div class="outcome-track" :class="{ empty: confirmedTotal === 0 }">
          <span class="solved" :style="{ width: `${confirmedTotal ? summary.totals.solved / confirmedTotal * 100 : 0}%` }" />
          <span class="partial" :style="{ width: `${confirmedTotal ? summary.totals.partial / confirmedTotal * 100 : 0}%` }" />
          <span class="unsolved" :style="{ width: `${confirmedTotal ? summary.totals.unsolved / confirmedTotal * 100 : 0}%` }" />
        </div>
        <div class="outcome-values">
          <span><i class="solved" />已解决 <strong>{{ summary.totals.solved }}</strong></span>
          <span><i class="partial" />部分解决 <strong>{{ summary.totals.partial }}</strong></span>
          <span><i class="unsolved" />未解决 <strong>{{ summary.totals.unsolved }}</strong></span>
          <span><i class="pending" />待确认 <strong>{{ summary.totals.pending_confirmation }}</strong></span>
          <span><i class="review" />人工复核 <strong>{{ summary.totals.requires_human_review }}</strong></span>
        </div>
      </section>

      <section class="analytics-split">
        <div class="analytics-section compact-section">
          <div class="section-heading"><div><h2>高频设备</h2><p>按问答关联设备统计</p></div></div>
          <div v-if="summary.top_devices.length" class="ranking-list">
            <div v-for="(item, index) in summary.top_devices" :key="item.name">
              <span>{{ index + 1 }}</span><strong>{{ item.name }}</strong><em>{{ item.count }}</em>
            </div>
          </div>
          <div v-else class="empty-state compact">暂无设备归类数据</div>
        </div>
        <div class="analytics-section compact-section">
          <div class="section-heading"><div><h2>技术失败原因</h2><p>仅统计执行异常，不包含业务未解决</p></div></div>
          <div v-if="summary.failure_reasons.length" class="failure-list">
            <div v-for="item in summary.failure_reasons" :key="item.reason"><strong>{{ item.reason }}</strong><span>{{ item.count }}</span></div>
          </div>
          <div v-else class="empty-state compact">当前区间没有技术失败</div>
        </div>
      </section>

      <section class="analytics-section attention-section">
        <div class="section-heading"><div><h2>最近待关注问答</h2><p>运行失败、未解决、部分解决或需要人工复核</p></div></div>
        <div v-if="summary.recent_attention.length" class="attention-table-wrap">
          <table class="attention-table">
            <thead><tr><th>时间</th><th>问题</th><th>设备</th><th>状态</th><th>Trace ID</th></tr></thead>
            <tbody>
              <tr v-for="item in summary.recent_attention" :key="item.trace_id">
                <td>{{ dateTime(item.started_at) }}</td>
                <td><strong>{{ item.question || '图片问答' }}</strong><small v-if="item.review_reason">{{ item.review_reason }}</small></td>
                <td>{{ item.device_names.join('、') || '--' }}</td>
                <td><span class="status-chip" :class="statusClass(item)">{{ statusLabel(item) }}</span></td>
                <td><code>{{ item.trace_id.slice(0, 10) }}</code></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="empty-state">当前区间没有待关注问答</div>
      </section>
    </main>

    <ApiKeyDialog v-model="settingsVisible" :api-key="apiKey" @save="saveSettings" />
  </div>
</template>
