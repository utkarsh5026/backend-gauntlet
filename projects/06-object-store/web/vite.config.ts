import { defineConfig } from 'vite'
import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The object store speaks path-style S3 over HTTP. We proxy everything under /s3
// to it and strip the prefix, so the browser only ever talks to the Vite dev
// origin — no CORS layer needed on the Rust backend.
//
// Default port is 9006 (project 06), NOT the store's built-in 9000 default:
// MinIO squats :9000, so run the backend with `PORT=9006 cargo run -p object-store`.
// Override the whole URL with OBJECT_STORE_URL=http://host:port when starting dev.
const target = process.env.OBJECT_STORE_URL || 'http://localhost:9006'

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
      '/s3': {
        target,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/s3/, ''),
      },
    },
  },
})
