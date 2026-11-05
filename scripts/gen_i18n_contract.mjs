#!/usr/bin/env node

/**
 * Generate the frontend i18n contracts from the language manifest and locales.
 *
 * `languages.json` declares the selectable languages, the source locales that
 * developers author with one shared key set, and the fallback locale. Source
 * locales are the only key and placeholder authority. Every other locale is a
 * derived translation (scripts/translate_i18n_locales.py). A derived entry is
 * usable when its key exists in the sources and its placeholders are safe to
 * render: only placeholders the sources define, every placeholder all sources
 * share, and none more often than the sources use it (some call sites replace
 * only the first occurrence). Delivery catalogs complete missing or unusable
 * derived entries from the fallback locale, so the browser never renders a raw
 * key and a build never breaks on a pending translation; `--check` requires
 * every derived locale to be complete and usable.
 *
 * Outputs: contract.generated.ts (key and placeholder types),
 * languages.generated.ts (language descriptors with catalog URLs), and one
 * compact delivery JSON per language. Vite serves the JSON as content-hashed
 * data, so thousands of messages never enter the JavaScript parse path.
 */

import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const i18nDirectory = path.join(repositoryRoot, 'frontend/src/i18n');
const manifestPath = path.join(i18nDirectory, 'languages.json');
const localeDirectory = path.join(i18nDirectory, 'locales');
const outputPath = path.join(i18nDirectory, 'contract.generated.ts');
const languagesOutputPath = path.join(i18nDirectory, 'languages.generated.ts');
const deliveryDirectory = path.join(i18nDirectory, 'generated');
const deliverySuffix = '.generated.json';
const languageCodePattern = /^[a-z]{2,3}$/;
const languageTagPattern = /^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/;
const keyPattern = /^[A-Za-z0-9_.-]+$/;
const placeholderPattern = /\{([A-Za-z0-9_]+)\}/g;
const translationCommand = 'python3 scripts/translate_i18n_locales.py --model <translator> --review-model <reviewer>';

function relativeToRepository(value) {
  return path.relative(repositoryRoot, value).split(path.sep).join('/');
}

