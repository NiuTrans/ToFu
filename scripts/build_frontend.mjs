#!/usr/bin/env node

import { build } from 'vite';
import {
  copyFile, mkdir, mkdtemp, open, readFile, readdir, rename, rm, stat, writeFile,
} from 'node:fs/promises';
import { dirname, join, relative, resolve, sep } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const liveDir = join(root, 'static', 'vite');
const assetsDir = join(liveDir, 'assets');
const requiredEntries = ['frontend/src/main.ts', 'frontend/src/admin.ts'];

function safeAsset(value) {
  if (typeof value !== 'string' || !value.startsWith('assets/') || value.includes('\\')) return '';
  const resolved = resolve(liveDir, value);
  const prefix = liveDir.endsWith(sep) ? liveDir : `${liveDir}${sep}`;
  return resolved.startsWith(prefix) && !value.split('/').includes('..') ? value : '';
}

async function readJson(path) {
  return JSON.parse(await readFile(path, 'utf8'));
}

async function validateManifest(manifest, baseDir) {
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
    throw new Error('Vite manifest root must be an object');
  }
  const seen = new Set();
  const assets = new Set();
  async function visit(key) {
    if (seen.has(key)) return;
    const row = manifest[key];
    if (!row || typeof row !== 'object' || Array.isArray(row)) {
      throw new Error(`Vite manifest reference is missing: ${key}`);
    }
    seen.add(key);
    for (const value of [row.file, ...(row.css || []), ...(row.assets || [])]) {
      const asset = safeAsset(value);
      if (!asset) throw new Error(`Unsafe Vite asset path: ${String(value)}`);
      const info = await stat(join(baseDir, asset));
      if (!info.isFile()) throw new Error(`Vite asset is not a file: ${asset}`);
      assets.add(asset);
    }
    for (const field of ['imports', 'dynamicImports']) {
      const references = row[field] || [];
      if (!Array.isArray(references) || references.some((value) => typeof value !== 'string')) {
        throw new Error(`Vite manifest ${field} must be a string array`);
      }
      for (const reference of references) await visit(reference);
    }
  }
  for (const entry of requiredEntries) {
    if (!manifest[entry] || manifest[entry].isEntry !== true) {
      throw new Error(`Vite manifest has no entry: ${entry}`);
    }
    await visit(entry);
  }
  // Vite emits URL-imported workers as standalone manifest rows which are not
  // connected through imports/dynamicImports. They are still part of the
  // deployable graph and must be published and retained.
  for (const key of Object.keys(manifest)) await visit(key);
  return assets;
}

async function atomicCopy(source, destination) {
  await mkdir(dirname(destination), { recursive: true });
  try {
    await open(destination, 'r').then((handle) => handle.close());
    return;
  } catch {
    // A content-hashed destination that is not present must be published.
  }
  const temporary = `${destination}.publish-${process.pid}-${Math.random().toString(16).slice(2)}`;
  await copyFile(source, temporary);
  await rename(temporary, destination);
}

async function publishManifest(path, value) {
  const temporary = `${path}.publish-${process.pid}`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
  await rename(temporary, path);
}

async function listFiles(directory, prefix = '') {
  let rows = [];
  try {
    rows = await readdir(directory, { withFileTypes: true });
  } catch (error) {
    if (error && error.code === 'ENOENT') return [];
    throw error;
  }
  const output = [];
  for (const row of rows) {
    const name = prefix ? `${prefix}/${row.name}` : row.name;
    if (row.isDirectory()) output.push(...await listFiles(join(directory, row.name), name));
    else if (row.isFile()) output.push(name);
  }
  return output;
}

async function main() {
  await mkdir(dirname(liveDir), { recursive: true });
  // Snapshot the live graph before invoking Vite. This also protects against
  // build-tool configuration drift that might otherwise touch the live outDir.
  let previousManifest = null;
  let previousAssets = new Set();
  try {
    previousManifest = await readJson(join(liveDir, 'manifest.json'));
    previousAssets = await validateManifest(previousManifest, liveDir);
  } catch {
    previousManifest = null;
    previousAssets = new Set();
  }

  const temporaryDir = await mkdtemp(join(dirname(liveDir), '.vite-build-'));
  try {
    process.env.TOFU_VITE_OUT_DIR = temporaryDir;
    await build({ configFile: join(root, 'vite.config.mjs') });
    const nextManifest = await readJson(join(temporaryDir, 'manifest.json'));
    const nextAssets = await validateManifest(nextManifest, temporaryDir);

    await mkdir(assetsDir, { recursive: true });
    for (const asset of nextAssets) {
      await atomicCopy(join(temporaryDir, asset), join(liveDir, asset));
    }

    if (previousManifest) {
      await publishManifest(join(liveDir, 'previous-manifest.json'), previousManifest);
    } else {
      await rm(join(liveDir, 'previous-manifest.json'), { force: true });
    }

    // The manifest is the commit point: every path it names is present first.
    await publishManifest(join(liveDir, 'manifest.json'), nextManifest);

    const retained = new Set([...nextAssets, ...previousAssets].map((path) => path.slice('assets/'.length)));
    for (const file of await listFiles(assetsDir)) {
      if (!retained.has(file)) await rm(join(assetsDir, ...file.split('/')), { force: true });
    }
    await validateManifest(await readJson(join(liveDir, 'manifest.json')), liveDir);
    process.stdout.write(`Published ${nextAssets.size} Vite assets; retained ${previousAssets.size} from the previous graph.\n`);
  } finally {
    delete process.env.TOFU_VITE_OUT_DIR;
    await rm(temporaryDir, { recursive: true, force: true });
  }
}

await main();
