<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, type Component } from 'vue'
import { Connection, Lock, Warning } from '@element-plus/icons-vue'
import ApiKeyDialog from './ApiKeyDialog.vue'
import { authState, hasAppRole, refreshCurrentPrincipal, type AppRole } from './auth'
import { API_KEY_CHANGED_EVENT, getApiKey, saveApiKey } from './storage'

const props = defineProps<{
  appComponent: Component
  requiredRole?: AppRole
}>()

const settingsVisible = ref(false)
const apiKey = ref(getApiKey())
const allowed = computed(() => Boolean(
  authState.principal && (!props.requiredRole || hasAppRole(props.requiredRole)),
))
const forbidden = computed(() => Boolean(authState.principal && !allowed.value))

async function refreshAccess(): Promise<void> {
  apiKey.value = getApiKey()
  await refreshCurrentPrincipal()
}

function updateApiKey(value: string): void {
  saveApiKey(value)
  apiKey.value = value.trim()
}

function handleApiKeyChange(): void {
  void refreshAccess()
}

onMounted(() => {
  window.addEventListener(API_KEY_CHANGED_EVENT, handleApiKeyChange)
  void refreshAccess()
})

onBeforeUnmount(() => window.removeEventListener(API_KEY_CHANGED_EVENT, handleApiKeyChange))
</script>

<template>
  <component :is="appComponent" v-if="allowed" />
  <main v-else class="access-page">
    <section class="access-panel" aria-live="polite">
      <span class="access-mark"><el-icon><Lock v-if="!authState.loading" /><Connection v-else /></el-icon></span>
      <template v-if="authState.loading">
        <h1>正在验证访问权限</h1>
        <p>正在读取当前租户和角色。</p>
      </template>
      <template v-else-if="forbidden">
        <h1>没有页面访问权限</h1>
        <p>当前身份不具备此模块所需角色。</p>
      </template>
      <template v-else>
        <h1>需要连接凭据</h1>
        <p>{{ authState.error || '请输入有效的 API Key 后继续。' }}</p>
      </template>
      <button v-if="!authState.loading" type="button" @click="settingsVisible = true">
        <el-icon><Warning v-if="forbidden" /><Connection v-else /></el-icon>
        更换连接凭据
      </button>
    </section>
    <ApiKeyDialog v-model="settingsVisible" :api-key="apiKey" @save="updateApiKey" />
  </main>
</template>

<style scoped>
.access-page { min-height: 100vh; padding: 24px; display: grid; place-items: center; background: #f4f6f8; }
.access-panel { width: min(420px, 100%); padding: 30px; border: 1px solid #dfe3e8; border-radius: 8px; text-align: center; background: #fff; box-shadow: 0 12px 32px rgb(16 24 40 / 8%); }
.access-mark { width: 46px; height: 46px; margin: 0 auto 18px; display: grid; place-items: center; border-radius: 8px; color: #245bdb; font-size: 22px; background: #edf3ff; }
.access-panel h1 { margin: 0; color: #1f2937; font-size: 20px; letter-spacing: 0; }
.access-panel p { min-height: 40px; margin: 10px 0 20px; color: #667085; font-size: 13px; line-height: 1.6; }
.access-panel button { min-height: 38px; padding: 0 15px; display: inline-flex; align-items: center; gap: 7px; border: 1px solid #cfd6df; border-radius: 6px; color: #344054; background: #fff; cursor: pointer; }
</style>
