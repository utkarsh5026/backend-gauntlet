import { defineConfig } from 'vite'
import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The ingest server serves the LL-HLS media playlist + fMP4 parts/segments over
// HTTP_PORT (default 8080). RTMP ingest (1935) is a raw-TCP plane the browser
// never touches. We proxy the delivery routes so the player is same-origin.
//
// Override with LIVE_URL=http://host:port when starting `bun run dev`.
const target = process.env.LIVE_URL || 'http://localhost:8080'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5113,
    proxy: {
      '/live': { target, changeOrigin: true },
      '/healthz': { target, changeOrigin: true },
    },
  },
})
