import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Rust binary serves the *built* app at `/` (via rust-embed) and the API
// under `/api`. In dev you run two processes — `cargo run` (backend, :8080) and
// `npm run dev` (this server, :5173) — and these proxies forward API + redirect
// traffic to the backend so the SPA stays same-origin (no CORS, relative fetch).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8080",
      "/healthz": "http://localhost:8080",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
