import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/dates": "http://127.0.0.1:8000",
      "/predictions": "http://127.0.0.1:8000",
      "/run": "http://127.0.0.1:8000",
    },
  },
});
