import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built to ./dist, served by the Hub (FastAPI) at "/" on the same origin as the API,
// so all fetch() calls use relative paths. In `npm run dev`, proxy API calls to the Hub.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: {
      // dev-only: forward API calls to the Hub on 8080
      "/health": "http://localhost:8080",
      "/auth": "http://localhost:8080",
      "/sessions": "http://localhost:8080",
      "/identities": "http://localhost:8080",
      "/bookings": "http://localhost:8080",
      "/settings": "http://localhost:8080",
    },
  },
});
