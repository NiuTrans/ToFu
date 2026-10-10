import { defineConfig } from 'vitest/config';

// Vitest owns co-located unit tests for pure TypeScript modules under
// frontend/src (new behavior lands as *.test.ts next to its owner). The
// legacy node --test *.test.cjs harnesses keep their own npm scripts; this
// runner deliberately does not pick them up.
export default defineConfig({
  test: {
    include: ['frontend/src/**/*.test.ts'],
    environment: 'node',
    watch: false,
  },
});
