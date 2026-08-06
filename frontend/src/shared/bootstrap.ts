import { createApp, markRaw, type Component } from 'vue'
import ElementPlus from 'element-plus'
import ProtectedPage from './ProtectedPage.vue'
import type { AppRole } from './auth'

export function mountProtectedPage(appComponent: Component, requiredRole?: AppRole): void {
  createApp(ProtectedPage, { appComponent: markRaw(appComponent), requiredRole })
    .use(ElementPlus)
    .mount('#app')
}
