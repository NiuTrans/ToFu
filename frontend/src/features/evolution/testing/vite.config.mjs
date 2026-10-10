/** Isolated design server: do not scan the application's data/runtime trees. */
import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';
export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { fs: { allow: [fileURLToPath(new URL('../../../../', import.meta.url))] } },
});
