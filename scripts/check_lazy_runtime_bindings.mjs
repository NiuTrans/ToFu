#!/usr/bin/env node

/** Fail when a generated lazy retained runtime references an undeclared name.
 *
 * Rollup can preserve a free identifier as a browser global, which turns a
 * missed migration dependency into a click-time ReferenceError. TypeScript's
 * JavaScript checker gives this boundary a closed-world binding audit without
 * forcing the retained sections to satisfy the full typed-module contract.
 */

import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const require = createRequire(import.meta.url);
const ts = require('typescript');
const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const runtimeRoot = path.join(repositoryRoot, 'frontend/src/runtime');
const manifestPath = path.join(runtimeRoot, 'sections/manifest.json');
const unresolvedDiagnosticCodes = new Set([2304, 2503, 2552, 2580, 2591]);

function lazyOutputPaths() {
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  if (manifest.version !== 2 || !Array.isArray(manifest.lazyBundles)) {
    throw new Error('runtime-section manifest does not declare lazy bundles');
  }
  const prefix = runtimeRoot.endsWith(path.sep) ? runtimeRoot : `${runtimeRoot}${path.sep}`;
  return manifest.lazyBundles.map((bundle) => {
    const output = path.resolve(repositoryRoot, String(bundle.output || ''));
    if (!output.startsWith(prefix) || !output.endsWith('.js')) {
      throw new Error(`unsafe lazy runtime output: ${String(bundle.output)}`);
    }
    return { name: String(bundle.name || ''), output };
  });
}

/** Share parsed files and filesystem probes across the per-bundle programs.
 *
 * One check reads an unchanging checkout. Without sharing, every bundle
 * program re-resolves the ~60 default lib files (about 3,000 directory probes
 * each on a network-mounted checkout) and re-parses lib.dom.d.ts. Programs stay
 * separate, so names declared by one bundle's imports never resolve another's.
 */
function createSharedCompilerHost(compilerOptions) {
  const host = ts.createCompilerHost(compilerOptions);
  for (const name of ['fileExists', 'directoryExists', 'readFile', 'realpath', 'getDirectories']) {
    const probe = host[name];
    if (typeof probe !== 'function') continue;
    const results = new Map();
    host[name] = (target) => {
      if (!results.has(target)) results.set(target, probe.call(host, target));
      return results.get(target);
    };
  }
  const sourceFiles = new Map();
  const getSourceFile = host.getSourceFile;
  host.getSourceFile = (fileName, ...rest) => {
    let sourceFile = sourceFiles.get(fileName);
    if (sourceFile === undefined) {
      sourceFile = getSourceFile.call(host, fileName, ...rest);
      if (sourceFile !== undefined) sourceFiles.set(fileName, sourceFile);
    }
    return sourceFile;
  };
  return host;
}

export function checkLazyRuntimeBindings() {
  const failures = [];
  const outputs = lazyOutputPaths();
  const compilerOptions = {
    allowJs: true,
    checkJs: true,
    noEmit: true,
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    lib: ['lib.es2022.d.ts', 'lib.dom.d.ts'],
    noImplicitAny: false,
    skipLibCheck: true,
  };
  const host = createSharedCompilerHost(compilerOptions);
  for (const bundle of outputs) {
    if (!fs.existsSync(bundle.output)) {
      failures.push(`${bundle.name}: generated output is missing`);
      continue;
    }
    const program = ts.createProgram([bundle.output], compilerOptions, host);
    // Only the bundle's own diagnostics are reported, and a bundle exports
    // nothing another file could resolve, so imported modules go unchecked.
    const sourceFile = program.getSourceFile(bundle.output);
    for (const diagnostic of ts.getPreEmitDiagnostics(program, sourceFile)) {
      if (!diagnostic.file
          || path.resolve(diagnostic.file.fileName) !== bundle.output
          || !unresolvedDiagnosticCodes.has(diagnostic.code)) continue;
      const position = diagnostic.file.getLineAndCharacterOfPosition(
        diagnostic.start || 0,
      );
      const message = ts.flattenDiagnosticMessageText(diagnostic.messageText, ' ');
      failures.push(
        `${bundle.name}:${position.line + 1}:${position.character + 1}: ${message}`,
      );
    }
  }
  if (failures.length > 0) {
    throw new Error(`lazy runtime binding audit failed:\n${failures.join('\n')}`);
  }
  return outputs.length;
}

const invokedPath = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : '';
if (import.meta.url === invokedPath) {
  const count = checkLazyRuntimeBindings();
  process.stdout.write(`Lazy runtime bindings verified (${count} bundles).\n`);
}
