#!/usr/bin/env node

import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { build } from 'vite';
import {
  mkdir, mkdtemp, readFile, readdir, rm, stat, utimes,
} from 'node:fs/promises';
import { dirname, join, relative, resolve } from 'node:path';
import { composeRuntime } from './compose_frontend_runtime.mjs';
import { checkLazyRuntimeBindings } from './check_lazy_runtime_bindings.mjs';
import { checkFiles as checkFrontendTdz, defaultRuntimePaths } from './check_frontend_tdz.mjs';
import { composeStyles } from './compose_frontend_styles.mjs';
import { createViteArtifactValidator, safeViteAsset } from './vite_asset_manifest.mjs';
import { publishViteGeneration } from './vite_asset_publication.mjs';
import { syncFrontendVendorAssets } from './frontend_vendor_assets.mjs';

import { generateSystemNotePresentation } from './gen_system_note_presentation.mjs';
import { syncSystemNotePresentationEmbed } from './check_note_presentation_parity.mjs';
import { composeSettingsPanels } from './compose_settings_panels.mjs';
import {
  generateI18nContract,
  readI18nCatalog,
} from './gen_i18n_contract.mjs';
import {
  planRetentionDeletions,
  retentionPolicyFromEnvironment,
} from './vite_asset_retention.mjs';

const root = resolve(import.meta.dirname, '..');

await import('../frontend/scripts/generate-rich-response.mjs');
const liveDir = join(root, 'static', 'vite');
const assetsDir = join(liveDir, 'assets');
const authoringDigestField = 'tofuAuthoringSha256';
const authoringInputs = JSON.parse(await readFile(
  join(root, 'frontend', 'authoring-inputs.json'), 'utf8',
));
if (!Array.isArray(authoringInputs.configPaths)
    || !Array.isArray(authoringInputs.sourceSuffixes)) {
  throw new Error('frontend/authoring-inputs.json has an invalid shape');
}
const requiredEntries = Object.values(authoringInputs.entries);
const artifactValidator = createViteArtifactValidator(requiredEntries);
const mainEntry = authoringInputs.entries.main;
const authoringConfigPaths = authoringInputs.configPaths;
const authoringSuffixes = new Set(authoringInputs.sourceSuffixes);

