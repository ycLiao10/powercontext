import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * 前端构建配置。
 *
 * 关键点：`@huggingface/transformers` 必须从 Vite 的依赖预打包中排除。
 * 它在运行时用动态 import 加载 onnxruntime-web 的 `.mjs` 加载器与 `.wasm`，
 * 预打包会把这些动态 import 改写成静态 chunk，导致 WebGPU/WASM 后端选择失效。
 */
/**
 * 调试用插件：VITE_LOG_REQUESTS=1 时打印每个请求的 URL（定位模型加载路径问题）。
 */
function requestLogger() {
  return {
    name: 'stepfun-request-logger',
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        if (process.env.VITE_LOG_REQUESTS) {
          console.log('[vite-req]', req.method, req.url);
        }
        next();
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), requestLogger()],
  optimizeDeps: {
    exclude: ['@huggingface/transformers'],
  },
  worker: {
    format: 'es',
  },
  build: {
    target: 'es2022',
    sourcemap: false,
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
    strictPort: true,
  },
});
