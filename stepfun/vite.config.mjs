import { defineConfig } from "vite";

export default defineConfig({
  root: "web",
  base: "/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2022",
    chunkSizeWarningLimit: 4096,
  },
  server: {
    port: Number(process.env.WEB_PORT ?? 5173),
    proxy: {
      "/api": "http://127.0.0.1:8787",
      "/models": "http://127.0.0.1:8787",
      "/ort": "http://127.0.0.1:8787",
      "/corpus": "http://127.0.0.1:8787",
    },
  },
  // onnxruntime-web 自带 wasm 加载逻辑，交给浏览器原生 ESM 处理更稳
  optimizeDeps: {
    exclude: ["@huggingface/transformers"],
  },
});
