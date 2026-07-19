import { defineConfig } from 'vite'
import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The VOD server serves manifests + fMP4 segments over plain HTTP. We proxy the
// media routes to it so the browser only ever talks to the Vite dev origin — the
// backend's CORS layer is then irrelevant for local dev.
//
// Backend default port is 8080 (project-11 `PORT`). Override the whole URL with
// VOD_URL=http://host:port when starting `bun run dev`.
const target = process.env.VOD_URL || 'http://localhost:8080'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5111,
    proxy: {
      '/vod': { target, changeOrigin: true },
      '/assets': { target, changeOrigin: true },
      '/healthz': { target, changeOrigin: true },
    },
  },
})
