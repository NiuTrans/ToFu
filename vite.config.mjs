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

const TASK_MODE_CHUNK_MARKER = '/frontend/src/features/orchestration/task-mode-';

export function manualChunkName(moduleId) {
  const normalizedId = moduleId.replaceAll('\\', '/');
  // The Task Mode owner family (task-mode-*.ts, ~180 KB of source) is a
  // separable cache generation inside the lazy orchestration feature: a
  // task-mode edit must not invalidate the Studio chunk and vice versa, and
  // each chunk stays under the async-chunk delivery budget on its own. The
  // tiny task-mode.ts barrel deliberately stays in the entry chunk — the
  // marker's trailing dash matches only the family files.
  if (normalizedId.includes(TASK_MODE_CHUNK_MARKER)) {
    return 'orchestration-task-mode';
  }
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
        manualChunks: manualChunkName,
      },
    },
  },
});
