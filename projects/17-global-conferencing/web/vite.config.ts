import { defineConfig } from 'vite'
import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// A cascaded SFU is a mesh of regional SFUs. `bun run dev` points at ONE of them
// (its signaling HTTP port, default 8080); its `GET /rooms` reports the global
// picture the whole cluster agrees on. Media is UDP (MEDIA_PORT 7000) and the
// inter-SFU backbone is UDP (CASCADE_PORT 7100) — neither goes through Vite.
//
// Point at a specific region with CONF_URL=http://host:port bun run dev.
const target = process.env.CONF_URL || 'http://localhost:8080'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5117,
    proxy: {
      '/rooms': { target, changeOrigin: true },
      '/cluster': { target, changeOrigin: true },
      '/status': { target, changeOrigin: true },
      '/healthz': { target, changeOrigin: true },
      '/metrics': { target, changeOrigin: true },
    },
  },
})
