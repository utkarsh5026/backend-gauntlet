import { defineConfig } from 'vite'
import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The search engine speaks plain HTTP at the root (/search, /documents, /_stats…).
// We proxy everything under /api to it and strip the prefix, so the browser only
// ever talks to the Vite dev origin — no CORS layer needed on the Rust backend.
//
// Default port is 9200 (project 20 — the Elasticsearch HTTP-port convention).
// Override with SEARCH_URL=http://host:port when starting dev.
const target = process.env.SEARCH_URL || 'http://localhost:9200'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
