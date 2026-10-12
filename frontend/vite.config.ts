import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: { proxy: { '/api': 'http://127.0.0.1:8765' } },
  build: { target: 'es2022' },
  // Playwright specs (e2e/) run with `npm run e2e`, not vitest.
  test: { include: ['src/**/*.test.{ts,tsx}'] },
});
