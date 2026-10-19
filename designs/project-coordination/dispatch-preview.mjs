import { defineConfig } from 'vite';
import { resolve } from 'node:path';
/** Rendering-only build; no backend server, provider or watcher is started. */
export default defineConfig({
  root: '.', base: './',
  build: {
    outDir: '.native/project-dispatch-preview', emptyOutDir: false,
    rollupOptions: { input: resolve('designs/project-coordination/dispatch-preview.html') },
  },
});
