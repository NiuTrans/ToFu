// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

// The retained runtime is a static import of main.ts, so its first paint
// translates before main.ts calls ready(). Those lookups are premature, not
// missing keys; a key is missing only when a loaded catalog lacks it.
const catalog = { 'mobile.medium': '中', 'mobile.default': '默认' };
let release: () => void = () => {};

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
  document.getElementById('tofu-boot-config')?.remove();
  document.documentElement.lang = '';
  const arrived = new Promise<void>((resolve) => { release = resolve; });
  vi.stubGlobal('fetch', vi.fn(async () => {
    await arrived;
    return { ok: true, status: 200, json: async () => catalog };
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const lookup = (translate: (key: never) => string, key: string) => translate(key as never);

function stubCatalogs(messages: Record<string, Record<string, string>>, gates: Record<string, Promise<void>> = {}) {
  const fetchMock = vi.fn(async (url: string) => {
    const language = Object.keys(messages).find((code) => url.includes(`/${code}.generated`));
    if (!language) throw new Error(`unexpected catalog URL ${url}`);
    await gates[language];
    return { ok: true, status: 200, json: async () => messages[language] };
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

it('verifies boot-time lookups against the catalog instead of reporting them missing', async () => {
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
  const i18n = await import('./index');

  expect(lookup(i18n.t, 'mobile.medium')).toBe('mobile.medium');
  expect(lookup(i18n.t, 'sidebar.notInCatalog')).toBe('sidebar.notInCatalog');
  expect(warn).not.toHaveBeenCalled();

  const ready = i18n.ready();
  release();
  await ready;

  expect(lookup(i18n.t, 'mobile.medium')).toBe('中');
  expect(warn.mock.calls).toEqual([['[i18n] missing zh:sidebar.notInCatalog']]);
});

it('reports a key absent from the loaded catalog once', async () => {
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
  const i18n = await import('./index');
  const ready = i18n.ready();
  release();
  await ready;

  expect(lookup(i18n.t, 'mobile.default')).toBe('默认');
  expect(lookup(i18n.t, 'toolbar.notInCatalog')).toBe('toolbar.notInCatalog');
  expect(lookup(i18n.t, 'toolbar.notInCatalog')).toBe('toolbar.notInCatalog');
  expect(warn.mock.calls).toEqual([['[i18n] missing zh:toolbar.notInCatalog']]);
});

it('boots a stored manifest language with its catalog and BCP 47 tag', async () => {
  localStorage.setItem('tofu_ui_lang', 'ja');
  const fetchMock = stubCatalogs({ ja: { 'mobile.medium': '中' } });
  const i18n = await import('./index');
  await i18n.ready();

  expect(i18n._i18nLang).toBe('ja');
  expect(String(fetchMock.mock.calls[0][0])).toContain('/ja.generated');
  expect(document.documentElement.lang).toBe('ja');
  expect(document.cookie).toContain('tofu_ui_lang=ja');
});

it('ignores an unknown stored language and follows the server hint, then the default', async () => {
  localStorage.setItem('tofu_ui_lang', 'xx');
  const hint = document.createElement('script');
  hint.id = 'tofu-boot-config';
  hint.type = 'application/json';
  hint.textContent = JSON.stringify({ uiLanguageHint: 'de' });
  document.body.appendChild(hint);
  let i18n = await import('./index');
  expect(i18n._i18nLang).toBe('de');

  hint.textContent = JSON.stringify({ uiLanguageHint: '../../etc' });
  vi.resetModules();
  i18n = await import('./index');
  expect(i18n._i18nLang).toBe('zh');
});

it('applies the latest picker choice when an earlier catalog arrives later', async () => {
  let releaseFrench: () => void = () => {};
  const french = new Promise<void>((resolve) => { releaseFrench = resolve; });
  stubCatalogs({ zh: {}, fr: { 'mobile.medium': 'Moyen' }, ru: { 'mobile.medium': 'Средний' } }, { fr: french });
  const i18n = await import('./index');
  await i18n.ready();

  const slow = i18n.setLanguage('fr');
  await i18n.setLanguage('ru');
  releaseFrench();
  await slow;

  expect(i18n._i18nLang).toBe('ru');
  expect(lookup(i18n.t, 'mobile.medium')).toBe('Средний');
  expect(document.documentElement.lang).toBe('ru');
  expect(localStorage.getItem('tofu_ui_lang')).toBe('ru');
});

it('retains only the active catalog and refetches a language after switching away', async () => {
  const fetchMock = stubCatalogs({ zh: { 'mobile.medium': '中' }, ko: { 'mobile.medium': '중간' } });
  const i18n = await import('./index');
  await i18n.ready();
  await i18n.setLanguage('ko');
  expect(lookup(i18n.t, 'mobile.medium')).toBe('중간');
  await i18n.setLanguage('zh');

  expect(lookup(i18n.t, 'mobile.medium')).toBe('中');
  expect(fetchMock.mock.calls.map(([url]) => String(url).match(/\/(\w+)\.generated/)?.[1]))
    .toEqual(['zh', 'ko', 'zh']);
});

it('rejects languages outside the manifest without changing the active one', async () => {
  const fetchMock = stubCatalogs({ zh: {} });
  const i18n = await import('./index');
  await i18n.ready();
  await i18n.setLanguage('pt');
  i18n._onLanguageChange('../en');
  i18n._onLanguageChange(42);
  await Promise.resolve();

  expect(i18n._i18nLang).toBe('zh');
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it('switches to every manifest language through the declarative picker receiver', async () => {
  const codes = ['zh', 'en', 'ja', 'ko', 'fr', 'de', 'ru'];
  stubCatalogs(Object.fromEntries(codes.map((code) => [code, { 'mobile.medium': code }])));
  const i18n = await import('./index');
  await i18n.ready();
  expect(i18n.UI_LANGUAGES.map((language) => language.code)).toEqual(codes);

  for (const language of i18n.UI_LANGUAGES) {
    i18n._onLanguageChange(language.code);
    await vi.waitFor(() => expect(i18n._i18nLang).toBe(language.code));
    expect(document.documentElement.lang).toBe(language.tag);
    expect(lookup(i18n.t, 'mobile.medium')).toBe(language.code);
  }
});

function mountLanguageSelect(): HTMLSelectElement {
  document.getElementById('settingLanguage')?.remove();
  const select = document.createElement('select');
  select.id = 'settingLanguage';
  document.body.appendChild(select);
  return select;
}

const optionTexts = (select: HTMLSelectElement) => [...select.options].map((option) => option.text);

it('renders the Settings language select from the manifest in the active language', async () => {
  const codes = ['zh', 'en', 'ja', 'ko', 'fr', 'de', 'ru'];
  stubCatalogs(Object.fromEntries(codes.map((code) => [code, {}])));
  const select = mountLanguageSelect();
  const i18n = await import('./index');
  await i18n.ready();

  expect([...select.options].map((option) => option.value)).toEqual(codes);
  expect(select.value).toBe('zh');
  // Every option leads with the endonym; the others add their Chinese name.
  expect(optionTexts(select)).toEqual([
    '中文', 'English — 英语', '日本語 — 日语', '한국어 — 韩语',
    'Français — 法语', 'Deutsch — 德语', 'Русский — 俄语',
  ]);

  await i18n.setLanguage('fr');
  expect(select.value).toBe('fr');
  expect(optionTexts(select)).toEqual([
    '中文 — Chinois', 'English — Anglais', '日本語 — Japonais', '한국어 — Coréen',
    'Français', 'Deutsch — Allemand', 'Русский — Russe',
  ]);
});

it('keeps the language options of an unchanged language in place', async () => {
  stubCatalogs({ zh: {} });
  const select = mountLanguageSelect();
  const i18n = await import('./index');
  await i18n.ready();
  const before = [...select.options];

  i18n._applyI18n();

  expect([...select.options]).toEqual(before);
  expect([...select.options].every((option, index) => option === before[index])).toBe(true);
});
