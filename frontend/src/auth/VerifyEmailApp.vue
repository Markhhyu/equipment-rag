<script setup lang="ts">
import { CircleCheck, CircleClose, Loading, Message } from '@element-plus/icons-vue'
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { verifyEmail } from '../shared/auth'

const route = useRoute()
const state = ref<'verifying' | 'success' | 'error'>('verifying')
const errorMessage = ref('')
const redirect = computed(() => {
  const value = typeof route.query.redirect === 'string' ? route.query.redirect : '/apps'
  return value.startsWith('/') && !value.startsWith('//') ? value : '/apps'
})

onMounted(async () => {
  const token = typeof route.query.token === 'string' ? route.query.token : ''
  if (!token) {
    state.value = 'error'
    errorMessage.value = '验证链接缺少令牌'
    return
  }
  try {
    await verifyEmail(token)
    state.value = 'success'
  } catch (error) {
    state.value = 'error'
    errorMessage.value = error instanceof Error ? error.message : String(error)
  }
})
</script>

<template>
  <main class="auth-page">
    <section class="auth-panel verification-panel">
      <a class="auth-brand" href="/apps">
        <span class="brand-mark">EA</span>
        <span class="brand-copy"><strong>设备知识助手</strong><span>Equipment Assistant</span></span>
      </a>

      <div class="verification-result" :class="state">
        <span class="verification-result-icon">
          <el-icon v-if="state === 'verifying'" class="is-loading"><Loading /></el-icon>
          <el-icon v-else-if="state === 'success'"><CircleCheck /></el-icon>
          <el-icon v-else><CircleClose /></el-icon>
        </span>
        <h1>{{ state === 'verifying' ? '正在验证邮箱' : state === 'success' ? '邮箱验证成功' : '邮箱验证失败' }}</h1>
        <p v-if="state === 'verifying'">请稍候</p>
        <p v-else-if="state === 'success'">账号已经激活</p>
        <p v-else>{{ errorMessage }}</p>
        <a v-if="state === 'success'" class="verification-command primary" :href="redirect">进入应用中心</a>
        <a v-else-if="state === 'error'" class="verification-command" href="/login"><el-icon><Message /></el-icon>返回登录</a>
      </div>
    </section>
  </main>
</template>
