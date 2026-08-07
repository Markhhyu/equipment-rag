<script setup lang="ts">
import { Hide, Lock, Message, View } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  authState,
  loadAuthConfig,
  loginWithPassword,
  refreshCurrentPrincipal,
  registerWithPassword,
} from '../shared/auth'

const route = useRoute()
const router = useRouter()
const email = ref('')
const password = ref('')
const confirmPassword = ref('')
const passwordVisible = ref(false)
const submitting = ref(false)
const loading = ref(true)
const isRegister = computed(() => route.path === '/register')
const registrationEnabled = computed(() => Boolean(authState.config?.registration_enabled))
const formComplete = computed(() => Boolean(
  email.value.trim()
  && password.value
  && (!isRegister.value || confirmPassword.value),
))

function safeRedirect(): string {
  const value = typeof route.query.redirect === 'string' ? route.query.redirect : '/apps'
  if (!value.startsWith('/') || value.startsWith('//') || value.startsWith('/login') || value.startsWith('/register')) {
    return '/apps'
  }
  return value
}

async function submit(): Promise<void> {
  if (isRegister.value && password.value !== confirmPassword.value) {
    ElMessage.error('两次输入的密码不一致')
    return
  }
  submitting.value = true
  try {
    if (isRegister.value) await registerWithPassword(email.value, password.value)
    else await loginWithPassword(email.value, password.value)
    await router.replace(safeRedirect())
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  try {
    const config = await loadAuthConfig()
    if (!config.password_login_enabled) {
      ElMessage.warning('当前部署未启用邮箱账号登录')
    } else if (await refreshCurrentPrincipal()) {
      await router.replace(safeRedirect())
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <main class="auth-page" v-loading="loading">
    <section class="auth-panel">
      <a class="auth-brand" href="/apps">
        <span class="brand-mark">EA</span>
        <span class="brand-copy"><strong>设备知识助手</strong><span>Equipment Assistant</span></span>
      </a>

      <div class="auth-heading">
        <span class="auth-heading-icon"><el-icon><Lock /></el-icon></span>
        <h1>{{ isRegister ? '创建账号' : '账号登录' }}</h1>
        <p>{{ isRegister ? '注册后即可使用智能问答' : '登录设备知识工作台' }}</p>
      </div>

      <form v-if="!isRegister || registrationEnabled" class="auth-form" @submit.prevent="submit">
        <label>
          <span>邮箱</span>
          <el-input v-model="email" size="large" type="email" autocomplete="email" placeholder="name@example.com">
            <template #prefix><el-icon><Message /></el-icon></template>
          </el-input>
        </label>
        <label>
          <span>密码</span>
          <el-input
            v-model="password"
            size="large"
            :type="passwordVisible ? 'text' : 'password'"
            :autocomplete="isRegister ? 'new-password' : 'current-password'"
            placeholder="至少 10 个字符"
          >
            <template #prefix><el-icon><Lock /></el-icon></template>
            <template #suffix>
              <el-icon class="password-toggle" @click="passwordVisible = !passwordVisible">
                <View v-if="!passwordVisible" /><Hide v-else />
              </el-icon>
            </template>
          </el-input>
        </label>
        <label v-if="isRegister">
          <span>确认密码</span>
          <el-input v-model="confirmPassword" size="large" type="password" autocomplete="new-password" placeholder="再次输入密码">
            <template #prefix><el-icon><Lock /></el-icon></template>
          </el-input>
        </label>
        <el-button class="auth-submit" type="primary" native-type="submit" size="large" :disabled="!formComplete" :loading="submitting">
          {{ isRegister ? '注册并登录' : '登录' }}
        </el-button>
      </form>

      <div v-else class="auth-disabled">
        <el-icon><Lock /></el-icon>
        <strong>暂未开放邮箱注册</strong>
      </div>

      <p class="auth-switch">
        <template v-if="isRegister">已有账号？<router-link :to="{ path: '/login', query: route.query }">登录</router-link></template>
        <template v-else>还没有账号？<router-link :to="{ path: '/register', query: route.query }">注册</router-link></template>
      </p>
    </section>
  </main>
</template>
