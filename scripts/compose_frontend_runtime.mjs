#!/usr/bin/env node

/**
 * Compose the retained browser runtime from model-readable source sections.
 *
 * The authoring sources and their explicit evaluation order live under
 * `frontend/src/runtime/sections/`. The manifest composes the boot-critical
 * lexical runtime plus optional feature-scoped lexical runtimes. Feature
 * runtimes are native ESM chunks with declared imports and runtime-service
 * dependencies; they never duplicate source or evaluate strings. New product
 * logic belongs in normal TypeScript modules, so every retained bundle remains
 * a shrinking migration boundary.
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import {
  collectAuthoredActionNames,
  collectTopLevelActionReceivers,
  readRepositoryActionReferences,
} from './runtime_action_analysis.mjs';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const runtimeRoot = path.join(repositoryRoot, 'frontend/src/runtime');
const sectionsRoot = path.join(runtimeRoot, 'sections');
const manifestPath = path.join(sectionsRoot, 'manifest.json');
const markerPattern = /^\/\* ===== migrated source: (.+?) ===== \*\/\r?\n/gm;
const epilogueMarker = '\nexport async function loadFeatureFlags()';
const identifierPattern = /^[A-Za-z_$][\w$]*$/;

function relativeToRepository(value) {
  return path.relative(repositoryRoot, value).split(path.sep).join('/');
}

function validateRelativePath(value, label) {
  if (typeof value !== 'string' || !value || path.isAbsolute(value)) {
    throw new Error(`${label} must be a non-empty relative path`);
  }
  const parts = value.replaceAll('\\', '/').split('/');
  if (parts.includes('') || parts.includes('.') || parts.includes('..')) {
    throw new Error(`${label} escapes its declared root: ${value}`);
  }
  return parts.join('/');
}

function sectionFileName(sourceName) {
  const safeName = validateRelativePath(sourceName, 'section source');
  if (!safeName.endsWith('.js')) {
    throw new Error(`runtime section must name a JavaScript source: ${sourceName}`);
  }
  return safeName;
}

function validateOutputPath(value, label) {
  const relativePath = validateRelativePath(value, label);
  if (!relativePath.endsWith('.js')) {
    throw new Error(`${label} must name a JavaScript output: ${value}`);
  }
  const absolutePath = path.resolve(repositoryRoot, relativePath);
  const prefix = runtimeRoot.endsWith(path.sep) ? runtimeRoot : `${runtimeRoot}${path.sep}`;
  if (!absolutePath.startsWith(prefix)) {
    throw new Error(`${label} must stay inside frontend/src/runtime: ${value}`);
  }
  return { absolutePath, relativePath };
}

function validateIdentifier(value, label) {
  if (typeof value !== 'string' || !identifierPattern.test(value)) {
    throw new Error(`${label} must be a JavaScript identifier: ${String(value)}`);
  }
  return value;
}

function validateIdentifierList(values, label) {
  if (!Array.isArray(values)) throw new Error(`${label} must be an array`);
  const names = new Set();
  for (const value of values) {
    const name = validateIdentifier(value, label);
    if (names.has(name)) throw new Error(`duplicate ${label}: ${name}`);
    names.add(name);
  }
}

function moduleSpecifier(outputPath, repositoryRelativeSource) {
  const sourcePath = path.resolve(repositoryRoot, repositoryRelativeSource);
  let relativePath = path.relative(path.dirname(outputPath), sourcePath)
    .split(path.sep).join('/').replace(/\.(?:m?[jt]s)$/, '');
  if (!relativePath.startsWith('.')) relativePath = `./${relativePath}`;
  return relativePath;
}

function validateSectionRows(rows, label, sourceNames, sourcePaths) {
  if (!Array.isArray(rows) || rows.length === 0) {
    throw new Error(`${label} must declare at least one section`);
  }
  for (const row of rows) {
    if (!row || typeof row !== 'object') throw new Error(`${label} row must be an object`);
    row.source = sectionFileName(row.source);
    row.path = validateRelativePath(row.path, `section path for ${row.source}`);
    if (sourceNames.has(row.source)) throw new Error(`duplicate runtime section: ${row.source}`);
    if (sourcePaths.has(row.path)) throw new Error(`duplicate runtime section path: ${row.path}`);
    sourceNames.add(row.source);
    sourcePaths.add(row.path);
  }
}

async function readManifest() {
  const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
  if (manifest.version !== 2 || !Array.isArray(manifest.sections)
      || !Array.isArray(manifest.lazyBundles)) {
    throw new Error(`unsupported runtime-section manifest: ${relativeToRepository(manifestPath)}`);
  }
  manifest.output = validateOutputPath(manifest.output, 'manifest output').relativePath;
  validateRelativePath(manifest.prelude, 'manifest prelude');
  validateRelativePath(manifest.epilogue, 'manifest epilogue');
  const sourceNames = new Set();
  const sourcePaths = new Set();
  const bundleNames = new Set();
  const outputPaths = new Set([manifest.output]);
  validateSectionRows(manifest.sections, 'main runtime', sourceNames, sourcePaths);
  for (const bundle of manifest.lazyBundles) {
    if (!bundle || typeof bundle !== 'object') throw new Error('lazy runtime bundle must be an object');
    bundle.name = validateRelativePath(bundle.name, 'lazy runtime bundle name');
    if (!/^[a-z][a-z0-9-]*$/.test(bundle.name)) {
      throw new Error(`lazy runtime bundle name is invalid: ${bundle.name}`);
    }
    if (bundleNames.has(bundle.name)) throw new Error(`duplicate lazy runtime bundle: ${bundle.name}`);
    bundleNames.add(bundle.name);
    bundle.output = validateOutputPath(
      bundle.output, `lazy runtime output for ${bundle.name}`,
    ).relativePath;
    if (outputPaths.has(bundle.output)) throw new Error(`duplicate runtime output: ${bundle.output}`);
    outputPaths.add(bundle.output);
    if (!Array.isArray(bundle.moduleImports) || !Array.isArray(bundle.registryImports)
        || !Array.isArray(bundle.runtimeServices)
        || !Array.isArray(bundle.runtimeExports)
        || !Array.isArray(bundle.runtimeBindings)) {
      throw new Error(
        `lazy runtime ${bundle.name} must declare imports, registries, services, exports, and bindings`,
      );
    }
    validateIdentifierList(bundle.runtimeExports, `runtime export for ${bundle.name}`);
    const runtimeBindingNames = new Set();
    for (const binding of bundle.runtimeBindings) {
      if (!binding || typeof binding !== 'object') {
        throw new Error(`lazy runtime ${bundle.name} has an invalid runtime binding`);
      }
      binding.name = validateIdentifier(
        binding.name, `runtime binding for ${bundle.name}`,
      );
      if (!['array', 'boolean', 'object', 'value'].includes(binding.kind)) {
        throw new Error(`runtime binding ${binding.name} has an invalid kind`);
      }
      if (runtimeBindingNames.has(binding.name)) {
        throw new Error(`duplicate runtime binding: ${binding.name}`);
      }
      runtimeBindingNames.add(binding.name);
    }
    const dependencyNames = new Set(['runtimeScope']);
    for (const moduleImport of bundle.moduleImports) {
      if (!moduleImport || typeof moduleImport !== 'object'
          || !Array.isArray(moduleImport.bindings)
          || moduleImport.bindings.length === 0) {
        throw new Error(`lazy runtime ${bundle.name} has an invalid module import`);
      }
      moduleImport.source = validateRelativePath(
        moduleImport.source, `module import source for ${bundle.name}`,
      );
      if (!moduleImport.source.startsWith('frontend/src/')) {
        throw new Error(`lazy runtime ${bundle.name} import must stay in frontend/src`);
      }
      for (const binding of moduleImport.bindings) {
        validateIdentifier(binding, `module import binding for ${bundle.name}`);
        if (dependencyNames.has(binding)) throw new Error(`duplicate lazy dependency: ${binding}`);
        dependencyNames.add(binding);
      }
    }
    for (const registryImport of bundle.registryImports) {
      if (!registryImport || typeof registryImport !== 'object'
          || !Array.isArray(registryImport.members)
          || registryImport.members.length === 0) {
        throw new Error(`lazy runtime ${bundle.name} has an invalid registry import`);
      }
      registryImport.source = validateRelativePath(
        registryImport.source, `registry import source for ${bundle.name}`,
      );
      if (!registryImport.source.startsWith('frontend/src/')) {
        throw new Error(`lazy runtime ${bundle.name} registry must stay in frontend/src`);
      }
      registryImport.binding = validateIdentifier(
        registryImport.binding, `registry import binding for ${bundle.name}`,
      );
      validateIdentifierList(
        registryImport.members, `registry member for ${bundle.name}`,
      );
      for (const name of [registryImport.binding, ...registryImport.members]) {
        if (dependencyNames.has(name)) throw new Error(`duplicate lazy dependency: ${name}`);
        dependencyNames.add(name);
      }
    }
    for (const dependency of bundle.runtimeServices) {
      if (!dependency || typeof dependency !== 'object') {
        throw new Error(`lazy runtime ${bundle.name} has an invalid runtime service`);
      }
      dependency.name = validateIdentifier(
        dependency.name, `runtime service for ${bundle.name}`,
      );
      if (!['function', 'object', 'value'].includes(dependency.kind)) {
        throw new Error(`runtime service ${dependency.name} has an invalid kind`);
      }
      if (dependency.providedBy !== undefined
          && !['runtime', 'feature'].includes(dependency.providedBy)) {
        throw new Error(`runtime service ${dependency.name} has an invalid providedBy`);
      }
      if (dependencyNames.has(dependency.name)) {
        throw new Error(`duplicate lazy dependency: ${dependency.name}`);
      }
      dependencyNames.add(dependency.name);
    }
    validateSectionRows(
      bundle.sections, `lazy runtime ${bundle.name}`, sourceNames, sourcePaths,
    );
  }
  return manifest;
}

async function readSectionPieces(rows) {
  const pieces = [];
  for (const row of rows) {
    const relativePath = row.path;
    const absolutePath = path.resolve(sectionsRoot, relativePath);
    const prefix = sectionsRoot.endsWith(path.sep) ? sectionsRoot : `${sectionsRoot}${path.sep}`;
    if (!absolutePath.startsWith(prefix)) throw new Error(`unsafe runtime section path: ${relativePath}`);
    const source = await fs.readFile(absolutePath, 'utf8');
    const expected = `/* ===== migrated source: ${row.source} ===== */`;
    if (!source.startsWith(`${expected}\n`)) {
      throw new Error(`runtime section marker mismatch: ${row.path}`);
    }
    pieces.push(source);
  }
  return pieces;
}

function runtimeBindingSetter(binding) {
  if (binding.kind === 'array') {
    return `if (Array.isArray(value)) ${binding.name} = value;`;
  }
  if (binding.kind === 'boolean') return `${binding.name} = Boolean(value);`;
  if (binding.kind === 'object') {
    return `if (value && typeof value === 'object') ${binding.name} = value;`;
  }
  return `${binding.name} = value;`;
}

function renderLazyRuntimePorts(bundle, actionReceivers) {
  const actionNames = new Set(actionReceivers);
  const explicitExports = bundle.runtimeExports.filter((name) => !actionNames.has(name));
  const lines = ['', `// BEGIN GENERATED LAZY RUNTIME PORTS — ${bundle.name}`];
  if (bundle.runtimeBindings.length > 0) {
    lines.push('Object.defineProperties(runtimeScope, {');
    for (const binding of bundle.runtimeBindings) {
      lines.push(`  ${binding.name}: {`);
      lines.push('    configurable: true,');
      lines.push('    enumerable: false,');
      lines.push(`    get: () => ${binding.name},`);
      lines.push(`    set: (value) => { ${runtimeBindingSetter(binding)} },`);
      lines.push('  },');
    }
    lines.push('});');
  }
  lines.push(...explicitExports.map((name) => `runtimeScope.${name} = ${name};`));
  lines.push('// END GENERATED LAZY RUNTIME PORTS');
  lines.push('');
  return lines.join('\n');
}

