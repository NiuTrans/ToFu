import type {
  I18nArgs,
  I18nKey,
  Translator,
} from './contract.generated';
import {
  DEFAULT_UI_LANGUAGE,
  UI_LANGUAGES,
  type UiLanguage,
  type UiLanguageDescriptor,
} from './languages.generated';

export type {
  I18nArgs,
  I18nKey,
  I18nParamsFor,
  Translator,
} from './contract.generated';
export type { UiLanguage, UiLanguageDescriptor } from './languages.generated';
export { UI_LANGUAGES } from './languages.generated';

type Messages = Readonly<Record<string, string>>;
type TranslationParams = Readonly<Record<string, unknown>>;

const descriptors: ReadonlyMap<string, UiLanguageDescriptor> = new Map(
  UI_LANGUAGES.map((descriptor) => [descriptor.code, descriptor]),
);

function isUiLanguage(value: unknown): value is UiLanguage {
  return typeof value === 'string' && descriptors.has(value);
}

// Only the active language's catalog is retained: every delivery catalog is
// complete (the generator fills pending translations from the fallback
// locale), so no second catalog is ever consulted.
const loaded = new Map<UiLanguage, Messages>();
// Coalescing preserves the module-loader behavior that concurrent boot/switch
// requests share one network read.
const loading = new Map<UiLanguage, Promise<Messages>>();
// The latest picker choice wins even when an earlier catalog arrives later.
let latestLanguageRequest = 0;

function decodeMessages(language: UiLanguage, value: unknown): Messages {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`Invalid ${language} locale catalog root`);
  }
  for (const [key, message] of Object.entries(value)) {
    if (!key || typeof message !== 'string') {
      throw new Error(`Invalid ${language} locale catalog entry: ${key}`);
    }
  }
  return value as Messages;
}
const missing = new Set<string>();
// Lookups before any catalog arrives (the retained runtime paints while the
// boot fetch is in flight) are premature, not missing; verify them on arrival.
const premature = new Set<string>();

function preferredLanguage(): UiLanguage {
  try {
    const stored = localStorage.getItem('tofu_ui_lang');
    if (isUiLanguage(stored)) return stored;
  } catch {
    // Continue with the inert server hint when storage is unavailable.
  }
  try {
    const element = document.getElementById('tofu-boot-config');
    const hint = element?.textContent
      ? (JSON.parse(element.textContent) as { uiLanguageHint?: unknown }).uiLanguageHint
      : undefined;
    return isUiLanguage(hint) ? hint : DEFAULT_UI_LANGUAGE;
  } catch {
    return DEFAULT_UI_LANGUAGE;
  }
}

export let _i18nLang: UiLanguage = preferredLanguage();

function syncLanguageCookie(language: UiLanguage): void {
  try {
    document.cookie = `tofu_ui_lang=${language};path=/;max-age=31536000;SameSite=Lax`;
  } catch {
    // The cookie is only a server-visible rendering hint. localStorage stays authoritative.
  }
}

async function loadLanguage(language: UiLanguage): Promise<Messages> {
  const current = loaded.get(language);
  if (current) return current;
  const pending = loading.get(language);
  if (pending) return pending;
  const request = fetch(descriptors.get(language)!.catalogUrl, {
    cache: 'force-cache',
    credentials: 'same-origin',
  }).then(async (response) => {
    if (!response.ok) {
      throw new Error(`Locale catalog request failed (${language}): ${response.status}`);
    }
    const messages = decodeMessages(language, await response.json());
    loaded.set(language, messages);
    return messages;
  });
  loading.set(language, request);
  try {
    return await request;
  } finally {
    if (loading.get(language) === request) loading.delete(language);
  }
}

function retainOnlyActiveCatalog(): void {
  for (const language of [...loaded.keys()]) {
    if (language !== _i18nLang) loaded.delete(language);
  }
}

/** Load the preferred locale data before the application announces readiness. */
export async function ready(): Promise<void> {
  await loadLanguage(_i18nLang);
  retainOnlyActiveCatalog();
  for (const key of premature) lookupMessage(key);
  premature.clear();
  syncLanguageCookie(_i18nLang);
  _applyI18n();
  // The initial locale application is observable for the same reason a
  // user-initiated switch is: JS-driven labels rendered before this chunk
  // landed (the pet's scene button mounts at DOMContentLoaded and wins that
  // race) can only re-render once a language is actually active.
  window.dispatchEvent(new CustomEvent('tofu:language-change', {
    detail: { language: _i18nLang },
  }));
}

