#!/usr/bin/env node
/**
 * Test-bundle adapter: drives the SAME Vite/Rollup pipeline that ships the
 * production frontend, behind the small esbuild CLI subset the test suite was
 * written against. This keeps exactly one bundler in the project — tests now
 * exercise the real production module semantics (Vite resolution, `?url`
 * imports, CSS extraction, JSON modules) instead of a parallel esbuild graph.
 *
 * Supported subset (everything the suite uses):
 *   vite_test_bundle.mjs ENTRY... --bundle
 *     --format=iife|cjs|esm --platform=browser|node
 *     [--global-name=NAME] [--footer:js=CODE]
 *     [--outfile=FILE | --outdir=DIR] [--target=T] [--log-level=L]
 *     [--loader=ts --sourcefile=NAME]   (read entry source from stdin)
 */

import { builtinModules } from 'node:module';
import { mkdtempSync, writeFileSync, mkdirSync, readFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, extname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const temporaryDirectories = new Set();

function parseArgs(argv) {
  const entries = [];
  const flags = {};
  for (const arg of argv) {
    if (arg.startsWith('--')) {
      const eq = arg.indexOf('=');
      if (eq === -1) flags[arg.slice(2)] = true;
      else flags[arg.slice(2, eq)] = arg.slice(eq + 1);
    } else {
      entries.push(arg);
    }
  }
  return { entries, flags };
}

function readStdin() {
  return new Promise((resolveStdin, rejectStdin) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => { data += chunk; });
    process.stdin.on('end', () => resolveStdin(data));
    process.stdin.on('error', rejectStdin);
  });
}

async function main() {
  const { entries, flags } = parseArgs(process.argv.slice(2));
  const format = flags.format || 'iife';
  const platform = flags.platform || 'browser';
  const footer = flags['footer:js'];
  const globalName = flags['global-name'];
  const target = flags.target || 'esnext';

  // stdin mode: esbuild's --loader/--sourcefile pair feeds one virtual entry.
  if (entries.length === 0 && flags.sourcefile) {
    const dir = mkdtempSync(join(tmpdir(), 'tofu-vite-stdin-'));
    temporaryDirectories.add(dir);
    const virtual = join(dir, basename(String(flags.sourcefile)));
    writeFileSync(virtual, await readStdin(), 'utf8');
    entries.push(virtual);
  }
  if (entries.length === 0) {
    throw new Error('no entry point given');
  }

  const { build } = await import('vite');

  // Some harness entries import owners via absolute POSIX paths. Rollup would
  // otherwise treat a leading '/' as a URL; resolve them as filesystem paths.
  const absolutePathResolver = {
    name: 'tofu-test-absolute-path-resolver',
    resolveId(id) {
      if (id.startsWith('/') && existsSync(id)) return id;
      return null;
    },
  };

  const nodeExternals = platform === 'node'
    ? [...builtinModules, ...builtinModules.map((m) => `node:${m}`)]
    : [];

  async function buildOne(entry, outfile) {
    const outDir = dirname(outfile);
    mkdirSync(outDir, { recursive: true });
    await build({
      configFile: false,
      root: process.cwd(),
      logLevel: flags['log-level'] === 'warning' ? 'warn' : (flags['log-level'] || 'warn'),
      plugins: [absolutePathResolver],
      build: {
        target,
        minify: false,
        sourcemap: false,
        cssCodeSplit: false,
        emptyOutDir: false,
        outDir,
        // Library mode: esbuild's --bundle contract is exactly "package this
        // module and expose the entry's exports". Vite's application-entry
        // mode discards entry exports (an HTML shell has no import consumer),
        // which silently emptied every harness bundle.
        lib: {
          entry: resolve(entry),
          formats: [format],
          name: globalName || 'TofuTestBundle',
          fileName: () => basename(outfile),
        },
        rollupOptions: {
          external: nodeExternals,
          // esbuild's --bundle keeps the whole reachable module graph and the
          // entry's exports verbatim. Mirrors that contract exactly — harnesses
          // assert on internal implementation details that production
          // tree-shaking would legitimately drop.
          treeshake: false,
          output: {
            ...(footer ? { footer } : {}),
            inlineDynamicImports: true,
            assetFileNames: 'assets/[name]-[hash][extname]',
            exports: 'auto',
          },
        },
      },
    });
  }

  if (flags.outfile || entries.length === 1) {
    const outfile = flags.outfile
      || join(flags.outdir || '.', `${basename(entries[0], extname(entries[0]))}.js`);
    await buildOne(entries[0], outfile);
    return;
  }

  // Multi-entry form (esbuild emits one self-contained IIFE per entry). Keep
  // bounded concurrency so the legacy-root materialization stays fast without
  // forking one node process per owner.
  const outdir = flags.outdir || '.';
  const queue = [...entries];
  const workers = Array.from({ length: Math.min(8, queue.length) }, async () => {
    while (queue.length) {
      const entry = queue.shift();
      const outfile = join(outdir, `${basename(entry, extname(entry))}.js`);
      await buildOne(entry, outfile);
    }
  });
  await Promise.all(workers);
}

main()
  .catch((error) => {
    process.stderr.write(`vite_test_bundle failed: ${error && error.stack || error}\n`);
    process.exitCode = 1;
  })
  .finally(() => {
    for (const directory of temporaryDirectories) {
      rmSync(directory, { recursive: true, force: true });
    }
  });
