#!/usr/bin/env node
/** Retained settings markup -> legacy server-injected static fragments.
 * Migrated panels are authored in frontend/src/settings-panels/. Older panels
 * retain their existing delivery until migrated; this generator never prunes.
 */
import { readFile, readdir, writeFile, mkdir, rename } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';

const root = resolve(import.meta.dirname, '..');
export async function composeSettingsPanels({ check = false, rootDirectory = root } = {}) {
  const source = join(rootDirectory, 'frontend/src/settings-panels');
  const destination = join(rootDirectory, 'static/settings_panels');
  const files = (await readdir(source)).filter((name) => /^[a-z_]+\.html$/.test(name)).sort();
  for (const name of files) {
    const text = await readFile(join(source, name), 'utf8');
    const output = join(destination, name);
    let previous = null;
    try { previous = await readFile(output, 'utf8'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (previous === text) continue;
    if (check) throw new Error(`Settings panel output is stale: static/settings_panels/${name}`);
    await mkdir(destination, { recursive: true });
    const temporary = `${output}.compose-${process.pid}`;
    await writeFile(temporary, text, 'utf8');
    await rename(temporary, output);
  }
  return files;
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const files = await composeSettingsPanels({ check: process.argv.includes('--check') });
  console.log(`Settings panel sources verified: ${files.length}`);
}
