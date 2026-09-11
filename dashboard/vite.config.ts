import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: {
      // Local dev fetches /r2/<slug>.json same-origin; the dev server (Node,
      // not a browser -> no CORS) forwards to the real R2 public domain.
      "/r2": {
        target: "https://store-reviews.lascadian.com",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/r2/, "/fa880a0ed915c24a2a73"),
      },
    },
  },
})
