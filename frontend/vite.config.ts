import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Builds the MCPy admin dashboard into the Python package's web/dist folder
// so it ships with `pip install mcpy-proxy` and is served from /admin.
export default defineConfig({
  plugins: [react()],
  base: "/admin/static/dist/",
  build: {
    outDir: path.resolve(__dirname, "../src/mcp_proxy/web/dist"),
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
    target: "es2020",
  },
  server: {
    port: 5173,
    proxy: {
      "/admin/api": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/status": "http://127.0.0.1:8000",
    },
  },
});
