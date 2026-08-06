import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '^/auth/me$': 'http://127.0.0.1:8001',
      '^/(query|feedback|resolution)$': 'http://127.0.0.1:8001',
      '^/(attachments|history|runs|analytics|stream)/': 'http://127.0.0.1:8001',
      '^/upload$': 'http://127.0.0.1:8000',
      '^/(status|knowledge)/': 'http://127.0.0.1:8000',
      '^/workflow/': 'http://127.0.0.1:8002',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        apps: resolve(__dirname, 'apps.html'),
        chat: resolve(__dirname, 'chat.html'),
        analytics: resolve(__dirname, 'analytics.html'),
        import: resolve(__dirname, 'import.html'),
        knowledge: resolve(__dirname, 'knowledge.html'),
        workflow: resolve(__dirname, 'workflow.html'),
      },
    },
  },
})
