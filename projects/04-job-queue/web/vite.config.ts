import { defineConfig } from 'vite'
import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The dashboard is a pure client: it talks only to endpoints the Rust job-queue
// already serves (no backend changes). We proxy them to the backend so the browser
// only ever sees the Vite dev origin — no CORS to configure on the server.
//
//   GET  /metrics          Prometheus text — depth / running / dlq / lag + counters
//   POST /jobs             enqueue (auth: Bearer ENQUEUE_TOKEN)
//   GET  /jobs/{id}        one job
//   GET  /dlq              dead-letter list
//   POST /job/{id}/requeue requeue a dead job (auth)
//   GET  /healthz          liveness
//
// Default backend is http://localhost:8080 (project 04's PORT). Point at another
// instance (e.g. a second worker process in a multi-process V1/V2 run) with:
//   JOBQUEUE_URL=http://localhost:8081 bun run dev
const target = process.env.JOBQUEUE_URL || 'http://localhost:8080'
const proxy = (path: string) => ({ [path]: { target, changeOrigin: true } })

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5273,
    proxy: {
      ...proxy('/metrics'),
      ...proxy('/jobs'),
      ...proxy('/job'),
      ...proxy('/dlq'),
      ...proxy('/healthz'),
    },
  },
})
