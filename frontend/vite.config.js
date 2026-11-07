import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// base './' so the built assets load correctly when FastAPI serves them from
// the mounted static root. In dev, proxy the API + WebSocket to the Python
// server (default :8099) so the React dev server (5173) talks to it directly.
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8099',
      '/ws': { target: 'ws://localhost:8099', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
