import { defineConfig } from 'vite'
import { resolve } from 'node:path'

export default defineConfig({
  base: './',
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', ws: true, changeOrigin: false },
      '/health': 'http://127.0.0.1:8000',
    },
  },
  build: {
    rollupOptions: { input: {
      operator: resolve(import.meta.dirname, 'index.html'),
      glasses: resolve(import.meta.dirname, 'glasses.html'),
    } },
  },
})
