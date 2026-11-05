/** Own candidate admission and snapshots of previously published asset graphs.
 * Entry points: createViteArtifactValidator, safeViteAsset. Dependencies: filesystem, paths,
 * and the explicit current entry contract; no publication or retention policy.
 */
import { readFile, stat } from 'node:fs/promises';
import { join, resolve, sep } from 'node:path';

export function safeViteAsset(baseDir, value) {
  if (typeof value !== 'string' || !value.startsWith('assets/') || value.includes('\\')) return '';
  const root = resolve(baseDir);
  const resolved = resolve(root, value);
  const prefix = root.endsWith(sep) ? root : `${root}${sep}`;
  return resolved.startsWith(prefix) && !value.split('/').includes('..') ? value : '';
}

async function validateGraph(manifest, baseDir, entries) {
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
      const asset = safeViteAsset(baseDir, value);
      if (!asset) throw new Error(`Unsafe Vite asset path: ${String(value)}`);
      const info = await stat(join(baseDir, asset));
      if (!info.isFile()) throw new Error(`Vite asset is not a file: ${asset}`);
      assets.add(asset);
    }
    for (const field of ['imports', 'dynamicImports']) {
      const references = row[field] || [];
      if (!Array.isArray(references) || references.some(value => typeof value !== 'string')) {
        throw new Error(`Vite manifest ${field} must be a string array`);
      }
      for (const reference of references) await visit(reference);
    }
  }
  for (const entry of entries) {
    if (!manifest[entry] || manifest[entry].isEntry !== true) {
      throw new Error(`Vite manifest has no entry: ${entry}`);
    }
    await visit(entry);
  }
  // URL workers and other declared assets also belong to the complete graph.
  for (const key of Object.keys(manifest)) await visit(key);
  return assets;
}

export function createViteArtifactValidator(requiredEntries) {
  const validateCandidate = (manifest, baseDir) => validateGraph(manifest, baseDir, requiredEntries);
  async function readPrevious(baseDir) {
    try {
      const manifest = JSON.parse(await readFile(join(baseDir, 'manifest.json'), 'utf8'));
      // An intact older generation predates newly registered entries. Its own
      // declared entries and every referenced asset must still remain pinned.
      const entries = Object.keys(manifest).filter(key => manifest[key]?.isEntry === true);
      if (entries.length === 0) throw new Error('Previous Vite manifest has no entry');
      const assets = await validateGraph(manifest, baseDir, entries);
      return { manifest, assets };
    } catch {
      return { manifest: null, assets: new Set() };
    }
  }
  return { validateCandidate, readPrevious };
}
