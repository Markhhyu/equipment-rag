<script setup lang="ts">
import { ref, watch } from 'vue'
import { Lock, View, Hide } from '@element-plus/icons-vue'

const props = defineProps<{ modelValue: boolean; apiKey: string }>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  save: [value: string]
}>()

const draft = ref('')
const visible = ref(false)

watch(() => props.modelValue, (opened) => {
  if (opened) draft.value = props.apiKey
})

function save(): void {
  emit('save', draft.value.trim())
  emit('update:modelValue', false)
}

function clearCredential(): void {
  emit('save', '')
  emit('update:modelValue', false)
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    width="min(460px, calc(100vw - 32px))"
    title="连接设置"
    destroy-on-close
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="dialog-intro">
      <span class="dialog-icon"><el-icon><Lock /></el-icon></span>
      <div>
        <strong>API Key</strong>
        <p>仅保存在当前浏览器中。开发环境关闭认证时可以留空。</p>
      </div>
    </div>
    <el-input
      v-model="draft"
      :type="visible ? 'text' : 'password'"
      size="large"
      placeholder="请输入 X-API-Key"
      autocomplete="off"
      @keyup.enter="save"
    >
      <template #suffix>
        <el-icon class="password-toggle" @click="visible = !visible">
          <View v-if="!visible" />
          <Hide v-else />
        </el-icon>
      </template>
    </el-input>
    <template #footer>
      <div class="credential-dialog-footer">
        <el-button type="danger" plain @click="clearCredential">清除凭据</el-button>
        <span>
          <el-button @click="emit('update:modelValue', false)">取消</el-button>
          <el-button type="primary" @click="save">保存设置</el-button>
        </span>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.credential-dialog-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.credential-dialog-footer > span { display: flex; gap: 8px; }

@media (max-width: 420px) {
  .credential-dialog-footer { align-items: stretch; flex-direction: column-reverse; }
  .credential-dialog-footer > span { justify-content: flex-end; }
}
</style>