function nonEmptyString(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

async function readLanguageManifest() {
  const source = await fs.readFile(manifestPath, 'utf8');
  const where = relativeToRepository(manifestPath);
  let manifest;
  try {
    manifest = JSON.parse(source);
  } catch (error) {
    throw new Error(`${where} is not valid JSON: ${error.message}`);
  }
  const languages = Array.isArray(manifest?.languages) ? manifest.languages : null;
  if (!languages || languages.length === 0) throw new Error(`${where} must list languages`);
  const codes = [];
  for (const language of languages) {
    if (!languageCodePattern.test(language?.code ?? '')) {
      throw new Error(`${where} has invalid language code ${JSON.stringify(language?.code)}`);
    }
    if (!languageTagPattern.test(language.tag ?? '')
        || !nonEmptyString(language.name) || !nonEmptyString(language.englishName)) {
      throw new Error(`${where} language ${language.code} needs tag, name and englishName`);
    }
    if (codes.includes(language.code)) throw new Error(`${where} repeats language ${language.code}`);
    codes.push(language.code);
  }
  const sources = Array.isArray(manifest.sources) ? manifest.sources : [];
  if (sources.length === 0 || sources.some((code) => !codes.includes(code))) {
    throw new Error(`${where} sources must be declared languages`);
  }
  if (!sources.includes(manifest.fallback)) throw new Error(`${where} fallback must be a source`);
  if (!codes.includes(manifest.default)) throw new Error(`${where} default must be a declared language`);
  return {
    source,
    defaultLanguage: manifest.default,
    fallback: manifest.fallback,
    sources,
    languages: languages.map(({ code, tag, name, englishName }) => ({ code, tag, name, englishName })),
  };
}

async function readLocale(language) {
  const filePath = path.join(localeDirectory, `${language}.json`);
  let source;
  try {
    source = await fs.readFile(filePath, 'utf8');
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
    throw new Error(`${relativeToRepository(filePath)} is missing; create it with: ${translationCommand}`);
  }
  let messages;
  try {
    messages = JSON.parse(source);
  } catch (error) {
    throw new Error(`${relativeToRepository(filePath)} is not valid JSON: ${error.message}`);
  }
  if (!messages || typeof messages !== 'object' || Array.isArray(messages)) {
    throw new Error(`${relativeToRepository(filePath)} must contain one object`);
  }
  for (const [key, value] of Object.entries(messages)) {
    if (!keyPattern.test(key)) {
      throw new Error(`${relativeToRepository(filePath)} has invalid key ${JSON.stringify(key)}`);
    }
    if (typeof value !== 'string') {
      throw new Error(`${relativeToRepository(filePath)}.${key} must be a string`);
    }
  }
  return { language, filePath, source, messages };
}

function placeholderCounts(value) {
  const counts = new Map();
  for (const match of value.matchAll(placeholderPattern)) {
    counts.set(match[1], (counts.get(match[1]) ?? 0) + 1);
  }
  return counts;
}

/** Placeholder rules every derived entry of `key` must satisfy. */
function placeholderRules(sourceLocales, key) {
  const maximum = new Map();
  let shared = null;
  for (const locale of sourceLocales) {
    const counts = placeholderCounts(locale.messages[key]);
    for (const [name, count] of counts) maximum.set(name, Math.max(maximum.get(name) ?? 0, count));
    const names = new Set(counts.keys());
    shared = shared === null ? names : new Set([...shared].filter((name) => names.has(name)));
  }
  return { maximum, required: shared };
}

function placeholderProblem(rules, value) {
  const counts = placeholderCounts(value);
  for (const [name, count] of counts) {
    if (!rules.maximum.has(name)) return `unknown placeholder {${name}}`;
    if (count > rules.maximum.get(name)) return `placeholder {${name}} repeated`;
  }
  for (const name of rules.required) {
    if (!counts.has(name)) return `missing placeholder {${name}}`;
  }
  return '';
}

export async function readI18nCatalog() {
  const manifest = await readLanguageManifest();
  const locales = await Promise.all(manifest.languages.map(({ code }) => readLocale(code)));
  const localeByCode = new Map(locales.map((locale) => [locale.language, locale]));
  const sourceLocales = manifest.sources.map((code) => localeByCode.get(code));
  const authorityOrder = Object.keys(sourceLocales[0].messages);
  const authorityKeys = [...authorityOrder].sort();
  const authoritySet = new Set(authorityKeys);
  for (const locale of sourceLocales.slice(1)) {
    const localeKeys = Object.keys(locale.messages);
    const localeSet = new Set(localeKeys);
    const missing = authorityKeys.filter((key) => !localeSet.has(key));
    const extra = localeKeys.filter((key) => !authoritySet.has(key)).sort();
    if (missing.length || extra.length) {
      throw new Error(
        `${relativeToRepository(locale.filePath)} key drift: `
        + `missing=${JSON.stringify(missing.slice(0, 20))} `
        + `extra=${JSON.stringify(extra.slice(0, 20))}`,
      );
    }
  }

  const paramsByKey = new Map();
  const rulesByKey = new Map();
  for (const key of authorityKeys) {
    const rules = placeholderRules(sourceLocales, key);
    rulesByKey.set(key, rules);
    paramsByKey.set(key, [...rules.maximum.keys()].sort());
  }

  const fallbackMessages = localeByCode.get(manifest.fallback).messages;
  const delivery = new Map();
  const derivedProblems = new Map();
  for (const locale of locales) {
    if (manifest.sources.includes(locale.language)) {
      delivery.set(locale.language, locale.messages);
      continue;
    }
    const messages = {};
    const missing = [];
    const invalid = [];
    for (const key of authorityOrder) {
      const value = locale.messages[key];
      if (value === undefined) {
        missing.push(key);
      } else if (placeholderProblem(rulesByKey.get(key), value)) {
        invalid.push(`${key} (${placeholderProblem(rulesByKey.get(key), value)})`);
      } else {
        messages[key] = value;
        continue;
      }
      messages[key] = fallbackMessages[key];
    }
    const extra = Object.keys(locale.messages).filter((key) => !authoritySet.has(key)).sort();
    delivery.set(locale.language, messages);
    if (missing.length || invalid.length || extra.length) {
      derivedProblems.set(locale.language, { missing, invalid, extra });
    }
  }

  const digest = crypto.createHash('sha256');
  digest.update('tofu-i18n-catalog-v2\0');
  digest.update(manifest.source);
  digest.update('\0');
  for (const locale of locales) {
    digest.update(`${locale.language}\0`);
    digest.update(locale.source);
    digest.update('\0');
  }
  return {
    digest: digest.digest('hex'),
    keys: authorityKeys,
    locales,
    manifest,
    paramsByKey,
    delivery,
    derivedProblems,
  };
}

function describeDerivedProblems(catalog) {
  return [...catalog.derivedProblems].map(([language, { missing, invalid, extra }]) => {
    const parts = [];
    if (missing.length) parts.push(`${missing.length} missing ${JSON.stringify(missing.slice(0, 5))}`);
    if (invalid.length) parts.push(`${invalid.length} unusable ${JSON.stringify(invalid.slice(0, 5))}`);
    if (extra.length) parts.push(`${extra.length} extra ${JSON.stringify(extra.slice(0, 5))}`);
    return `frontend/src/i18n/locales/${language}.json: ${parts.join(', ')}`;
  });
}

function renderContract(catalog) {
  const sources = catalog.manifest.sources.join(',');
  const lines = [
    '/**',
    ' * Generated by scripts/gen_i18n_contract.mjs.',
    ` * Source authority: frontend/src/i18n/languages.json and locales/{${sources}}.json.`,
    ' * Do not edit this file directly.',
    ' */',
    '',
    `export const I18N_CATALOG_DIGEST = '${catalog.digest}' as const;`,
    '',
    'export type I18nKey =',
    ...catalog.keys.map((key, index) => (
      `  | ${JSON.stringify(key)}${index === catalog.keys.length - 1 ? ';' : ''}`
    )),
    '',
    'export interface I18nParamsByKey {',
  ];
  for (const key of catalog.keys) {
    const names = catalog.paramsByKey.get(key);
    const value = names.length
      ? `Readonly<{ ${names.map((name) => `${JSON.stringify(name)}: unknown`).join('; ')} }>`
      : 'never';
    lines.push(`  ${JSON.stringify(key)}: ${value};`);
  }
  lines.push(
    '}',
    '',
    'export type I18nParamsFor<K extends I18nKey> = I18nParamsByKey[K];',
    '',
    'export type I18nArgs<K extends I18nKey> =',
    '  [I18nParamsFor<K>] extends [never]',
    '    ? readonly [params?: never]',
    '    : readonly [params: I18nParamsFor<K>];',
    '',
    'export type Translator = <K extends I18nKey>(',
    '  key: K,',
    '  ...args: I18nArgs<K>',
    ') => string;',
    '',
  );
  return `${lines.join('\n')}\n`;
}

function renderLanguages(catalog) {
  const { languages, defaultLanguage } = catalog.manifest;
  const lines = [
    '/**',
    ' * Generated by scripts/gen_i18n_contract.mjs.',
    ' * Source authority: frontend/src/i18n/languages.json.',
    ' * Do not edit this file directly.',
    ' */',
    '',
    ...languages.map(({ code }) => (
      `import ${code}CatalogUrl from './generated/${code}${deliverySuffix}?url';`
    )),
    '',
    `export type UiLanguage = ${languages.map(({ code }) => JSON.stringify(code)).join(' | ')};`,
    '',
    'export interface UiLanguageDescriptor {',
    '  readonly code: UiLanguage;',
    '  /** BCP 47 tag for `<html lang>` and Intl formatting. */',
    '  readonly tag: string;',
    '  /** Endonym shown in the language picker. */',
    '  readonly name: string;',
    '  /** Content-hashed URL of the compact delivery catalog. */',
    '  readonly catalogUrl: string;',
    '}',
    '',
    `export const DEFAULT_UI_LANGUAGE: UiLanguage = ${JSON.stringify(defaultLanguage)};`,
    '',
    'export const UI_LANGUAGES: readonly UiLanguageDescriptor[] = Object.freeze([',
    ...languages.map(({ code, tag, name }) => (
      `  Object.freeze({ code: ${JSON.stringify(code)}, tag: ${JSON.stringify(tag)}, `
      + `name: ${JSON.stringify(name)}, catalogUrl: ${code}CatalogUrl }),`
    )),
    ']);',
    '',
  ];
  return lines.join('\n');
}

export async function generateI18nContract({ check = false } = {}) {
  const catalog = await readI18nCatalog();
  const artifacts = [
    [outputPath, renderContract(catalog)],
    [languagesOutputPath, renderLanguages(catalog)],
    ...catalog.manifest.languages.map(({ code }) => [
      path.join(deliveryDirectory, `${code}${deliverySuffix}`),
      `${JSON.stringify(catalog.delivery.get(code))}\n`,
    ]),
  ];
  const stale = [];
  for (const [artifactPath, expected] of artifacts) {
    let actual = '';
    try {
      actual = await fs.readFile(artifactPath, 'utf8');
    } catch (error) {
      if (!error || error.code !== 'ENOENT') throw error;
    }
    if (actual !== expected) stale.push([artifactPath, expected]);
  }
  const declared = new Set(artifacts.map(([artifactPath]) => artifactPath));
  const strays = (await fs.readdir(deliveryDirectory).catch(() => []))
    .filter((name) => name.endsWith(deliverySuffix))
    .map((name) => path.join(deliveryDirectory, name))
    .filter((artifactPath) => !declared.has(artifactPath));
  const derivedProblems = describeDerivedProblems(catalog);
  if (check) {
    const problems = [];
    if (stale.length || strays.length) {
      problems.push(
        `${[...stale.map(([artifactPath]) => artifactPath), ...strays]
          .map(relativeToRepository).join(', ')} is stale; run npm run generate:i18n`,
      );
    }
    if (derivedProblems.length) {
      problems.push(...derivedProblems, `translate them with: ${translationCommand}`);
    }
    if (problems.length) throw new Error(problems.join('\n'));
  }
  for (const [artifactPath, expected] of stale) {
    await fs.mkdir(path.dirname(artifactPath), { recursive: true });
    await fs.writeFile(artifactPath, expected, 'utf8');
  }
  for (const artifactPath of strays) await fs.unlink(artifactPath);
  return {
    changed: stale.length > 0 || strays.length > 0,
    digest: catalog.digest,
    keyCount: catalog.keys.length,
    languageCount: catalog.manifest.languages.length,
    derivedProblems,
  };
}

async function main() {
  const mode = process.argv[2] || '--write';
  if (!['--write', '--check'].includes(mode)) {
    throw new Error('usage: gen_i18n_contract.mjs [--write|--check]');
  }
  const result = await generateI18nContract({ check: mode === '--check' });
  for (const problem of result.derivedProblems) {
    process.stderr.write(`warning: ${problem}; the fallback locale fills these entries\n`);
  }
  if (result.derivedProblems.length) {
    process.stderr.write(`warning: translate them with: ${translationCommand}\n`);
  }
  process.stdout.write(
    `i18n contract ${result.changed ? 'generated' : 'verified'} `
    + `(${result.keyCount} keys, ${result.languageCount} languages, `
    + `${result.digest.slice(0, 12)})\n`,
  );
}

const invokedPath = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : '';
if (invokedPath === import.meta.url) {
  try {
    await main();
  } catch (error) {
    process.stderr.write(`gen_i18n_contract: ${error.message}\n`);
    process.exitCode = 1;
  }
}
