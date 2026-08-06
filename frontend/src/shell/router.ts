import { defineAsyncComponent, markRaw, type Component } from 'vue'
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import ProtectedPage from '../shared/ProtectedPage.vue'
import type { AppRole } from '../shared/auth'

function protectedRoute(
  path: string,
  loader: () => Promise<{ default: Component }>,
  requiredRole?: AppRole,
): RouteRecordRaw {
  return {
    path,
    component: ProtectedPage,
    props: {
      appComponent: markRaw(defineAsyncComponent(loader)),
      requiredRole,
    },
  }
}

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/apps' },
    protectedRoute('/apps', () => import('../apps/AppsApp.vue')),
    protectedRoute('/chat', () => import('../chat/ChatApp.vue'), 'query'),
    protectedRoute('/analytics', () => import('../analytics/AnalyticsApp.vue'), 'query'),
    protectedRoute('/import', () => import('../import/ImportApp.vue'), 'import'),
    protectedRoute('/knowledge', () => import('../knowledge/KnowledgeApp.vue'), 'admin'),
    protectedRoute('/workflow', () => import('../workflow/WorkflowApp.vue'), 'workflow'),
    { path: '/:pathMatch(.*)*', redirect: '/apps' },
  ],
})
