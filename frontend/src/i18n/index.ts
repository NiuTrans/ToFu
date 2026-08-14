export type UiLanguage = 'zh' | 'en';

type Messages = Readonly<Record<string, string>>;
type TranslationParams = Readonly<Record<string, unknown>>;

const localeLoaders: Record<UiLanguage, () => Promise<{ default: Messages }>> = {
  zh: () => import('./locales/zh.json'),
  en: () => import('./locales/en.json'),
};

const loaded = new Map<UiLanguage, Messages>();
const missing = new Set<string>();
// Tiny runtime-only labels whose state is delivered out-of-band from tool
// content. Keeping them here avoids making the generated locale chunks part
// of the model/tool protocol surface.
const internalMessages: Record<UiLanguage, Messages> = {
  zh: { 'toolCmd.grepSearchIntercepted': 'grep_search 接管' },
  en: { 'toolCmd.grepSearchIntercepted': 'grep_search takeover' },
};

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
  const messages = (await localeLoaders[language]()).default;
  loaded.set(language, messages);
  return messages;
}

/** Load the preferred locale chunk before the application announces readiness. */
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

export function t(key: string, params?: TranslationParams): string {
  const primary = loaded.get(_i18nLang);
  const fallback = loaded.get('zh');
  let value = primary?.[key] ?? fallback?.[key]
    ?? internalMessages[_i18nLang]?.[key] ?? internalMessages.zh[key];
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
    if (key) element.textContent = t(key);
  });
  root.querySelectorAll<HTMLElement>('[data-i18n-html]').forEach((element) => {
    const key = element.dataset.i18nHtml;
    if (key) element.innerHTML = t(key);
  });
  root.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>('[data-i18n-placeholder]')
    .forEach((element) => {
      const key = element.dataset.i18nPlaceholder;
      if (key) element.placeholder = t(key);
    });
  root.querySelectorAll<HTMLElement>('[data-i18n-title]').forEach((element) => {
    const key = element.dataset.i18nTitle;
    if (key) element.title = t(key);
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
