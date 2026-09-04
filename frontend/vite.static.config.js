import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Serverless build for GitHub Pages. The dashboard only ever reads, so the API
// is replaced by flat JSON written by `rebound.py export-static` and shipped
// alongside the bundle. Nothing to deploy, nothing to keep running, nothing that
// can be down when somebody clicks the link.
//
// `base: "./"` keeps every asset path relative, so the same build works whether
// it is served from a repository subpath or opened from disk.
export default defineConfig({
  plugins: [react()],
  base: "./",
  define: { "import.meta.env.VITE_STATIC": JSON.stringify("1") },
  build: { outDir: "../docs/demo", emptyOutDir: false },
});
