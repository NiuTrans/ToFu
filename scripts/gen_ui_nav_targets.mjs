#!/usr/bin/env node

/** Generate contracts/ui_nav_targets_v1.json — the model-facing UI-jump
 * directory — from the frontend registries (single source of truth):
 *
 *   - frontend/src/features/settings/setting-jump.ts  (SETTING_JUMP_TARGETS)
 *   - frontend/src/features/ui-nav.ts                 (PAGE_JUMP_TARGETS)
 *
 * Names/descriptions follow DOM ownership in the existing panel/tab markup,
 * resolving data-i18n, data-i18n-html, and literal label text. Ambiguous or
 * missing associations fail generation; freshness is not semantic proof.
 * The backend context-composer block (lib/tasks_pkg/context_composer/_providers.py)
 * renders this file into the system prompt; the model answers with links
 * like `[打开调试模式](setting:debug-mode)` / `[打开记忆](page:memory)`.
 *
 * Usage: --write (default) rewrites the contract; --check exits 1 on drift.
 */

import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUTPUT = 'contracts/ui_nav_targets_v1.json';

function readRepo(rel) {
  return fs.readFileSync(path.join(repoRoot, rel), 'utf8');
}

/* Extract a plain string-valued registry literal from a TS module:
 * `export const NAME = Object.freeze({ 'key': { ... }, ... })`. Registries
 * are pure literals by design (pinned by setting-jump.test.ts), so a static
 * AST walk is exact — no eval. */
