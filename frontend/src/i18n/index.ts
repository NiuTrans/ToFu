import type {
  I18nArgs,
  I18nKey,
  Translator,
} from './contract.generated';
import enCatalogUrl from './generated/en.generated.json?url';
import zhCatalogUrl from './generated/zh.generated.json?url';

export type {
  I18nArgs,
  I18nKey,
  I18nParamsFor,
  Translator,
} from './contract.generated';

export type UiLanguage = 'zh' | 'en';

type Messages = Readonly<Record<string, string>>;
type TranslationParams = Readonly<Record<string, unknown>>;

const localeUrls: Readonly<Record<UiLanguage, string>> = Object.freeze({
  zh: zhCatalogUrl,
  en: enCatalogUrl,
});

const loaded = new Map<UiLanguage, Messages>();
// At most two entries can exist. Coalescing preserves the module-loader
// behavior that concurrent boot/switch requests share one network read.
const loading = new Map<UiLanguage, Promise<Messages>>();

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

function preferredLanguage(): UiLanguage {
  try {
    const stored = localStorage.getItem('tofu_ui_lang');
    if (stored === 'zh' || stored === 'en') return stored;
  } catch {
    // Continue with the inert server hint when storage is unavailable.
  }
  try {
    const element = document.getElementById('tofu-boot-config');
    const hint = element?.textContent
      ? (JSON.parse(element.textContent) as { uiLanguageHint?: unknown }).uiLanguageHint
      : undefined;
    return hint === 'en' ? 'en' : 'zh';
  } catch {
    return 'zh';
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
  const request = fetch(localeUrls[language], {
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

/** Load the preferred locale data before the application announces readiness. */
export async function ready(): Promise<void> {
  await loadLanguage(_i18nLang);
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

function translateMessage(key: string, params?: TranslationParams): string {
  const primary = loaded.get(_i18nLang);
  const fallback = loaded.get('zh');
  let value = primary?.[key] ?? fallback?.[key];
  if (value === undefined) {
    const fingerprint = `${_i18nLang}:${key}`;
    if (!missing.has(fingerprint)) {
      missing.add(fingerprint);
      console.warn(`[i18n] missing ${fingerprint}`);
    }
    value = key;
  }
  if (!params) return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name: string) => (
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name] ?? '') : token
  ));
}

/**
 * The only application-facing translation interface. Its key and placeholder
 * vocabulary is generated from both locale authorities; untyped DOM/runtime
 * adapters stay inside this module and call translateMessage directly.
 */
export const t: Translator = <K extends I18nKey>(
  key: K,
  ...args: I18nArgs<K>
): string => translateMessage(key, args[0] as TranslationParams | undefined);

export async function setLanguage(language: string): Promise<void> {
  if (language !== 'zh' && language !== 'en') return;
  await loadLanguage(language);
  _i18nLang = language;
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
  document.documentElement.lang = _i18nLang === 'zh' ? 'zh-CN' : 'en';
  const select = document.getElementById('settingLanguage') as HTMLSelectElement | null;
  if (select) select.value = _i18nLang;
  _syncLangPicker(_i18nLang);
}

export function _syncLangPicker(language: UiLanguage = _i18nLang): void {
  document.querySelectorAll<HTMLElement>('.lang-option').forEach((element) => {
    element.classList.toggle('active', element.dataset.lang === language);
  });
}

export function _onLanguageChange(language: UiLanguage): void {
  void setLanguage(language);
}
