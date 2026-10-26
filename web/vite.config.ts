import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// `npm run dev` serves the page with hot reload and proxies the API to a running `clipit --dashboard`.
const api = `http://127.0.0.1:${process.env.CLIPIT_PORT || 8765}`;
const dev = "http://localhost:5173";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    host: "localhost",
    port: 5173,
    strictPort: true,
    proxy: Object.fromEntries(
      ["/api", "/media"].map((path) => [
        path,
        {
          target: api,
          changeOrigin: true, // Host: the server only answers its own
          configure: (proxy) =>
            // writes must come from the server's own Origin: vouch only for this dev page, so other sites stay refused
            proxy.on("proxyReq", (req) => req.getHeader("origin") === dev && req.setHeader("origin", api)),
        },
      ]),
    ),
  },
  build: {
    outDir: "../clipit/web", // committed, so `pip install` needs no Node; CI checks it matches src/
    emptyOutDir: true,
    modulePreload: { polyfill: false },
    chunkSizeWarningLimit: 900, // one bundle on purpose: served from this PC, so size barely matters and fixed names do
    rolldownOptions: {
      // fixed names: dashboard.py serves exactly these files, and a rebuild diffs cleanly
      output: { entryFileNames: "assets/app.js", chunkFileNames: "assets/[name].js", assetFileNames: "assets/app[extname]" },
    },
  },
  test: { environment: "node" },
});
