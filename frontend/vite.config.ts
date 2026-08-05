import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        chat: resolve(__dirname, 'chat.html'),
        analytics: resolve(__dirname, 'analytics.html'),
        import: resolve(__dirname, 'import.html'),
        knowledge: resolve(__dirname, 'knowledge.html'),
      },
    },
  },
})
