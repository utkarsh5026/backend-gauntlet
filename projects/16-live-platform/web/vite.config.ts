import { defineConfig } from 'vite'
import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The platform serves everything on one HTTP listener (default PORT=8080):
// playback (/live), chat WebSocket (/chat), ingest webhook (/ingest), status.
// We proxy them all so the watch page is same-origin — note ws:true on /chat so
// the WebSocket upgrade is forwarded.
//
// Override with PLATFORM_URL=http://host:port when starting `bun run dev`.
const target = process.env.PLATFORM_URL || 'http://localhost:8080'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5116,
    proxy: {
      '/live': { target, changeOrigin: true },
      '/chat': { target, changeOrigin: true, ws: true },
      '/ingest': { target, changeOrigin: true },
      '/status': { target, changeOrigin: true },
      '/healthz': { target, changeOrigin: true },
    },
  },
})
