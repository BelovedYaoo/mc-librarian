import path from 'node:path';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// ----------------------------------------------------------------------

const PORT = 5173;

/** 本地开发时后端地址；`bun run dev` 会把它注入到环境变量里。 */
const API_URL = process.env.MC_LIBRARIAN_API_URL ?? 'http://127.0.0.1:8787';

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      {
        find: /^src(.+)/,
        replacement: path.resolve(import.meta.dirname, 'src/$1'),
      },
    ],
  },
  server: {
    port: PORT,
    strictPort: true,
    host: '127.0.0.1',
    // 与生产同构：前端只认 `/api`，由反向代理（开发期是 vite）转到后端 `/v1/*`。
    proxy: {
      '/api': {
        target: API_URL,
        changeOrigin: true,
        rewrite: (requestPath) => requestPath.replace(/^\/api/, ''),
      },
    },
  },
  preview: { port: PORT, strictPort: true, host: '127.0.0.1' },
});
