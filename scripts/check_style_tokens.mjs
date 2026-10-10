#!/usr/bin/env node

/**
 * Design-token gate for authored CSS (docs/DESIGN_SYSTEM.md).
 *
 * Files outside the legacy allowlist or an unchanged manifest-bound copy use tokens:
 *   1. no hex colors (`#abc` / `#aabbccdd`)
 *   2. no rgba()/hsla() paints
 *   3. no raw px in margin/padding/gap/inset properties (0 is fine)
 *   4. (ui kit only) no [data-theme] selectors — themes adapt via tokens
 * The token layer itself (00-design-tokens.css) is exempt from rules 1–2:
 * defining colors is its job.
 *
 * Also asserts the composed-stylesheet size budget (personal-computer
 * resource budget: CSS is shipped to every session).
 *
 * Legacy files are allow-listed explicitly; the list only ever SHRINKS.
 * Split copies have exact manifest hashes, never a blanket exemption.
 * (phase-4 migration). A file added under frontend/src/styles that is not
 * listed here is gated automatically — that is the point.
 *
 * Exit 0 = clean; exit 1 = violations (printed with file:line).
 */

import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { styleTokenViolations, unchangedLegacyTokenCopy } from './style_token_policy.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

/** Legacy allowlist — every pre-design-system sheet. Only shrinks. */
const LEGACY_ALLOWLIST = new Set([
  'application/01-shell-navigation.css',
  // Byte-moved splits of allowlisted legacy sheets (2026-09, 100 KiB
  // composition budget): same pre-design-system bytes, new filename — this
  // is a rename-following entry, not new allowlist surface.
  'application/27-evolution-research.css',
  'application/03-paper-reader-streaming.css',
  'application/03-paper-reader-responsive.css',
  'application/04-paper-reader-themes.css',
  'application/05-paper-report-media.css',
  'application/06-file-changes-settings-shell.css',
  'application/07-settings-providers-onboarding.css',
  'application/11-tofu-theme-foundation.css',
  'application/12-tofu-theme-project-controls.css',
  'application/13-tofu-theme-composer-tools.css',
  'application/14-bridges-memory-branches.css',
  'application/15-queue-mobile-my-day.css',
  'application/16-my-day-context-indicators.css',
  'application/17-compaction-context-rail.css',
  'application/18-artifacts-knowledge-library.css',
  'application/19-project-brain.css',
  'application/21-local-knowledge-plan-mode.css',
  'application/22-shell-mobile-brand.css',
  'application/25-conversation-bar.css',
  'application/26-evolution.css',
  'settings/01-core-panels.css',
  'settings/02-mcp-apps.css',
  'settings/03-model-visibility.css',
  'settings/04-search-network-advanced.css',
  'settings/05-remote-devices.css',
  'settings/06-model-routing-v2.css',
  'settings/07-access-matrix.css',
  'settings/08-local-deploy.css',
  'settings/09-dropdown.css',
]);

/** The token layer defines colors; everyone else consumes them. */
const COLOR_DEFINITION_FILES = new Set(['application/00-design-tokens.css']);
/** The kit must stay theme-agnostic (rule 4 applies only to it). */
const THEME_AGNOSTIC_FILES = new Set(['application/30-ui-kit.css']);

/** Composed-sheet budgets in bytes (docs/DESIGN_SYSTEM.md#budgets). */
const SHEET_BUDGETS = new Map([
  ['static/styles.css', 1_500_000],
  ['static/settings.css', 300_000],
]);

async function listCssFiles(dir, prefix) {
  const entries = await fs.readdir(path.join(root, dir), { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    if (entry.isFile() && entry.name.endsWith('.css') && entry.name !== 'manifest.json') {
      files.push(`${prefix}/${entry.name}`);
    }
  }
  return files;
}

async function main() {
  const files = [
    ...(await listCssFiles('frontend/src/styles/application', 'application')),
    ...(await listCssFiles('frontend/src/styles/settings', 'settings')),
  ];

  // Manifest hashes admit unchanged byte copies from a retired giant sheet.
  // A changed owner automatically returns to strict tokens; hashes never grant
  // an open-ended exemption and may not be refreshed for new raw declarations.
  const frozen = new Map();
  for (const domain of ['application', 'settings']) {
    const manifest = JSON.parse(await fs.readFile(path.join(root,
      'frontend/src/styles', domain, 'manifest.json'), 'utf8'));
    for (const row of [...manifest.sections, ...(manifest.deferredSections ?? [])]) {
      if (row.legacyTokenSha256 === undefined) continue;
      const rel = `${domain}/${row.path}`;
      if (!files.includes(rel) || frozen.has(rel)) throw new Error(`invalid frozen stylesheet owner: ${rel}`);
      // Validate the digest even when the file itself already passes tokens.
      unchangedLegacyTokenCopy('', row.legacyTokenSha256);
      frozen.set(rel, row.legacyTokenSha256);
    }
  }
  const violations = [];
  let unchangedCopies = 0;
  for (const rel of files) {
    if (LEGACY_ALLOWLIST.has(rel)) continue;
    const text = await fs.readFile(path.join(root, 'frontend/src/styles', rel), 'utf8');
    if (unchangedLegacyTokenCopy(text, frozen.get(rel))) { unchangedCopies++; continue; }
    violations.push(...styleTokenViolations(text, rel, {
      allowColor: COLOR_DEFINITION_FILES.has(rel), themeAgnostic: THEME_AGNOSTIC_FILES.has(rel),
    }));
  }

  const budgetNotes = [];
  for (const [sheet, budget] of SHEET_BUDGETS) {
    const size = (await fs.stat(path.join(root, sheet))).size;
    budgetNotes.push(`${sheet} ${size}B / ${budget}B`);
    if (size > budget) {
      violations.push(`${sheet} ${size}B exceeds budget ${budget}B (docs/DESIGN_SYSTEM.md#budgets)`);
    }
  }

  if (violations.length > 0) {
    console.error(`style-token gate: ${violations.length} violation(s)`);
    for (const violation of violations) console.error(`  - ${violation}`);
    process.exit(1);
  }
  console.log(
    `style-token gate: clean (${files.length - LEGACY_ALLOWLIST.size - unchangedCopies} gated files, `
    + `${LEGACY_ALLOWLIST.size} legacy allow-listed, ${unchangedCopies} unchanged legacy copies, ${budgetNotes.join(', ')})`);
}

await main();
