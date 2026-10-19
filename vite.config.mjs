import { defineConfig } from 'vite';
import { resolve } from 'node:path';

const outDir = process.env.TOFU_VITE_OUT_DIR || 'static/vite';
const TOOL_PRESENTATION_CHUNK_MARKER = '/frontend/src/conversation/presentation/';

/* Derive the chunk by path prefix, not a filename allowlist: a rename stays in
 * the chunk instead of silently no-op'ing back into the entry graph. The whole
 * presentation directory is small, startup-required policy/presentation code,
 * so one eagerly cached chunk is the intended delivery shape. */
export function manualChunkName(moduleId) {
  const normalizedId = moduleId.replaceAll('\\', '/');
  return normalizedId.includes(TOOL_PRESENTATION_CHUNK_MARKER)
    ? 'tool-presentation'
    : undefined;
}

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
      input: {
        main: resolve(process.cwd(), 'frontend/src/main.ts'),
        admin: resolve(process.cwd(), 'frontend/src/admin.ts'),
      },
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
        manualChunks: manualChunkName,
      },
    },
  },
});