function renderRegistryBinding(registryImport) {
  return [
    'const {',
    ...registryImport.members.map((name) => `  ${name},`),
    `} = ${registryImport.binding};`,
  ].join('\n');
}

// Every service a lazy bundle declares must resolve at chunk evaluation.
// The retained runtime publishes through four seams: the epilogue
// Object.assign(runtimeScope, …) table, Object.defineProperties(runtimeScope,
// …) accessors, direct runtimeScope.<name> = writes in main sections, and the
// generated runtimeActions table. Services the owning feature chunk installs
// on the registry before importing its presenters opt out via
// "providedBy": "feature"; everything else must appear in one of those seams
// or the generated guard throws at click time (the "功能模块加载失败" class
// of failure this audit exists to prevent).
function collectMainRuntimePublications(source) {
  const names = new Set();
  for (const match of source.matchAll(/runtimeScope\.([A-Za-z_$][\w$]*)\s*=(?![=>])/g)) {
    names.add(match[1]);
  }
  for (const block of source.matchAll(/Object\.assign\(runtimeScope, \{([\s\S]*?)\n\}\)/g)) {
    for (const match of block[1].matchAll(/^ {2}([A-Za-z_$][\w$]*)\s*[:,]/gm)) {
      names.add(match[1]);
    }
  }
  for (const block of source.matchAll(/Object\.defineProperties\(runtimeScope, \{([\s\S]*?)\n\}\);/g)) {
    for (const match of block[1].matchAll(/^ {2}([A-Za-z_$][\w$]*)\s*:/gm)) {
      names.add(match[1]);
    }
  }
  const actions = /const runtimeActions = Object\.freeze\(\{([\s\S]*?)\n\}\);/.exec(source);
  if (actions) {
    for (const match of actions[1].matchAll(/^ {2}([A-Za-z_$][\w$]*)\s*,?$/gm)) {
      names.add(match[1]);
    }
  }
  return names;
}

