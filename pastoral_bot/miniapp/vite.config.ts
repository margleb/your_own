import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base: './',
  plugins: [react()],
  build: {
    // Rollup's call-argument tree shaker expands this hook graph past the VPS
    // memory limit. Keep minification and skip that optional optimization.
    rollupOptions: { treeshake: false },
  },
});
