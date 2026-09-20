import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // start.ps1/start.sh set FPL_API_TARGET when running with a custom --port.
      "/api": process.env.FPL_API_TARGET || "http://localhost:8000",
    },
  },
});