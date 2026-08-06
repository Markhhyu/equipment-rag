<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { Delete, Link, Lock } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { apiFetch } from '../shared/api'

interface FeishuSettings {
  enabled: boolean
  app_id: string
  approval_code: string
  initiator_user_id: string
  user_id_type: 'open_id' | 'user_id' | 'union_id'
  base_url: string
  timeout_seconds: number
  secret_configured: boolean
  source: string
  updated_at: string | null
}

const props = defineProps<{ modelValue: boolean; apiKey: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const deleting = ref(false)
const secretConfigured = ref(false)
const source = ref('default')
const draft = reactive({
  enabled: false,
  app_id: '',
  app_secret: '',
  approval_code: '',
  initiator_user_id: '',
  user_id_type: 'open_id' as FeishuSettings['user_id_type'],
  base_url: 'https://open.feishu.cn',
  timeout_seconds: 10,
})

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const response = await apiFetch('/workflow/connectors/feishu', props.apiKey)
    const settings = await response.json() as FeishuSettings
    draft.enabled = settings.enabled
    draft.app_id = settings.app_id || ''
    draft.app_secret = ''
    draft.approval_code = settings.approval_code || ''
    draft.initiator_user_id = settings.initiator_user_id || ''
    draft.user_id_type = settings.user_id_type || 'open_id'
    draft.base_url = settings.base_url || 'https://open.feishu.cn'
    draft.timeout_seconds = settings.timeout_seconds || 10
    secretConfigured.value = settings.secret_configured
    source.value = settings.source
  } catch (error) {
    ElMessage.error(`飞书配置加载失败：${errorText(error)}`)
  } finally {
    loading.value = false
  }
}

function requestBody(): Record<string, unknown> {
  return {
    ...draft,
    app_id: draft.app_id.trim(),
    app_secret: draft.app_secret.trim(),
    approval_code: draft.approval_code.trim(),
    initiator_user_id: draft.initiator_user_id.trim(),
    base_url: draft.base_url.trim(),
  }
}

async function save(closeAfter = true): Promise<boolean> {
  saving.value = true
  try {
    const response = await apiFetch('/workflow/connectors/feishu', props.apiKey, {
      method: 'PUT',
      body: JSON.stringify(requestBody()),
    }, true)
    const settings = await response.json() as FeishuSettings
    draft.app_secret = ''
    secretConfigured.value = settings.secret_configured
    source.value = settings.source
    ElMessage.success('飞书配置已保存')
    if (closeAfter) emit('update:modelValue', false)
    return true
  } catch (error) {
    ElMessage.error(`飞书配置保存失败：${errorText(error)}`)
    return false
  } finally {
    saving.value = false
  }
}

async function saveAndTest(): Promise<void> {
  if (!await save(false)) return
  testing.value = true
  try {
    const response = await apiFetch('/workflow/connectors/feishu/test', props.apiKey, { method: 'POST' })
    const result = await response.json() as { approval_name?: string }
    ElMessage.success(result.approval_name ? `连接成功：${result.approval_name}` : '飞书连接测试通过')
  } catch (error) {
    ElMessage.error(`飞书连接测试失败：${errorText(error)}`)
  } finally {
    testing.value = false
  }
}

async function clearConfig(): Promise<void> {
  deleting.value = true
  try {
    await apiFetch('/workflow/connectors/feishu', props.apiKey, { method: 'DELETE' })
    Object.assign(draft, {
      enabled: false, app_id: '', app_secret: '', approval_code: '', initiator_user_id: '',
      user_id_type: 'open_id', base_url: 'https://open.feishu.cn', timeout_seconds: 10,
    })
    secretConfigured.value = false
    source.value = 'default'
    ElMessage.success('飞书配置已清除')
  } catch (error) {
    ElMessage.error(`飞书配置清除失败：${errorText(error)}`)
  } finally {
    deleting.value = false
  }
}

watch(() => props.modelValue, (opened) => {
  if (opened) void load()
})
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    width="min(680px, calc(100vw - 28px))"
    title="飞书审批设置"
    destroy-on-close
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div v-loading="loading" class="feishu-config">
      <div class="config-status">
        <span><el-icon><Link /></el-icon>飞书审批</span>
        <el-switch v-model="draft.enabled" inline-prompt active-text="启用" inactive-text="停用" />
      </div>

      <div class="config-grid">
        <label><span>App ID</span><el-input v-model="draft.app_id" autocomplete="off" /></label>
        <label>
          <span>App Secret <i :class="{ ready: secretConfigured }">{{ secretConfigured ? '已保存' : '未配置' }}</i></span>
          <el-input v-model="draft.app_secret" type="password" show-password autocomplete="new-password" placeholder="留空则保持现有密钥" />
        </label>
        <label><span>当前默认审批 Code</span><el-input v-model="draft.approval_code" autocomplete="off" /></label>
        <label class="initiator-field">
          <span>发起人</span>
          <div><el-select v-model="draft.user_id_type"><el-option label="Open ID" value="open_id" /><el-option label="User ID" value="user_id" /><el-option label="Union ID" value="union_id" /></el-select><el-input v-model="draft.initiator_user_id" autocomplete="off" /></div>
        </label>
      </div>

      <div class="advanced-grid">
        <label><span>开放平台地址</span><el-input v-model="draft.base_url" autocomplete="off" /></label>
        <label><span>请求超时（秒）</span><el-input-number v-model="draft.timeout_seconds" :min="1" :max="120" /></label>
      </div>
    </div>

    <template #footer>
      <div class="config-footer">
        <el-popconfirm title="清除当前租户的飞书配置？" confirm-button-text="清除" cancel-button-text="取消" @confirm="clearConfig">
          <template #reference><el-button type="danger" plain :loading="deleting" :icon="Delete">清除配置</el-button></template>
        </el-popconfirm>
        <span />
        <el-button @click="emit('update:modelValue', false)">取消</el-button>
        <el-button :loading="saving" @click="save()">保存</el-button>
        <el-button type="primary" :loading="testing || saving" :icon="Lock" @click="saveAndTest">保存并测试</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.feishu-config { min-height: 260px; }
.config-status { height: 48px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #e5e8ed; }
.config-status > span { display: flex; align-items: center; gap: 7px; color: #344054; font-weight: 700; }
.config-grid { padding: 18px 0; display: grid; grid-template-columns: 1fr 1fr; gap: 15px 18px; }
label > span { margin-bottom: 6px; display: flex; align-items: center; justify-content: space-between; color: #667085; font-size: 11px; }
label i { color: #a45d12; font-size: 9px; font-style: normal; }.ready { color: #14785a; }
.initiator-field > div { display: grid; grid-template-columns: 110px 1fr; gap: 7px; }
.advanced-grid { padding-top: 2px; display: grid; grid-template-columns: 1fr 180px; gap: 18px; }
.config-footer { width: 100%; display: flex; align-items: center; gap: 8px; }.config-footer > span { flex: 1; }
@media (max-width: 720px) {
  .config-grid, .advanced-grid { grid-template-columns: 1fr; }
  .config-footer { flex-wrap: wrap; }.config-footer > span { display: none; }.config-footer .el-button { flex: 1; margin-left: 0; }
}
</style>
