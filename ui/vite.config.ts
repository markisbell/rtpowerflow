import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies API + WebSocket to the netzsim backend so the browser
// only ever talks to the Vite origin (no CORS dance in dev).
// Use 127.0.0.1 (not "localhost") so Windows doesn't try IPv6 ::1 first, which
// uvicorn (IPv4-only by default) refuses.
const BACKEND = process.env.NETZSIM_BACKEND ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: BACKEND,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
      "/ws": { target: BACKEND.replace(/^http/, "ws"), ws: true },
    },
  },
});