async function readJson(path) {
  return JSON.parse(await readFile(path, 'utf8'));
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

async function readViteAuthoringDigest() {
  const inputs = [];
  for (const relativePath of authoringConfigPaths) {
    try {
      const info = await stat(join(root, ...relativePath.split('/')));
      if (info.isFile()) inputs.push(relativePath);
    } catch (error) {
      if (!error || error.code !== 'ENOENT') throw error;
    }
  }
  for (const relativePath of await listFiles(join(root, 'frontend', 'src'))) {
    const basename = relativePath.slice(relativePath.lastIndexOf('/') + 1);
    const dot = basename.lastIndexOf('.');
    const suffix = dot >= 0 ? basename.slice(dot).toLowerCase() : '';
    if (authoringSuffixes.has(suffix)) {
      inputs.push(`frontend/src/${relativePath}`);
    }
  }
  inputs.sort();
  const digest = createHash('sha256');
  digest.update(Buffer.from('tofu-vite-authoring-v1\0', 'utf8'));
  for (const relativePath of inputs) {
    digest.update(Buffer.from(relativePath, 'utf8'));
    digest.update(Buffer.from([0]));
    digest.update(await readFile(join(root, ...relativePath.split('/'))));
    digest.update(Buffer.from([0]));
  }
  return digest.digest('hex');
}

/* Baseline bundle-size guard, post-manifest-validation: assert the primary
 * entry's gzip size stays under a ratcheted ceiling so an unbounded bundle
 * regression fails the build rather than slipping through unchecked. This is
 * deliberately lean — a single first-paint guard, not the full budget
 * framework (scripts/frontend_budget.py); that broader suite stays opt-in via
 * its own CI target. The ceiling is overridable via TOFU_BUILD_MAIN_MAX_KIB. */
const BUILD_MAIN_MAX_KIB = Math.max(1,
  Number.parseInt(process.env.TOFU_BUILD_MAIN_MAX_KIB || '', 10) || 620);

async function runBundleBudget(manifest, artifactDir) {
  const { gzipSync } = await import('node:zlib');
  const failures = [];
  for (const entry of requiredEntries) {
    const pending = [entry];
    if (authoringInputs.startupDynamicImports?.includes(entry)) {
      pending.push(...(manifest[entry]?.dynamicImports || []));
    }
    const seen = new Set();
    const assets = new Set();
    while (pending.length) {
      const key = pending.pop();
      if (seen.has(key)) continue;
      seen.add(key);
      const row = manifest[key];
      const asset = row && safeViteAsset(artifactDir, row.file);
      if (!asset) throw new Error(`frontend bundle budget: missing startup owner ${key}`);
      assets.add(asset);
      pending.push(...(row.imports || []));
    }
    let bytes = 0;
    for (const asset of assets) {
      bytes += gzipSync(await readFile(join(artifactDir, asset)), { level: 9 }).length;
    }
    const kib = bytes / 1024;
    process.stdout.write(`bundle budget: ${entry} = ${kib.toFixed(1)} KiB gzip `
      + `(ceiling ${BUILD_MAIN_MAX_KIB} KiB)\n`);
    if (kib > BUILD_MAIN_MAX_KIB) {
      failures.push(`${entry} is ${(kib - BUILD_MAIN_MAX_KIB).toFixed(1)} KiB over the build ceiling`);
    }
  }
  if (failures.length) {
    throw new Error('frontend bundle budget rejected the published graph: '
      + failures.join('; '));
  }
}

/** An explicit Playwright Node, or this process's binary when it really is
 *  Node. Empty means "let Playwright resolve its own driver", the correct
 *  fallback when ``process.execPath`` is a loader wrapper instead of Node. */
function playwrightNodePath() {
  const configured = process.env.PLAYWRIGHT_NODEJS_PATH;
  if (configured) return configured;
  return /(^|[\\/])node(\.exe)?$/.test(process.execPath) ? process.execPath : '';
}

async function runBundleCanary(artifactDir) {
  const python = process.env.TOFU_PYTHON || process.env.PYTHON || 'python3';
  const script = join(root, 'scripts', 'frontend_bundle_canary.py');
  await new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(
      python, [script, '--artifact-dir', artifactDir],
      {
        cwd: root,
        env: {
          ...process.env,
          // Reuse the Node already running this build: Playwright's bundled
          // driver may require a newer glibc than the host provides. Only when
          // execPath IS a node binary — under an APK-style toolchain it is the
          // musl loader (``…/ld-musl-x86_64.so.1``), and pointing the driver at
          // a loader kills it at startup with "Connection.init: Connection
          // closed while reading from the driver", failing EVERY build even
          // though the canary passes standalone.
          ...(playwrightNodePath()
            ? { PLAYWRIGHT_NODEJS_PATH: playwrightNodePath() } : {}),
        },
        stdio: 'inherit',
      },
    );
    child.once('error', rejectPromise);
    child.once('exit', (code, signal) => {
      if (code === 0) resolvePromise();
      else rejectPromise(new Error(
        `frontend bundle canary rejected unpublished graph `
        + `(exit=${String(code)}, signal=${String(signal)})`,
      ));
    });
  });
}

