#!/usr/bin/env node

/**
 * Parity gate: the retained-JS runtime cannot import the generated TS
 * contract, so it embeds a mirror of the backend registry's
 * InjectionSpec.presentation between BEGIN/END GENERATED markers in
 * `frontend/src/runtime/sections/ui/tool_rounds.js`. This gate regenerates
 * the authoritative map from the registry (same source as
 * `gen_system_note_presentation.mjs`) and fails if the embed has drifted —
 * keeping the single-source-of-truth invariant across both render surfaces.
 */

import { execFileSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const runtimePath = path.join(repositoryRoot, 'frontend/src/runtime/sections/ui/tool_rounds.js');

const REGISTRY_READER = String.raw`
import json
from lib.tasks_pkg.context_composer.registry import note_presentations
print(json.dumps({
    reason: {'labelKey': p.label_key, 'tone': p.tone}
    for reason, p in note_presentations().items()
}, sort_keys=True))
`;

function readRegistry() {
  const stdout = execFileSync('python3', ['-B', '-c', REGISTRY_READER],
    { cwd: repositoryRoot, encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 });
  return JSON.parse(stdout);
}

function renderEmbed(registry) {
  const reasons = Object.keys(registry).sort();
  const lines = reasons.map((reason) => {
    const p = registry[reason];
    return `  ${reason}: { labelKey: '${p.labelKey}', tone: '${p.tone}' },`;
  });
  return `const _SEG_NOTE_REASON_CHIP = {\n${lines.join('\n')}\n};`;
}

function extractEmbed(source) {
  const match = source.match(
    /BEGIN GENERATED \(registry note_presentations\) \*\/\s*\n([\s\S]*?)\n\/\* END GENERATED \*\//);
  if (!match) {
    throw new Error('GENERATED markers not found in tool_rounds.js — the embed was edited by hand');
  }
  return match[1].trim();
}

async function main() {
  const mode = process.argv[2] || '--check';
  if (!['--check', '--write'].includes(mode)) {
    throw new Error('usage: check_note_presentation_parity.mjs [--check|--write]');
  }
  const registry = readRegistry();
  const expected = renderEmbed(registry);
  const source = await fs.readFile(runtimePath, 'utf8');
  const actual = extractEmbed(source);
  if (actual !== expected) {
    if (mode === '--write') {
      const marker = /(BEGIN GENERATED \(registry note_presentations\) \*\/\s*\n)([\s\S]*?)(\n\/\* END GENERATED \*\/)/g;
      if ([...source.matchAll(marker)].length !== 1) {
        throw new Error('Expected exactly one owned note-presentation embed');
      }
      await fs.writeFile(runtimePath, source.replace(marker,
        (_match, before, _old, after) => before + expected + after), 'utf8');
      process.stdout.write('system-note runtime presentation embed generated\n');
      return;
    }
    process.stderr.write(
      'system-note reason chip embed in tool_rounds.js has drifted from the registry.\n'
      + 'Regenerate the typed contract with `npm run generate:note-presentation` and '
      + 'sync the GENERATED block (or run with --write).\n',
    );
    process.exit(1);
  }
  process.stdout.write('system-note presentation parity verified (registry ↔ TS ↔ JS runtime)\n');
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
