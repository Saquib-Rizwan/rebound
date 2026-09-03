import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build straight into the package the API serves from, so `python rebound.py
// serve` is the only command a reviewer needs after a one-off `npm run build`.
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: { outDir: "../backend/rebound/api/static", emptyOutDir: true },
  server: { proxy: { "/api": { target: "http://127.0.0.1:8000", rewrite: p => p.replace(/^\/api/, "") } } },
});
