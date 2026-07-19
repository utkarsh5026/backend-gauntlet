import { defineConfig } from 'vite'
import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// We proxy only the *signaling* + admin HTTP API (default HTTP_PORT=8080). The
// media plane is UDP on MEDIA_PORT (default 7000): the browser ICE-connects to
// the advertised host candidate directly — that traffic does NOT go through Vite.
//
// Override with SFU_URL=http://host:port when starting `bun run dev`.
const target = process.env.SFU_URL || 'http://localhost:8080'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5115,
    proxy: {
      '/rooms': { target, changeOrigin: true },
      '/status': { target, changeOrigin: true },
      '/healthz': { target, changeOrigin: true },
      '/metrics': { target, changeOrigin: true },
    },
  },
})