function auditLazyRuntimeServicePublication(manifest, mainSource) {
  const published = collectMainRuntimePublications(mainSource);
  const missing = [];
  for (const bundle of manifest.lazyBundles) {
    for (const dependency of bundle.runtimeServices) {
      if (dependency.providedBy === 'feature') continue;
      if (!published.has(dependency.name)) {
        missing.push(`${bundle.name}: ${dependency.name}`);
      }
    }
  }
  if (missing.length > 0) {
    throw new Error(
      'lazy runtime services are not published by the retained runtime. Publish the '
      + 'binding in frontend/src/runtime/sections/_epilogue.js (Object.assign seam) or '
      + 'mark the service "providedBy": "feature" when the owning feature chunk installs '
      + 'it before importing its presenters:\n' + missing.join('\n'),
    );
  }
}
async function renderRuntimes() {
  const manifest = await readManifest();
  const actionReferences = readRepositoryActionReferences(repositoryRoot);
  const authoredActionNames = collectAuthoredActionNames({
    references: actionReferences,
  });
  const mainPieces = [
    await fs.readFile(path.join(sectionsRoot, manifest.prelude), 'utf8'),
    ...await readSectionPieces(manifest.sections),
    await fs.readFile(path.join(sectionsRoot, manifest.epilogue), 'utf8'),
  ];
  auditLazyRuntimeServicePublication(manifest, mainPieces.join(''));
  const outputs = new Map([
    [path.resolve(repositoryRoot, manifest.output), mainPieces.join('')],
  ]);
  for (const bundle of manifest.lazyBundles) {
    const outputPath = path.resolve(repositoryRoot, bundle.output);
    const imports = [
      `import { featureRegistry as runtimeScope } from '${moduleSpecifier(
        outputPath, 'frontend/src/feature-registry.ts',
      )}';`,
      ...bundle.moduleImports.map((moduleImport) => (
        `import { ${moduleImport.bindings.join(', ')} } from '${moduleSpecifier(
          outputPath, moduleImport.source,
        )}';`
      )),
      ...bundle.registryImports.map((registryImport) => (
        `import { ${registryImport.binding} } from '${moduleSpecifier(
          outputPath, registryImport.source,
        )}';`
      )),
    ];
    const registryBindings = bundle.registryImports.map(renderRegistryBinding);
    const dependencies = [];
    for (const dependency of bundle.runtimeServices) {
      dependencies.push(`const ${dependency.name} = runtimeScope.${dependency.name};`);
      const predicate = dependency.kind === 'function'
        ? `typeof ${dependency.name} !== 'function'`
        : dependency.kind === 'object'
          ? `!${dependency.name} || typeof ${dependency.name} !== 'object'`
          : `${dependency.name} === undefined`;
      dependencies.push(
        `if (${predicate}) throw new Error(`
        + `'${bundle.name} runtime dependency is unavailable: ${dependency.name}');`,
      );
    }
    const header = [
      '// @ts-check',
      `/* Generated lazy retained runtime: ${bundle.name}. Do not edit directly. */`,
      ...imports,
      '',
      ...registryBindings,
      ...(registryBindings.length > 0 ? [''] : []),
      ...dependencies,
      '',
    ].join('\n');
    const retainedSource = (await readSectionPieces(bundle.sections)).join('');
    const actionReceivers = collectTopLevelActionReceivers({
      definitionPath: outputPath,
      definitionSource: retainedSource,
      references: actionReferences,
      seedNames: ['openTradingMode', '_openActiveCompaction'],
    });
    const registryActionReceivers = bundle.registryImports.flatMap(
      (registryImport) => registryImport.members
        .filter((name) => authoredActionNames.has(name))
        .map((name) => ({ name, registry: registryImport.binding })),
    );
    const localActionNames = new Set(actionReceivers);
    const actionPublications = [
      `// BEGIN GENERATED LAZY RUNTIME ACTIONS — ${bundle.name}`,
      ...actionReceivers.map((name) => `runtimeScope.${name} = ${name};`),
      ...registryActionReceivers
        .filter(({ name }) => !localActionNames.has(name))
        .map(({ name, registry }) => `runtimeScope.${name} = ${registry}.${name};`),
      '// END GENERATED LAZY RUNTIME ACTIONS',
      '',
    ].join('\n');
    outputs.set(
      outputPath,
      header + retainedSource
        + renderLazyRuntimePorts(bundle, actionReceivers)
        + actionPublications,
    );
  }
  return { manifest, outputs };
}

