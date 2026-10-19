import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev proxy avoids CORS: the browser calls same-origin /api/v1 and Vite
// forwards to the FastAPI server. In the container, nginx does the same
// (see ui/nginx.conf), so no API CORS change is required.
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: process.env.AAP_BRIDGE_API_URL || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  preview: {
    port: 3000,
  },
});
