/// <reference types="vitest" />
import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default function config() {
  const allowedHosts = (process.env.VITE_ALLOWED_HOSTS ?? "")
    .split(",")
    .map((host) => host.trim())
    .filter(Boolean);
  const apiProxy = {
    "/api": {
      target: process.env.VITE_API_TARGET ?? "http://api:8000",
      changeOrigin: true,
    },
  };

  return defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    // 反代域名（如 sslip.io）需显式放行；逗号分隔，空值=仅默认主机。
    allowedHosts,
    proxy: apiProxy,
  },
  //: 生产用 `vite build` 的产物 + `vite preview` 静态服务（开发服务器会按需转换、
  //: 依赖预构建会中途作废响应：实测 caddy 报 "aborting with incomplete response"，
  //: iPhone/微信真机拿不到 JS 分片 → 白屏"打不开"）。preview 需要自己的一份配置。
  preview: {
    host: "0.0.0.0",
    port: 5173,
    allowedHosts,
    proxy: apiProxy,
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          antd: ["antd", "@ant-design/icons"],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    // antd form/step transitions settle asynchronously in jsdom and each
    // flushed interaction can take a few seconds; keep a generous ceiling.
    testTimeout: 60000,
  },
});
}