async function main() {
  await syncFrontendVendorAssets();
  await composeSettingsPanels();
  // Generate both render surfaces before runtime composition and digest capture.
  // Backend registry edits must never ship with a stale presentation map.
  await generateSystemNotePresentation();
  await syncSystemNotePresentationEmbed({ check: false });
  // The retained runtime is a delivery artifact. Compose it before Vite so a
  // clean checkout and every direct build entry point consume the section
  // manifest rather than depending on an accidentally fresh generated file.
  await composeRuntime();
  checkLazyRuntimeBindings();
  checkFrontendTdz(defaultRuntimePaths());
  await composeStyles();
  const i18nCatalog = await generateI18nContract();
  const authoringDigest = await readViteAuthoringDigest();
  await mkdir(dirname(liveDir), { recursive: true });
  // Snapshot the live graph before invoking Vite. This also protects against
  // build-tool configuration drift that might otherwise touch the live outDir.
  const { manifest: previousManifest, assets: previousAssets } =
    await artifactValidator.readPrevious(liveDir);

  const temporaryDir = await mkdtemp(join(dirname(liveDir), '.vite-build-'));
  try {
    process.env.TOFU_VITE_OUT_DIR = temporaryDir;
    await build({ configFile: join(root, 'vite.config.mjs') });
    const catalogAfterBuild = await readI18nCatalog();
    if (catalogAfterBuild.digest !== i18nCatalog.digest) {
      throw new Error('i18n locale sources changed during the frontend build; retry the build');
    }
    const authoringDigestAfterBuild = await readViteAuthoringDigest();
    if (authoringDigestAfterBuild !== authoringDigest) {
      throw new Error('frontend authoring inputs changed during the frontend build; retry the build');
    }
    const nextManifest = await readJson(join(temporaryDir, 'manifest.json'));
    // Keep source and deployable locale chunks in one atomic generation. The
    // server validates this digest before serving any Vite entry, so editing a
    // locale without rebuilding fails closed instead of silently serving the
    // previous language chunks.
    nextManifest[mainEntry].tofuI18nCatalogSha256 = i18nCatalog.digest;
    nextManifest[mainEntry][authoringDigestField] = authoringDigest;
    const nextAssets = await publishViteGeneration({
      manifest: nextManifest, artifactDir: temporaryDir, liveDir,
      previousManifest, validator: artifactValidator,
      checkBudget: runBundleBudget, checkBoot: runBundleCanary,
    });

    // Re-stamp the live graph: atomicCopy short-circuits existing names, so
    // without the touch a dropped file would keep its first-publication
    // mtime and lose most of its retention grace exactly when long-lived
    // tabs start being the only consumers of that generation.
    const buildTime = new Date();
    for (const asset of nextAssets) {
      await utimes(join(liveDir, asset), buildTime, buildTime).catch(() => {});
    }

    // The current and previous graphs stay pinned unconditionally; older
    // dropped generations survive within the bounded retention window so a
    // tab open across several rebuilds can still fetch its lazy chunks.
    const retained = new Set([...nextAssets, ...previousAssets].map((path) => path.slice('assets/'.length)));
    const retentionCandidates = [];
    for (const file of await listFiles(assetsDir)) {
      if (retained.has(file)) continue;
      try {
        const info = await stat(join(assetsDir, ...file.split('/')));
        retentionCandidates.push({
          name: file,
          size: info.size,
          mtimeMs: info.mtimeMs,
        });
      } catch (error) {
        // A concurrent build may prune between the listing and the stat;
        // an already-gone candidate needs no deletion plan entry.
        if (!error || error.code !== 'ENOENT') throw error;
      }
    }
    const deletions = planRetentionDeletions(
      retentionCandidates,
      Date.now(),
      retentionPolicyFromEnvironment(),
    );
    for (const name of deletions) {
      await rm(join(assetsDir, ...name.split('/')), { force: true });
    }
    await artifactValidator.validateCandidate(await readJson(join(liveDir, 'manifest.json')), liveDir);
    process.stdout.write(
      `Published ${nextAssets.size} Vite assets; retained ${previousAssets.size} from the previous graph; `
      + `kept ${retentionCandidates.length - deletions.length} older-generation files, pruned ${deletions.length}.\n`,
    );
  } finally {
    delete process.env.TOFU_VITE_OUT_DIR;
    await rm(temporaryDir, { recursive: true, force: true });
  }
}

await main();
