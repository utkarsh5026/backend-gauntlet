import { defineConfig } from 'vite'
import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The pub/sub server speaks a tiny JSON protocol over a WebSocket at GET /ws, plus
// a GET /healthz. We proxy both to the Rust backend so the browser only ever talks
// to the Vite dev origin — no CORS, and the WebSocket upgrade is forwarded for us
// (`ws: true`). The frontend connects to a *relative* `/ws`, which lands here.
//
// Default backend is http://localhost:8080 (project 03's PORT). Point at another
// instance — e.g. the second node in a two-node V4 run — with:
//   PUBSUB_URL=http://localhost:8081 bun run dev
const target = process.env.PUBSUB_URL || 'http://localhost:8080'

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
      '/ws': { target, changeOrigin: true, ws: true },
      '/healthz': { target, changeOrigin: true },
    },
  },
})
