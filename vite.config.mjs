import { defineConfig } from 'vite';
import { resolve } from 'node:path';

const outDir = process.env.TOFU_VITE_OUT_DIR || 'static/vite';
const TOOL_PRESENTATION_CHUNK_MODULES = new Set([
  'image-source-policy.ts',
  'tool-approval-presentation.ts',
  'tool-browser-execution-presentation.ts',
  'tool-command-execution-presentation.ts',
  'tool-execution-groups.ts',
  'tool-image-presentation.ts',
  'tool-human-guidance-presentation.ts',
  'tool-injection-presentation.ts',
  'tool-result-presentation.ts',
  'tool-round-icons.ts',
  'tool-round-presentation.ts',
  'tool-search-presentation.ts',
  'turn-provenance.ts',
  'write-gate-refusal.ts',
]);

export function manualChunkName(moduleId) {
  const normalizedId = moduleId.replaceAll('\\', '/');
  const marker = '/frontend/src/conversation/presentation/';
  const markerIndex = normalizedId.lastIndexOf(marker);
  if (markerIndex < 0) return undefined;
  const relativeName = normalizedId.slice(markerIndex + marker.length);
  return TOOL_PRESENTATION_CHUNK_MODULES.has(relativeName)
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
