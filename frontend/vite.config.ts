import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

/**
 * Бэкенд слушает 127.0.0.1:8000, фронт ходит к нему через прокси по пути /api,
 * поэтому в браузере нет CORS и не нужен отдельный базовый URL.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
});