export async function composeRuntime({ check = false } = {}) {
  const { manifest, outputs } = await renderRuntimes();
  const stale = [];
  for (const [outputPath, expected] of outputs) {
    let actual = '';
    try {
      actual = await fs.readFile(outputPath, 'utf8');
    } catch (error) {
      if (!error || error.code !== 'ENOENT') throw error;
    }
    if (actual !== expected) stale.push({ outputPath, expected });
  }
  if (check && stale.length > 0) {
    throw new Error(
      `${stale.map((item) => relativeToRepository(item.outputPath)).join(', ')} `
      + 'is stale; run node scripts/compose_frontend_runtime.mjs',
    );
  }
  for (const item of stale) {
    await fs.mkdir(path.dirname(item.outputPath), { recursive: true });
    await fs.writeFile(item.outputPath, item.expected, 'utf8');
  }
  const sectionCount = manifest.sections.length + manifest.lazyBundles.reduce(
    (total, bundle) => total + bundle.sections.length, 0,
  );
  return {
    changed: stale.length > 0,
    lazyBundleCount: manifest.lazyBundles.length,
    sectionCount,
  };
}

async function extractRuntime() {
  const currentManifest = await readManifest();
  if (currentManifest.lazyBundles.length > 0) {
    throw new Error('--extract cannot flatten a manifest with lazy runtime bundles');
  }
  const outputPath = path.resolve(repositoryRoot, currentManifest.output);
  const source = await fs.readFile(outputPath, 'utf8');
  const matches = [...source.matchAll(markerPattern)];
  if (!matches.length) throw new Error('retained runtime has no migrated-source markers');
  const epilogueStart = source.indexOf(epilogueMarker, matches.at(-1).index);
  if (epilogueStart < 0) throw new Error('retained runtime epilogue marker is missing');

  await fs.rm(sectionsRoot, { recursive: true, force: true });
  await fs.mkdir(sectionsRoot, { recursive: true });
  const prelude = source.slice(0, matches[0].index);
  const epilogue = source.slice(epilogueStart + 1);
  await fs.writeFile(path.join(sectionsRoot, '_prelude.js'), prelude, 'utf8');
  await fs.writeFile(path.join(sectionsRoot, '_epilogue.js'), epilogue, 'utf8');

  const sections = [];
  for (let index = 0; index < matches.length; index += 1) {
    const match = matches[index];
    const sourceName = sectionFileName(match[1]);
    const start = match.index;
    const end = index + 1 < matches.length ? matches[index + 1].index : epilogueStart + 1;
    const body = source.slice(start, end);
    const relativePath = sourceName;
    const destination = path.join(sectionsRoot, ...relativePath.split('/'));
    await fs.mkdir(path.dirname(destination), { recursive: true });
    await fs.writeFile(destination, body, 'utf8');
    sections.push({ source: sourceName, path: relativePath });
  }

  const manifest = {
    version: 2,
    responsibility: 'Ordered authoring sources for the generated retained browser runtime.',
    output: 'frontend/src/runtime/app-runtime.js',
    prelude: '_prelude.js',
    epilogue: '_epilogue.js',
    lazyBundles: [],
    sections,
  };
  await fs.writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
  const rendered = await renderRuntimes();
  if (rendered.outputs.get(outputPath) !== source) {
    throw new Error('runtime extraction was not byte-identical');
  }
  return { changed: true, sectionCount: sections.length };
}

async function main() {
  const mode = process.argv[2] || '--write';
  let result;
  if (mode === '--extract') result = await extractRuntime();
  else if (mode === '--check') result = await composeRuntime({ check: true });
  else if (mode === '--write') result = await composeRuntime();
  else throw new Error('usage: compose_frontend_runtime.mjs [--write|--check|--extract]');
  process.stdout.write(
    `Runtime composition ${result.changed ? 'updated' : 'verified'} `
    + `(${result.sectionCount} sections, ${result.lazyBundleCount || 0} lazy bundles).\n`,
  );
}

const invokedPath = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : '';
if (import.meta.url === invokedPath) {
  await main();
}
