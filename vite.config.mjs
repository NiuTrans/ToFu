import { defineConfig } from 'vite';
import { resolve } from 'node:path';
import { readFileSync } from 'node:fs';

const { entries } = JSON.parse(readFileSync(new URL('./frontend/authoring-inputs.json', import.meta.url), 'utf8'));

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
    // Never wipe the output directory: the orchestrated build stages into a
    // private temp dir and publishes atomically with bounded multi-generation
    // retention (scripts/build_frontend.mjs), and a raw `vite build` pointed
    // at the live dir must not delete generations that long-lived tabs can
    // still import. Stale files left by raw builds are pruned by the next
    // orchestrated build.
    emptyOutDir: false,
    manifest: 'manifest.json',
    target: 'safari15',
    sourcemap: true,
    rollupOptions: {
      input: Object.fromEntries(Object.entries(entries).map(([name, path]) =>
        [name, resolve(process.cwd(), path)])),
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
});
