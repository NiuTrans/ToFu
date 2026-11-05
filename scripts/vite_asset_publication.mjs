/** Admit an unpublished graph before copying assets and atomically committing
 * its manifest. Entry: publishViteGeneration. Dependencies: explicit graph
 * validator, budget and browser checks, filesystem; no retention decisions.
 */
import { copyFile, mkdir, open, rename, rm, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';

async function atomicCopy(source, destination) {
  await mkdir(dirname(destination), { recursive: true });
  try {
    await open(destination, 'r').then(handle => handle.close());
    return;
  } catch {
    // Content-hashed assets already present belong to a published generation.
  }
  const temporary = `${destination}.publish-${process.pid}-${Math.random().toString(16).slice(2)}`;
  await copyFile(source, temporary);
  await rename(temporary, destination);
}

async function publishManifest(path, value) {
  const temporary = `${path}.publish-${process.pid}-${Math.random().toString(16).slice(2)}`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
  await rename(temporary, path);
}

export async function publishViteGeneration({
  manifest, artifactDir, liveDir, previousManifest, validator, checkBudget, checkBoot,
}) {
  const assets = await validator.validateCandidate(manifest, artifactDir);
  await checkBudget(manifest, artifactDir);
  await checkBoot(artifactDir);
  for (const asset of assets) {
    await atomicCopy(join(artifactDir, asset), join(liveDir, asset));
  }
  if (previousManifest) {
    await publishManifest(join(liveDir, 'previous-manifest.json'), previousManifest);
  } else {
    await rm(join(liveDir, 'previous-manifest.json'), { force: true });
  }
  // Every referenced byte is present before the atomic serving commit.
  await publishManifest(join(liveDir, 'manifest.json'), manifest);
  return assets;
}
