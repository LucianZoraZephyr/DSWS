import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * rebuild 前端独立端口 5273，代理到 rebuild 后端 8100。
 * 不与旧栈的 5173 / 8000 冲突，方便新旧并排对比。
 */
export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5273,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8100",
        changeOrigin: true,
      },
    },
  },
});
