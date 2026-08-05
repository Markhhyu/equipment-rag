<script setup lang="ts">
import {
  ChatDotRound,
  Coin,
  DataAnalysis,
  Document,
  DocumentAdd,
  Files,
  Grid,
  Histogram,
  Monitor,
  Operation,
  TrendCharts,
} from '@element-plus/icons-vue'
import type { Component } from 'vue'

interface AppLink {
  name: string
  description: string
  address: string
  href: string
  icon: Component
  tone: string
  external?: boolean
}

function serviceUrl(port: string, path = '/'): string {
  const url = new URL(window.location.href)
  url.port = port
  url.pathname = path
  url.search = ''
  url.hash = ''
  return url.toString()
}

const businessApps: AppLink[] = [
  { name: '智能问答', description: '设备咨询', address: '8001 / chat', href: serviceUrl('8001', '/chat.html'), icon: ChatDotRound, tone: 'blue' },
  { name: '知识库治理', description: '文档与版本', address: '8000 / knowledge', href: serviceUrl('8000', '/knowledge.html'), icon: Files, tone: 'green' },
  { name: '资料导入', description: '文件处理任务', address: '8000 / import', href: serviceUrl('8000', '/import.html'), icon: DocumentAdd, tone: 'amber' },
  { name: '问答运营', description: '问答结果统计', address: '8001 / analytics', href: serviceUrl('8001', '/analytics.html'), icon: DataAnalysis, tone: 'red' },
]

const componentApps: AppLink[] = [
  { name: 'Attu', description: 'Milvus 管理', address: '默认端口 3002', href: serviceUrl('3002'), icon: Coin, tone: 'green', external: true },
  { name: 'Langfuse', description: 'LLM 链路追踪', address: '默认端口 3000', href: serviceUrl('3000'), icon: Operation, tone: 'blue', external: true },
  { name: 'MinIO', description: '业务对象存储', address: '默认端口 9001', href: serviceUrl('9001'), icon: Files, tone: 'amber', external: true },
  { name: 'Grafana', description: '指标仪表盘', address: '默认端口 3001', href: serviceUrl('3001'), icon: Histogram, tone: 'red', external: true },
  { name: 'Prometheus', description: '指标查询', address: '默认端口 9090', href: serviceUrl('9090'), icon: TrendCharts, tone: 'blue', external: true },
  { name: 'Langfuse MinIO', description: '观测数据存储', address: '默认端口 9191', href: serviceUrl('9191'), icon: Files, tone: 'neutral', external: true },
]

const apiApps: AppLink[] = [
  { name: '查询 API', description: '接口文档', address: '默认端口 8001', href: serviceUrl('8001', '/docs'), icon: Document, tone: 'blue', external: true },
  { name: '导入 API', description: '接口文档', address: '默认端口 8000', href: serviceUrl('8000', '/docs'), icon: Document, tone: 'amber', external: true },
  { name: '工作流 API', description: '接口文档', address: '默认端口 8002', href: serviceUrl('8002', '/docs'), icon: Document, tone: 'green', external: true },
]
</script>

<template>
  <div class="apps-page">
    <header class="apps-header">
      <a class="apps-brand" :href="serviceUrl('8001', '/chat.html')">
        <span class="brand-mark">EA</span>
        <span class="brand-copy"><strong>设备知识助手</strong><span>应用与组件中心</span></span>
      </a>
      <a class="top-button" :href="serviceUrl('8001', '/chat.html')">
        <el-icon><ChatDotRound /></el-icon><span class="desktop-label">进入问答</span>
      </a>
    </header>

    <main class="apps-main">
      <section class="apps-titlebar">
        <div>
          <div class="eyebrow">Application Hub</div>
          <h1>应用与组件</h1>
        </div>
        <div class="hub-summary"><el-icon><Grid /></el-icon><span>{{ businessApps.length + componentApps.length + apiApps.length }} 个入口</span></div>
      </section>

      <section class="link-section">
        <div class="section-heading"><div><h2>业务应用</h2><p>设备问答与知识库</p></div><span>核心服务</span></div>
        <div class="business-grid">
          <a v-for="item in businessApps" :key="item.name" class="business-link" :href="item.href">
            <span class="link-icon" :class="item.tone"><el-icon><component :is="item.icon" /></el-icon></span>
            <span class="link-copy"><strong>{{ item.name }}</strong><small>{{ item.description }}</small></span>
            <code>{{ item.address }}</code>
          </a>
        </div>
      </section>

      <section class="link-section">
        <div class="section-heading"><div><h2>系统组件</h2><p>存储、检索与可观测性</p></div><span>独立服务</span></div>
        <div class="component-grid">
          <a
            v-for="item in componentApps"
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

      <section class="link-section api-section">
        <div class="section-heading"><div><h2>API 文档</h2><p>服务接口与调试</p></div><span>Swagger UI</span></div>
        <div class="api-links">
          <a v-for="item in apiApps" :key="item.name" :href="item.href" target="_blank" rel="noreferrer">
            <span><el-icon><component :is="item.icon" /></el-icon><strong>{{ item.name }}</strong></span>
            <small>{{ item.address }}</small>
          </a>
        </div>
      </section>
    </main>
  </div>
</template>
