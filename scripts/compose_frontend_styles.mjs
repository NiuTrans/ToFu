#!/usr/bin/env node

/**
 * Compose shipped stylesheets from ordered, model-readable source sections.
 *
 * CSS order is part of the cascade contract, so the manifest is explicit and
 * composition is byte-for-byte concatenation. `static/styles.css` and
 * `static/settings.css` are delivery artifacts; edit only the section sources.
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

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

function repositoryPath(relativePath) {
  return path.join(repositoryRoot, ...relativePath.split('/'));
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

async function readManifest(spec) {
  const root = repositoryPath(spec.sourceRoot);
  const manifestPath = path.join(root, 'manifest.json');
  const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
  if (manifest.version !== 1 || !Array.isArray(manifest.sections)) {
    throw new Error(`unsupported stylesheet manifest: ${spec.sourceRoot}/manifest.json`);
  }
  if (manifest.output !== spec.output) {
    throw new Error(`stylesheet output mismatch in ${spec.sourceRoot}/manifest.json`);
  }
  const names = new Set();
  for (const row of manifest.sections) {
    if (!row || typeof row !== 'object') throw new Error('stylesheet section row must be an object');
    row.path = validateRelativePath(row.path, `${spec.name} stylesheet section`);
    if (!row.path.endsWith('.css')) throw new Error(`stylesheet section must be CSS: ${row.path}`);
    if (names.has(row.path)) throw new Error(`duplicate stylesheet section: ${row.path}`);
    names.add(row.path);
  }
  return { manifest, root };
}

async function renderSheet(spec) {
  const { manifest, root } = await readManifest(spec);
  const pieces = [];
  const rootPrefix = root.endsWith(path.sep) ? root : `${root}${path.sep}`;
  for (const row of manifest.sections) {
    const absolutePath = path.resolve(root, row.path);
    if (!absolutePath.startsWith(rootPrefix)) {
      throw new Error(`unsafe stylesheet section path: ${row.path}`);
    }
    pieces.push(await fs.readFile(absolutePath, 'utf8'));
  }
  return { content: pieces.join(''), sectionCount: pieces.length };
}

async function composeSheet(spec, { check }) {
  const { content, sectionCount } = await renderSheet(spec);
  const outputPath = repositoryPath(spec.output);
  let actual = '';
  try {
    actual = await fs.readFile(outputPath, 'utf8');
  } catch (error) {
    if (!error || error.code !== 'ENOENT') throw error;
  }
  if (actual === content) return { changed: false, sectionCount };
  if (check) {
    throw new Error(`${spec.output} is stale; run node scripts/compose_frontend_styles.mjs`);
  }
  await fs.writeFile(outputPath, content, 'utf8');
  return { changed: true, sectionCount };
}

export async function composeStyles({ check = false } = {}) {
  const results = [];
  for (const spec of sheetSpecs) results.push(await composeSheet(spec, { check }));
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
