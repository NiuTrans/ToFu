import { defineConfig } from 'vite';
import { resolve } from 'node:path';

const outDir = process.env.TOFU_VITE_OUT_DIR || 'static/vite';

export default defineConfig({
  root: '.',
  // Keep emitted imports relative to their owning chunk. Deployments commonly
  // sit below an opaque reverse-proxy prefix (for example /proxy/15000/) that
  // Quart cannot see after the proxy strips it. An origin-absolute
  // /static/vite/ base bypasses that prefix and never reaches this app.
  base: './',
  build: {
    outDir,
    emptyOutDir: true,
    manifest: 'manifest.json',
    target: 'safari15',
    sourcemap: true,
    rollupOptions: {
      input: {
        main: resolve(process.cwd(), 'frontend/src/main.ts'),
        admin: resolve(process.cwd(), 'frontend/src/admin.ts'),
      },
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
});
