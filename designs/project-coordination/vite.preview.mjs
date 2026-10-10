import { defineConfig } from 'vite';
import { resolve } from 'node:path';
export default defineConfig({
  root: '.', base: './',
  build: {
    outDir: '.native/project-coordination-preview', emptyOutDir: false,
    rollupOptions: { input: resolve('designs/project-coordination/preview.html') },
  },
});
