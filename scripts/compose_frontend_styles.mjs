#!/usr/bin/env node

/**
 * Compose shipped stylesheets from ordered, model-readable source sections.
 *
 * CSS order is part of the cascade contract, so the manifest is explicit and
 * composition is byte-for-byte concatenation. Deferred sections name their
 * static feature import; generated CSS entries preserve the same ordered
 * manifest for Vite. Entry: composeStyles / CLI. No browser runtime or transport policy.
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import ts from 'typescript';
import { unchangedLegacyTokenCopy } from './style_token_policy.mjs';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const sheetSpecs = [
  {
    name: 'application',
    output: 'static/styles.css',
    sourceRoot: 'frontend/src/styles/application',
  },
  {
    name: 'settings',
    output: 'static/settings.css',
    sourceRoot: 'frontend/src/styles/settings',
  },
];

function repositoryPath(relativePath, projectRoot) {
  return path.join(projectRoot, ...relativePath.split('/'));
}

function validateRelativePath(value, label) {
  if (typeof value !== 'string' || !value || path.isAbsolute(value)) {
    throw new Error(`${label} must be a non-empty relative path`);
  }
  const normalized = value.replaceAll('\\', '/');
  const parts = normalized.split('/');
  if (parts.includes('') || parts.includes('.') || parts.includes('..')) {
    throw new Error(`${label} escapes its declared root: ${value}`);
  }
  return normalized;
}

async function verifyFeatureImport(projectRoot, entry, stylesheet) {
  const owner = repositoryPath(validateRelativePath(entry, 'stylesheet feature owner'), projectRoot);
  if (!owner.endsWith('.ts') && !owner.endsWith('.tsx')) {
    throw new Error(`stylesheet feature owner must be TypeScript: ${entry}`);
  }
  const source = ts.createSourceFile(owner, await fs.readFile(owner, 'utf8'), ts.ScriptTarget.Latest, true);
  const imports = source.statements.filter(ts.isImportDeclaration);
  if (!imports.some(node => !node.importClause && ts.isStringLiteral(node.moduleSpecifier)
      && node.moduleSpecifier.text.startsWith('.')
      && path.resolve(path.dirname(owner), node.moduleSpecifier.text) === stylesheet)) {
    throw new Error(`${entry} must statically import its declared stylesheet ${stylesheet}`);
  }
}

async function readManifest(spec, projectRoot) {
  const root = repositoryPath(spec.sourceRoot, projectRoot);
  const manifestPath = path.join(root, 'manifest.json');
  const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
  if (manifest.version !== 1 || !Array.isArray(manifest.sections)) {
    throw new Error(`unsupported stylesheet manifest: ${spec.sourceRoot}/manifest.json`);
  }
  if (manifest.output !== spec.output) {
    throw new Error(`stylesheet output mismatch in ${spec.sourceRoot}/manifest.json`);
  }
  const names = new Set();
  const deferred = manifest.deferredSections ?? [];
  if (!Array.isArray(deferred)) throw new Error('deferred stylesheet sections must be an array');
  for (const row of [...manifest.sections, ...deferred]) {
    if (!row || typeof row !== 'object') throw new Error('stylesheet section row must be an object');
    row.path = validateRelativePath(row.path, `${spec.name} stylesheet section`);
    if (!row.path.endsWith('.css')) throw new Error(`stylesheet section must be CSS: ${row.path}`);
    if (names.has(row.path)) throw new Error(`duplicate stylesheet section: ${row.path}`);
    names.add(row.path);
    if (row.legacyTokenSha256 !== undefined) unchangedLegacyTokenCopy('', row.legacyTokenSha256);
  }
  for (const row of deferred) {
    await verifyFeatureImport(projectRoot, row.featureEntry, path.resolve(root, row.path));
    await fs.access(path.resolve(root, row.path));
  }
  let sourceEntry;
  if (manifest.sourceEntry !== undefined) {
    const entry = validateRelativePath(manifest.sourceEntry, 'generated stylesheet entry');
    if (!entry.endsWith('.generated.css') || names.has(entry)) {
      throw new Error('stylesheet entry must be a separate .generated.css file');
    }
    sourceEntry = path.resolve(root, entry);
    await verifyFeatureImport(projectRoot, manifest.featureEntry, sourceEntry);
  } else if (manifest.featureEntry !== undefined) {
    throw new Error('stylesheet feature owner requires a generated source entry');
  }
  return { manifest, root, sourceEntry };
}

async function renderSheet(spec, projectRoot) {
  const { manifest, root, sourceEntry } = await readManifest(spec, projectRoot);
  const pieces = [];
  const rootPrefix = root.endsWith(path.sep) ? root : `${root}${path.sep}`;
  for (const row of manifest.sections) {
    const absolutePath = path.resolve(root, row.path);
    if (!absolutePath.startsWith(rootPrefix)) {
      throw new Error(`unsafe stylesheet section path: ${row.path}`);
    }
    pieces.push(await fs.readFile(absolutePath, 'utf8'));
  }
  const entryContent = sourceEntry ? '/* Generated by compose_frontend_styles.mjs; edit manifest.json. */\n'
    + manifest.sections.map(row => {
      const relative = path.relative(path.dirname(sourceEntry), path.resolve(root, row.path)).split(path.sep).join('/');
      return `@import ${JSON.stringify('./' + relative)};\n`;
    }).join('') : null;
  return { content: pieces.join(''), sectionCount: pieces.length, sourceEntry, entryContent };
}

async function writeOutput(outputPath, content, { check }) {
  let actual = '';
  try {
    actual = await fs.readFile(outputPath, 'utf8');
  } catch (error) {
    if (!error || error.code !== 'ENOENT') throw error;
  }
  if (actual === content) return false;
  if (check) {
    throw new Error(`${outputPath} is stale; run node scripts/compose_frontend_styles.mjs`);
  }
  await fs.writeFile(outputPath, content, 'utf8');
  return true;
}

async function composeSheet(spec, { check, projectRoot }) {
  const { content, sectionCount, sourceEntry, entryContent } = await renderSheet(spec, projectRoot);
  const entryChanged = sourceEntry ? await writeOutput(sourceEntry, entryContent, { check }) : false;
  const outputChanged = await writeOutput(repositoryPath(spec.output, projectRoot), content, { check });
  return { changed: entryChanged || outputChanged, sectionCount };
}

export async function composeStyles({ check = false, projectRoot = repositoryRoot } = {}) {
  const results = [];
  for (const spec of sheetSpecs) results.push(await composeSheet(spec, { check, projectRoot }));
  return {
    changed: results.some((result) => result.changed),
    sectionCount: results.reduce((total, result) => total + result.sectionCount, 0),
  };
}

async function main() {
  const mode = process.argv[2] || '--write';
  let result;
  if (mode === '--check') result = await composeStyles({ check: true });
  else if (mode === '--write') result = await composeStyles();
  else throw new Error('usage: compose_frontend_styles.mjs [--write|--check]');
  process.stdout.write(
    `Stylesheet composition ${result.changed ? 'updated' : 'verified'} `
      + `(${result.sectionCount} sections).\n`,
  );
}

const invokedPath = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : '';
if (import.meta.url === invokedPath) await main();
