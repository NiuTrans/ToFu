#!/usr/bin/env node

/**
 * i18n dead-key gate (complement to check_i18n_usage.mjs).
 *
 * check_i18n_usage.mjs is a validity gate: every referenced key must exist in
 * the locale authorities. This script walks the other direction: every key in
 * en.json/zh.json must be reachable somewhere the browser can receive it, so
 * unused keys surface instead of accumulating silently. Reachability scans
 * every surface that can ship a key to the client:
 *   - frontend/src TS/JS/TSX (AST string-literal union — a superset of the
 *     translator call sites, plus template-literal dynamic families)
 *   - frontend/src test harnesses (.test.* / .cjs — they are type-checked and
 *     assert on shipped literals)
 *   - HTML templates / fragments (data-i18n* attributes, incl. -aria-label)
 *   - Python lib/ + routes/ sources (exact catalog literals — the server emits
 *     detailKey/summaryKey/reasonKey payloads the HUD localizes client-side)
 *   - tests/ (pytest/vitest assert on shipped key literals)
 *
 * Dynamic key families like t('swarm.phase.' + phase) and
 * t(`err.k.${kind}.title`) never yield static keys; concatenation prefixes are
 * allowlisted below and template-literal heads are auto-discovered from the
 * AST. Add to the allowlist only when the concatenation site is real and
 * audited — it is the gate's false-positive boundary.
 *
 * Exit 0 always (WARNING gate). --fail exits 1 when dead keys exist.
 * --json prints the dead-key list as JSON for follow-up pruning passes.
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { readI18nCatalog } from './gen_i18n_contract.mjs';

const require = createRequire(import.meta.url);
const ts = require('typescript');
const execFileAsync = promisify(execFile);
const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const frontendRoot = path.join(repositoryRoot, 'frontend/src');
const dataAttributePattern = /data-i18n(?:-html|-placeholder|-title|-aria-label)?\s*=\s*["']([^"']+)["']/g;
const sourceExtensions = new Set(['.ts', '.js', '.tsx', '.cjs', '.mjs']);

// Concatenated prefix families with a verified call site; keys under these
// prefixes are reachable only at runtime, so static scanning cannot see them.
// Keep each entry paired with the call site that justifies it.
const DYNAMIC_PREFIXES = [
  // features/settings/tools-inventory.ts — localized(prefix, registry ID)
  'toolsInv.tool.',
  'toolsInv.family.',
  // background-activity-presentation.ts, agent-sidebar-dock.ts,
  // agent-sidebar-dock-projection.ts, conversation/ui/renderers/text-blocks.ts
  'swarm.phase.',
  // features/working-facts/view.ts — also t('workingFacts.' + action) for the
  // correct/confirm/retract action buttons
  'workingFacts.',
  // features/settings/provider-template-picker.ts
  'providerCatalog.',
  // rich-response/surface-blocks.ts, rich-response/resource-panel.ts
  'rich.surface.',
  // features/evolution/research/components/live-library.ts
  'evolution.research.library.',
  // rich-response/artifact-review.ts (ports.text prefix adapters)
  'artifactReview.', 'turnReview.',
  // runtime/sections/update.js
  'update.preparation.', 'update.phase.',
  // features/conversation-bar/context-meter.ts
  'conversationBar.',
  // runtime/sections/compaction-viewer.js
  'compactionViewer.trigger.', 'compactionViewer.source.',
  // runtime/sections/ui/finish_info_rich.js, runtime/sections/ui/finish_info.js
  'finishInfo.cbState.', 'finishInfo.evidence.', 'finishInfo.cb.',
  // runtime/sections/net-latency.js
  'net.state.',
  // runtime/sections/project-brain-integration.js (_pbiText prefix adapter:
  // every label resolves through 'projectBrain.integration.' + key)
  'projectBrain.integration.',
  // runtime/sections/settings/other_tabs.js
  'settings.costExperimentBlocker.',
];

async function* walkScriptFiles(directory) {
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      yield* walkScriptFiles(absolute);
      continue;
    }
    if (!entry.isFile() || !sourceExtensions.has(path.extname(entry.name))) continue;
    if (entry.name === 'app-runtime.js' || entry.name.includes('.generated.')) continue;
    yield absolute;
  }
}

// A catalog key is referenced when any string literal in a scanned source
// equals it, regardless of which helper (t, textFor, ports.text, …) consumes
// the literal. Template-literal heads (`prefix.${expr}`) are recorded as
// dynamic prefixes: every catalog key under one is reachable at runtime.
function collectScriptKeys(filePath, source, referenced, catalogSet, dynamicPrefixes) {
  const scriptKind = filePath.endsWith('.ts') || filePath.endsWith('.tsx')
    ? ts.ScriptKind.TS : ts.ScriptKind.JS;
  const sourceFile = ts.createSourceFile(
    filePath, source, ts.ScriptTarget.Latest, true, scriptKind,
  );
  function visit(node) {
    if ((ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node))
        && catalogSet.has(node.text)) {
      referenced.add(node.text);
    }
    if (ts.isTemplateExpression(node) && node.head.text
        && node.head.text.includes('.') && /^[A-Za-z][A-Za-z0-9_.-]*$/.test(node.head.text)) {
      dynamicPrefixes.add(node.head.text);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  for (const match of source.matchAll(dataAttributePattern)) referenced.add(match[1]);
}

async function optionalHtmlFiles() {
  const files = [path.join(repositoryRoot, 'index.html'), path.join(repositoryRoot, 'static/admin.html')];
  const htmlDirectories = [
    path.join(repositoryRoot, 'static/settings_panels'),
    path.join(repositoryRoot, 'frontend/src/application-shell/fragments'),
  ];
  for (const directory of htmlDirectories) {
    try {
      for (const name of await fs.readdir(directory)) {
        if (name.endsWith('.html')) files.push(path.join(directory, name));
      }
    } catch (error) {
      if (!error || error.code !== 'ENOENT') throw error;
    }
  }
  return files.sort();
}

// Server-side + test scan: Python emitters ship dotted key literals the HUD
// localizes, and pytest/vitest harnesses assert on shipped literals. A raw
// exact-catalog-literal scan over tracked files is sufficient — i18n keys are
// distinctive dotted names, so a quoted literal match is unambiguous.
async function collectTextKeys(referenced, catalogSet) {
  let stdout;
  try {
    ({ stdout } = await execFileAsync(
      'git', ['ls-files', 'lib', 'routes', 'tests'],
      { cwd: repositoryRoot, maxBuffer: 64 * 1024 * 1024 },
    ));
  } catch {
    return; // not a git checkout (or git unavailable): skip the text scan
  }
  const files = stdout.split('\n').filter(
    (name) => /\.(py|json|js|cjs|mjs|ts|html)$/.test(name),
  );
  await Promise.all(files.map(async (relative) => {
    let source;
    try {
      source = await fs.readFile(path.join(repositoryRoot, relative), 'utf8');
    } catch {
      return;
    }
    for (const match of source.matchAll(/["'`]([A-Za-z][A-Za-z0-9_.-]*\.[A-Za-z0-9_.-]+)["'`]/g)) {
      if (catalogSet.has(match[1])) referenced.add(match[1]);
    }
  }));
}

async function referencedKeys(catalogSet) {
  const referenced = new Set();
  const dynamicPrefixes = new Set();
  for await (const filePath of walkScriptFiles(frontendRoot)) {
    collectScriptKeys(
      filePath, await fs.readFile(filePath, 'utf8'), referenced, catalogSet, dynamicPrefixes,
    );
  }
  for (const filePath of await optionalHtmlFiles()) {
    try {
      const source = await fs.readFile(filePath, 'utf8');
      for (const match of source.matchAll(dataAttributePattern)) referenced.add(match[1]);
    } catch (error) {
      if (!error || error.code !== 'ENOENT') throw error;
    }
  }
  await collectTextKeys(referenced, catalogSet);
  for (const key of catalogSet) {
    for (const prefix of dynamicPrefixes) {
      if (key.startsWith(prefix)) { referenced.add(key); break; }
    }
  }
  return referenced;
}

export async function findDeadKeys() {
  const catalog = await readI18nCatalog();
  const catalogSet = new Set(catalog.keys);
  const referenced = await referencedKeys(catalogSet);
  const livePrefixes = DYNAMIC_PREFIXES.filter(
    (prefix) => catalog.keys.some((key) => key.startsWith(prefix)),
  );
  const dead = catalog.keys.filter(
    (key) => !referenced.has(key) && !livePrefixes.some((prefix) => key.startsWith(prefix)),
  );
  return { catalog, referenced, livePrefixes, dead };
}

async function main() {
  const fail = process.argv.includes('--fail');
  const json = process.argv.includes('--json');
  const { catalog, livePrefixes, dead } = await findDeadKeys();
  if (json) {
    process.stdout.write(`${JSON.stringify({ deadKeys: dead, dynamicPrefixes: livePrefixes }, null, 2)}\n`);
    if (fail && dead.length) process.exitCode = 1;
    return;
  }
  const sample = dead.slice(0, 40);
  process.stdout.write(
    `i18n dead-key scan: ${catalog.keys.length} declared keys, `
    + `${livePrefixes.length} dynamic prefix families allowlisted, `
    + `${dead.length} dead key(s) not referenced in frontend/src, scanned HTML, Python emitters, or tests.\n`
    + (sample.length ? `sample:\n${sample.map((key) => `  ${key}`).join('\n')}\n` : '')
    + (dead.length > sample.length ? `  … ${dead.length - sample.length} more\n` : '')
    + (fail ? '' : 'WARNING only: this gate does not fail builds yet (use --fail to enforce).\n'),
  );
  if (fail && dead.length) process.exitCode = 1;
}

const invokedPath = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : '';
if (invokedPath === import.meta.url) {
  await main();
}
