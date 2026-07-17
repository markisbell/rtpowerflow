import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies API + WebSocket to the netzsim backend so the browser
// only ever talks to the Vite origin (no CORS dance in dev).
// Use 127.0.0.1 (not "localhost") so Windows doesn't try IPv6 ::1 first, which
// uvicorn (IPv4-only by default) refuses.
// Ports come from the environment so several stacks (e.g. netzsim next to
// rtheatflow) can run in parallel: NETZSIM_PORT moves the proxy target along
// with the backend, NETZSIM_UI_PORT moves the dev server itself (Vite still
// auto-increments if that port is taken).
const BACKEND =
  process.env.NETZSIM_BACKEND ??
  `http://127.0.0.1:${process.env.NETZSIM_PORT ?? "8000"}`;
const UI_PORT = Number(process.env.NETZSIM_UI_PORT ?? "5173");

export default defineConfig({
  plugins: [react()],
  server: {
    port: UI_PORT,
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