function extractRegistry(sourcePath, exportName) {
  const source = readRepo(sourcePath);
  const sf = ts.createSourceFile(
    sourcePath, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS,
  );
  let found = null;
  const visit = (node) => {
    if (found) return;
    if (ts.isVariableStatement(node)) {
      for (const decl of node.declarationList.declarations) {
        if (!ts.isIdentifier(decl.name) || decl.name.text !== exportName
            || !decl.initializer) continue;
        let init = decl.initializer;
        if (ts.isCallExpression(init)
            && ts.isPropertyAccessExpression(init.expression)
            && init.expression.getText(sf) === 'Object.freeze') {
          init = init.arguments[0];
        }
        if (init && (ts.isAsExpression(init) || ts.isSatisfiesExpression(init))) {
          init = init.expression;
        }
        if (init && ts.isObjectLiteralExpression(init)) found = init;
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  if (!found) throw new Error(`${exportName} not found in ${sourcePath}`);
  const entries = [];
  for (const prop of found.properties) {
    if (!ts.isPropertyAssignment(prop)
        || !ts.isObjectLiteralExpression(prop.initializer)) continue;
    const key = prop.name.getText(sf).replace(/^['"]|['"]$/g, '');
    const value = {};
    for (const field of prop.initializer.properties) {
      if (!ts.isPropertyAssignment(field)) continue;
      const fieldName = field.name.getText(sf).replace(/^['"]|['"]$/g, '');
      const init = field.initializer;
      if (ts.isStringLiteral(init) || ts.isNoSubstitutionTemplateLiteral(init)) {
        value[fieldName] = init.text;
      }
    }
    entries.push([key, value]);
  }
  return entries;
}

const ROW = '.settings-field, .settings-toggle-row';
const HEADING = '.settings-section-title, .search-pipeline-title, summary';
const DESCRIPTION = '.settings-field-hint, .settings-toggle-desc, .settings-desc';
const SECTION_BOUNDARY = `${HEADING}, ${ROW}, .settings-divider, .settings-row, .settings-group, details`;
const NON_LABEL = 'input, select, textarea, option, button, svg, script, style, template, [aria-hidden="true"]';

function only(elements, context) {
  if (elements.length !== 1) {
    throw new Error(`${context}: expected one owning label/element, found ${elements.length}`);
  }
  return elements[0];
}

function byId(root, id) {
  return only([...root.querySelectorAll('[id]')].filter((el) => el.id === id), `#${id}`);
}

function normalize(text) {
  return text.replace(/\s+/g, ' ').trim();
}

/* Plain model text, never markup or option/placeholder/action text. Parsing
 * fragments is inert: no scripts run and no resources are fetched. */
function richText(html) {
  const fragment = JSDOM.fragment(html);
  fragment.querySelectorAll(NON_LABEL).forEach((el) => el.remove());
  fragment.querySelectorAll('br').forEach((el) => el.replaceWith(' '));
  return fragment.textContent;
}

function localize(elements, catalogs) {
  return Object.fromEntries(['zh', 'en'].map((locale) => {
    const textOf = (node, root) => {
      if (node.nodeType === 3) return node.textContent;
      if (node.nodeType !== 1 || (node !== root && node.matches(NON_LABEL))) return '';
      const key = node.getAttribute('data-i18n') ?? node.getAttribute('data-i18n-html');
      if (key) {
        const text = catalogs[locale][key];
        if (typeof text !== 'string' || !text.trim()) {
          throw new Error(`Missing ${locale} translation for ${key}`);
        }
        return node.hasAttribute('data-i18n-html') ? richText(text) : text;
      }
      if (node.tagName === 'BR') return ' ';
      return [...node.childNodes].map((child) => textOf(child, root)).join('');
    };
    const text = normalize(elements.map((el) => textOf(el, el)).join(' '));
    if (!text) throw new Error(`Empty ${locale} owning label/description`);
    return [locale, text];
  }));
}

function matching(roots, selector) {
  return roots.flatMap((root) => [
    ...(root.matches(selector) ? [root] : []), ...root.querySelectorAll(selector),
  ]);
}

function hasSectionControl(root) {
  return matching([root], 'input, select, textarea').some((control) => {
    if (control.matches('input[type="hidden"]')) return false;
    // Hidden companions are inert, unlike direct or wrapped visible controls.
    for (let node = control; node; node = node.parentElement) {
      if (node.hasAttribute('hidden') || /display\s*:\s*none/i.test(node.getAttribute('style') || '')) return false;
      if (node === root) break;
    }
    return true;
  });
}

/* Section-owned controls need not be in a field row: providers live inside
 * headings, system prompt follows a heading, and pricing belongs to a
 * disclosure summary. Walk actual sibling/ancestor boundaries, never back
 * through a closed field/group/disclosure or across an intervening control. */
function sectionOwner(control) {
  const heading = control.closest(HEADING);
  if (heading) return { labels: [heading], scope: [heading] };
  section: for (let branch = control; branch?.parentElement; branch = branch.parentElement) {
    if (branch.matches('.settings-tab-panel, details')) break;
    const scope = [branch];
    for (let sibling = branch.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
      if (sibling.matches(HEADING)) {
        for (let next = branch.nextElementSibling; next && !next.matches(SECTION_BOUNDARY)
            && !hasSectionControl(next); next = next.nextElementSibling) {
          scope.push(next);
        }
        return { labels: [sibling], scope };
      }
      if (sibling.matches(SECTION_BOUNDARY) || sibling.matches('input, select, textarea')
          || sibling.querySelector('input, select, textarea')) break section;
      scope.unshift(sibling);
    }
  }
  throw new Error(`#${control.id}: no owning label, heading, or disclosure summary`);
}

/* Exported for hermetic structural fixtures; registry keys never special-case
 * label text. Explicit associations win, followed by the closest actual row
 * or section. An unlabeled row fails instead of borrowing its parent's name. */
export function settingLabel(panel, elementId, catalogs) {
  const control = byId(panel, elementId);
  const referenced = (attribute) => {
    const ids = control.getAttribute(attribute)?.trim();
    return ids ? ids.split(/\s+/).map((id) => byId(panel, id)) : [];
  };
  const row = control.closest(ROW);
  let labels = referenced('aria-labelledby');
  if (!labels.length) {
    const explicit = [...panel.querySelectorAll('label[for]')]
      .filter((label) => label.htmlFor === elementId);
    if (explicit.length) labels = [only(explicit, `#${elementId} label[for]`)];
  }
  let scope;
  if (row) {
    scope = [row];
    if (!labels.length) {
      const candidates = [...row.querySelectorAll(
        'label:not(.stg-toggle), .settings-toggle-label, .settings-field-label, .settings-section-title',
      )].filter((label) => label.closest(ROW) === row
        && (!label.hasAttribute('for') || label.htmlFor === elementId));
      labels = [only(candidates, `#${elementId} row`)];
    }
  } else if (labels.length) {
    scope = []; // Only explicitly associated descriptions outside known rows.
  } else {
    ({ labels, scope } = sectionOwner(control));
  }
  let descriptions = referenced('aria-describedby');
  if (!descriptions.length) {
    descriptions = matching(scope, DESCRIPTION).filter((el) =>
      el.closest(ROW) === row
      && el.closest('details') === control.closest('details')
      && !labels.some((label) => label.contains(el))
      && ![...panel.querySelectorAll('[aria-describedby]')].some((other) => other !== control
        && el.id && other.getAttribute('aria-describedby').trim().split(/\s+/).includes(el.id)));
    if (descriptions.length > 1) only(descriptions, `#${elementId} description`);
  }
  return {
    name: localize(labels, catalogs),
    ...(descriptions.length ? { description: localize(descriptions, catalogs) } : {}),
  };
}

function tabLabel(shell, tabId, catalogs) {
  const button = only([...shell.querySelectorAll('button.settings-tab[data-tab]')]
    .filter((el) => el.dataset.tab === tabId), `settings tab ${tabId}`);
  // Includes literal labels such as MCP without a separate hardcoded map.
  return localize([button], catalogs);
}

const zhCatalog = JSON.parse(readRepo('frontend/src/i18n/locales/zh.json'));
const enCatalog = JSON.parse(readRepo('frontend/src/i18n/locales/en.json'));

const catalogs = { zh: zhCatalog, en: enCatalog };

function bilingual(i18nKey) {
  if (!i18nKey) return undefined;
  const zh = zhCatalog[i18nKey];
  const en = enCatalog[i18nKey];
  if (!zh && !en) return undefined;
  return { zh: zh ?? en, en: en ?? zh };
}

function buildTargets() {
  const shell = JSDOM.fragment(readRepo('index.html'));
  const panelCache = new Map();
  const readPanel = (tab) => {
    if (!panelCache.has(tab)) {
      panelCache.set(tab, JSDOM.fragment(readRepo(`static/settings_panels/${tab}.html`)));
    }
    return panelCache.get(tab);
  };

  const targets = [];
  const problems = [];

  const settingEntries = extractRegistry(
    'frontend/src/features/settings/setting-jump.ts', 'SETTING_JUMP_TARGETS');
  for (const [key, target] of settingEntries) {
    const entry = { key, scheme: 'setting', tab: target.tab };
    try {
      if (target.elementId) {
        Object.assign(entry, settingLabel(readPanel(target.tab), target.elementId, catalogs));
      } else {
        entry.tabLevel = true;
        entry.name = tabLabel(shell, target.tab, catalogs);
      }
    } catch (error) {
      problems.push(`setting:${key} — ${error.message}`);
    }
    targets.push(entry);
  }

  const pageEntries = extractRegistry(
    'frontend/src/features/ui-nav.ts', 'PAGE_JUMP_TARGETS');
  for (const [key, target] of pageEntries) {
    const entry = { key, scheme: 'page', name: bilingual(target.nameKey) };
    if (!entry.name) problems.push(`page:${key} — nameKey ${target.nameKey} unresolved`);
    if (target.requiresFlag) entry.requiresFlag = target.requiresFlag;
    targets.push(entry);
  }

  if (problems.length) {
    throw new Error(`ui-nav directory unresolved:\n  - ${problems.join('\n  - ')}`);
  }
  return targets;
}

function buildDocument() {
  return {
    $comment: 'GENERATED by scripts/gen_ui_nav_targets.mjs — do not edit. '
      + 'Sources: frontend/src/features/settings/setting-jump.ts, '
      + 'frontend/src/features/ui-nav.ts (registries), '
      + 'static/settings_panels/*.html + index.html (labels), '
      + 'frontend/src/i18n/locales/*.json (text).',
    contract: 'tofu.ui-nav-targets/v1',
    linkSyntax: {
      setting: '[按钮文字](setting:<key>) — 打开设置面板并定位到对应的开关/输入框/页签',
      page: '[按钮文字](page:<key>) — 打开对应的子页面',
    },
    targets: buildTargets(),
  };
}

function main() {
  const mode = process.argv[2] ?? '--write';
  if (!['--write', '--check'].includes(mode)) {
    console.error('Usage: node scripts/gen_ui_nav_targets.mjs [--write|--check]');
    process.exitCode = 2;
    return;
  }
  const doc = buildDocument();
  const next = JSON.stringify(doc, null, 2) + '\n';
  const outputPath = path.join(repoRoot, OUTPUT);
  const current = fs.existsSync(outputPath) ? fs.readFileSync(outputPath, 'utf8') : '';
  if (mode === '--check') {
    if (current !== next) {
      console.error(`${OUTPUT} is stale — run npm run generate:ui-nav`);
      process.exitCode = 1;
    } else {
      console.log(`ui-nav targets contract is current (${doc.targets.length} targets)`);
    }
  } else if (current !== next) {
    fs.writeFileSync(outputPath, next);
    console.log(`${OUTPUT} written (${doc.targets.length} targets)`);
  } else {
    console.log(`${OUTPUT} unchanged (${doc.targets.length} targets)`);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
