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
 *     [--inputs-manifest=FILE]  (versioned input and emitted-artifact receipt)
 */

import { builtinModules } from 'node:module';
import { mkdtempSync, writeFileSync, mkdirSync, readFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, extname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const temporaryDirectories = new Set();
const inputFiles = new Set();
const outputFiles = new Set();
let outputsComplete = true;
let portableInputs = true;

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

  // --inputs-manifest=FILE: record every file Rollup read (modules and watch
  // files) so a caller can content-validate a cached bundle.
  const inputRecorder = {
    name: 'tofu-test-input-recorder',
    transform(code, id) {
      // Location-sensitive modules keep checkout-local reuse. Do not infer
      // portability merely because constant folding removed a path string.
      if (/\.(?:css|less|sass|scss|styl|stylus)(?:\?|$)/.test(id)) return;
      let nodes;
      try {
        nodes = [this.parse(code)];
      } catch {
        // An unrecognized source form supplies no portability proof.
        portableInputs = false;
        return;
      }
      while (nodes.length) {
        const node = nodes.pop();
        if (!node || typeof node !== 'object') continue;
        if (node.type === 'MetaProperty'
            || (node.type === 'Identifier' && ['__dirname', '__filename'].includes(node.name))) {
          portableInputs = false;
          return;
        }
        for (const value of Object.values(node)) {
          if (value && typeof value === 'object') nodes.push(value);
        }
      }
    },
    configResolved(config) {
      // Public-directory copies do not pass through Rollup's output hooks.
      // Keep these builds executable, but do not certify an incomplete cache.
      if (config.build.copyPublicDir && config.publicDir && existsSync(config.publicDir)) {
        outputsComplete = false;
      }
    },
    buildEnd() {
      for (const id of [...this.getModuleIds(), ...this.getWatchFiles()]) {
        if (id.startsWith('\0')) continue;
        const file = id.split('?', 1)[0];
        if (file.startsWith('/')) inputFiles.add(file);
      }
    },
    writeBundle(options, bundle) {
      const directory = options.dir || dirname(options.file);
      for (const name of Object.keys(bundle)) outputFiles.add(resolve(directory, name));
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
      // Private generated entries use stable root-relative specifiers. Vite
      // still resolves and records the real files in this checkout.
      resolve: { alias: { '#tofu-test-root': process.cwd() } },
      logLevel: flags['log-level'] === 'warning' ? 'warn' : (flags['log-level'] || 'warn'),
      plugins: [absolutePathResolver, inputRecorder],
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

async function mainWithInputsManifest() {
  await main();
  const manifest = parseArgs(process.argv.slice(2)).flags['inputs-manifest'];
  if (manifest) {
    writeFileSync(String(manifest), JSON.stringify({
      format: 'tofu.test-bundle/v1',
      inputs: [...inputFiles].sort(),
      outputs: [...outputFiles].sort(),
      complete: outputsComplete,
      portable: portableInputs,
    }), 'utf8');
  }
}

mainWithInputsManifest()
  .catch((error) => {
    process.stderr.write(`vite_test_bundle failed: ${error && error.stack || error}\n`);
    process.exitCode = 1;
  })
  .finally(() => {
    for (const directory of temporaryDirectories) {
      rmSync(directory, { recursive: true, force: true });
    }
  });