function lookupMessage(key: string): string | undefined {
  const messages = loaded.get(_i18nLang);
  if (!messages) {
    premature.add(key);
    return undefined;
  }
  const value = messages[key];
  if (value === undefined) {
    const fingerprint = `${_i18nLang}:${key}`;
    if (!missing.has(fingerprint)) {
      missing.add(fingerprint);
      console.warn(`[i18n] missing ${fingerprint}`);
    }
  }
  return value;
}

function translateMessage(key: string, params?: TranslationParams): string {
  const value = lookupMessage(key) ?? key;
  if (!params) return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name: string) => (
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name] ?? '') : token
  ));
}

/**
 * The only application-facing translation interface. Its key and placeholder
 * vocabulary is generated from the source locale authorities; untyped
 * DOM/runtime adapters stay inside this module and call translateMessage
 * directly.
 */
export const t: Translator = <K extends I18nKey>(
  key: K,
  ...args: I18nArgs<K>
): string => translateMessage(key, args[0] as TranslationParams | undefined);

export async function setLanguage(language: string): Promise<void> {
  if (!isUiLanguage(language)) return;
  const request = ++latestLanguageRequest;
  await loadLanguage(language);
  if (request !== latestLanguageRequest) return;
  _i18nLang = language;
  retainOnlyActiveCatalog();
  try {
    localStorage.setItem('tofu_ui_lang', language);
  } catch {
    // Storage can be unavailable in hardened/private browser profiles.
  }
  syncLanguageCookie(language);
  _applyI18n();
  window.dispatchEvent(new CustomEvent('tofu:language-change', {
    detail: { language },
  }));
}

export function _applyI18n(root: ParentNode = document): void {
  root.querySelectorAll<HTMLElement>('[data-i18n]').forEach((element) => {
    const key = element.dataset.i18n;
    if (key) element.textContent = translateMessage(key);
    if (element.hasAttribute('data-i18n-once')) {
      element.removeAttribute('data-i18n');
      element.removeAttribute('data-i18n-once');
    }
  });
  root.querySelectorAll<HTMLElement>('[data-i18n-html]').forEach((element) => {
    const key = element.dataset.i18nHtml;
    if (key) element.innerHTML = translateMessage(key);
  });
  root.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>('[data-i18n-placeholder]')
    .forEach((element) => {
      const key = element.dataset.i18nPlaceholder;
      if (key) element.placeholder = translateMessage(key);
    });
  root.querySelectorAll<HTMLElement>('[data-i18n-title]').forEach((element) => {
    const key = element.dataset.i18nTitle;
    if (key) element.title = translateMessage(key);
  });
  root.querySelectorAll<HTMLElement>('[data-i18n-aria-label]').forEach((element) => {
    const key = element.dataset.i18nAriaLabel;
    if (key) element.setAttribute('aria-label', translateMessage(key));
  });
  document.documentElement.lang = descriptors.get(_i18nLang)!.tag;
  const select = document.getElementById('settingLanguage') as HTMLSelectElement | null;
  if (select) renderLanguageSelect(select);
}

// Options are rebuilt only when the active language changes, so an unrelated
// _applyI18n() pass never replaces the options of an open dropdown.
const renderedLanguageSelects = new WeakMap<HTMLSelectElement, UiLanguage>();

/**
 * Fill the Settings language select from the manifest. Each option leads with
 * the language's own name, so anyone can find theirs whatever the current UI
 * language; the other languages also carry their name in the current UI
 * language ("Deutsch — 德语"), mirroring the "Max — 最大" options beside it.
 */
function renderLanguageSelect(select: HTMLSelectElement): void {
  if (renderedLanguageSelects.get(select) !== _i18nLang) {
    const active = descriptors.get(_i18nLang)!;
    const names = new Intl.DisplayNames([active.tag], { type: 'language', fallback: 'none' });
    select.replaceChildren(...UI_LANGUAGES.map((language) => {
      const option = select.ownerDocument.createElement('option');
      option.value = language.code;
      const localized = language.code === active.code ? undefined : names.of(language.code);
      option.textContent = localized && localized !== language.name
        ? `${language.name} — ${capitalizeLabel(localized, active.tag)}`
        : language.name;
      return option;
    }));
    renderedLanguageSelects.set(select, _i18nLang);
  }
  select.value = _i18nLang;
}

/** Menu labels start uppercase where the language capitalizes them ("Japonais"). */
function capitalizeLabel(label: string, tag: string): string {
  const [first = '', ...rest] = label;
  return first.toLocaleUpperCase(tag) + rest.join('');
}

/** Declarative picker receiver; languages outside languages.json are ignored. */
export function _onLanguageChange(language: unknown): void {
  if (isUiLanguage(language)) void setLanguage(language);
}
