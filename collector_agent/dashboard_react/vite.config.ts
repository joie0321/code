import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api/v1/status": {
        target: "http://127.0.0.1:8443",
        changeOrigin: false,
      },
      "/api/v1/setup/register": {
        target: "http://127.0.0.1:8443",
        changeOrigin: false,
      },
      "/api/v1/setup/reconnect": {
        target: "http://127.0.0.1:8443",
        changeOrigin: false,
      },
      "/api/v1/setup/sources": {
        target: "http://127.0.0.1:8443",
        changeOrigin: false,
      },
      "/api/v1/setup/credentials": { target: "http://127.0.0.1:8443", changeOrigin: false },
      "/api/v1/setup/azure/credentials": { target: "http://127.0.0.1:8443", changeOrigin: false },
      "/api/v1/setup/azure/flow-logs": { target: "http://127.0.0.1:8443", changeOrigin: false },
      "/api/v1/setup/aws/credentials": { target: "http://127.0.0.1:8443", changeOrigin: false },
      "/api/v1/setup/aws/flow-logs": { target: "http://127.0.0.1:8443", changeOrigin: false },
      "/api/v1/collection/inventory/sync": { target: "http://127.0.0.1:8443", changeOrigin: false },
      "/api/v1/aws/flow-logs/sync": { target: "http://127.0.0.1:8443", changeOrigin: false },
      "/api/v1/azure/flow-logs/sync": { target: "http://127.0.0.1:8443", changeOrigin: false },
      "/api/v1/ipfix/setup": { target: "http://127.0.0.1:8443", changeOrigin: false },
      "/api/v1/ipfix/start": { target: "http://127.0.0.1:8443", changeOrigin: false },
      "/api/v1/ipfix/stop": { target: "http://127.0.0.1:8443", changeOrigin: false },
    },
  },
});
