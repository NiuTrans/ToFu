/* Structural label ownership regressions for the build-time generator.
 * No browser UI is changed: inert DOM fragments model the shipped panel shapes.
 * The Python contract suite runs these fixtures as part of the navigation gate.
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { settingLabel } from '../../scripts/gen_ui_nav_targets.mjs';

const catalogs = {
  zh: {
    depth: '默认思维深度', send: '发送方式', enable: '启用服务',
    enableDesc: '开关说明', provider: '服务商', option: '选项不是标签',
    prompt: '系统提示词', promptHint: '提示词说明', pricing: '价格覆盖',
    pricingDesc: '定价说明', note: '可选', app: 'App ID',
    rich: '默认模型 <span>（LLM_MODEL）</span>',
    help: '字段说明', richHelp: '说明 &amp; 详情<br>第二行',
  },
  en: {
    depth: 'Default Thinking Depth', send: 'Send Behavior', enable: 'Enable Service',
    enableDesc: 'Toggle description', provider: 'Provider', option: 'Not a label',
    prompt: 'System Prompt', promptHint: 'Prompt description', pricing: 'Pricing Override',
    pricingDesc: 'Pricing description', note: 'Optional', app: 'App ID',
    rich: 'Default Model <span>(LLM_MODEL)</span>',
    help: 'Field description', richHelp: 'Help &amp; details<br>Second line',
  },
};
const text = (key) => ({ zh: catalogs.zh[key], en: catalogs.en[key] });
const label = (html, id = 'control', translations = catalogs) => settingLabel(
  JSDOM.fragment(`<div class="settings-tab-panel">${html}</div>`), id, translations,
);
const toggle = `<div class="settings-toggle-row">
  <div><span class="settings-toggle-label" data-i18n="enable"></span>
    <div class="settings-toggle-desc" data-i18n="enableDesc"></div></div>
  <label class="stg-toggle"><input id="enabled" type="checkbox"><span></span></label>
</div>`;
const sendField = `<div class="settings-field">
  <label data-i18n="send"></label><select id="send"></select>
</div>`;

test('explicit label[for] beats preceding closed rows and select options regardless of distance', () => {
  const result = label(`${sendField}<div class="settings-field extra">
    <select id="control"><option data-i18n="option"></option></select>
    <!-- ${'long intervening markup '.repeat(100)} -->
    <label for="control" data-i18n="depth"></label>
    <span class="settings-field-hint" data-i18n="help"></span>
  </div>`);
  assert.deepEqual(result, { name: text('depth'), description: text('help') });
});

test('row ownership survives class order, wrappers, sibling reordering, and long selects', () => {
  const field = `<section class="extra settings-field">
    <div><label data-i18n="depth"></label></div>
    <select id="control">${'<option data-i18n="option"></option>'.repeat(35)}</select>
    <span class="settings-field-hint" data-i18n="help"></span>
  </section>`;
  for (const html of [sendField + field + toggle, toggle + field + sendField]) {
    assert.deepEqual(label(html), { name: text('depth'), description: text('help') });
  }
});

test('rich localized labels never become the first select option', () => {
  assert.deepEqual(label(`<div class="settings-field">
    <label data-i18n-html="rich">stale fallback</label>
    <select id="control"><option data-i18n="option"></option></select>
    <div class="settings-field-hint" data-i18n-html="richHelp"></div>
  </div>`), {
    name: { zh: '默认模型 （LLM_MODEL）', en: 'Default Model (LLM_MODEL)' },
    description: { zh: '说明 & 详情 第二行', en: 'Help & details Second line' },
  });
});

test('literal and composite labels stay inside their own field', () => {
  const fields = `<div class="settings-field">
    <label>API Key</label><input id="key" placeholder="not a label">
  </div><div class="settings-field">
    <label><span data-i18n="app"></span> <span data-i18n="note"></span></label>
    <input id="app" data-i18n-placeholder="option">
  </div>`;
  assert.deepEqual(label(toggle + fields, 'key'), { name: { zh: 'API Key', en: 'API Key' } });
  assert.deepEqual(label(toggle + fields, 'app'), {
    name: { zh: 'App ID 可选', en: 'App ID Optional' },
  });
});

test('a provider inside a heading cannot borrow the preceding checkbox or its description', () => {
  assert.deepEqual(label(`${toggle}<div style="display:none">
    <div class="settings-divider"></div>
    <div class="settings-section-title"><span data-i18n="provider"></span>
      <select id="control"><option data-i18n="option"></option></select>
    </div>
  </div>`), { name: text('provider') });
  assert.deepEqual(label(toggle, 'enabled'), {
    name: text('enable'), description: text('enableDesc'),
  });
});

test('a hidden section control follows its heading, not a previous closed field', () => {
  assert.deepEqual(label(`${sendField}<div class="settings-divider"></div>
    <div class="settings-section-title" data-i18n="prompt"></div>
    <textarea id="control" style="display:none"></textarea>
    <input type="hidden"><select style="display:none"><option>legacy</option></select>
    <div><button data-i18n="option"></button></div>
    <div class="settings-toggle-desc" data-i18n="promptHint"></div>
    <div class="settings-divider"></div>${toggle}`), {
    name: text('prompt'), description: text('promptHint'),
  });
});

test('pricing follows its disclosure summary, not a field or a sibling disclosure', () => {
  assert.deepEqual(label(`${sendField}
    <details><summary data-i18n="enable"></summary><input id="unrelated"></details>
    <details><summary><strong data-i18n="pricing"></strong></summary>
      <div class="settings-disclosure-body">
        <p class="settings-desc" data-i18n="pricingDesc"></p>
        <textarea id="control"></textarea>
      </div>
    </details>
    <details><summary data-i18n="send"></summary></details>`), {
    name: text('pricing'), description: text('pricingDesc'),
  });
});

test('heading-delimited subsections do not absorb the next subsection description', () => {
  assert.deepEqual(label(`<details><summary data-i18n="enable"></summary><div>
    <div class="search-pipeline-title" data-i18n="provider"></div>
    <p class="settings-desc" data-i18n="help"></p><div id="control"></div>
    <div class="search-pipeline-title" data-i18n="send"></div>
    <p class="settings-desc" data-i18n="enableDesc"></p><div id="other"></div>
  </div></details>`), { name: text('provider'), description: text('help') });
});

test('aria associations take precedence and explicitly own out-of-row descriptions', () => {
  assert.deepEqual(label(`<span id="name" data-i18n="depth"></span>
    <span id="note" data-i18n="note"></span><p id="help" data-i18n="help"></p>
    <div class="settings-field"><label for="control" data-i18n="send"></label>
      <input id="control" aria-labelledby="name note" aria-describedby="help">
      <span class="settings-field-hint" data-i18n="enableDesc"></span>
    </div>`), {
    name: { zh: '默认思维深度 可选', en: 'Default Thinking Depth Optional' },
    description: text('help'),
  });
});

test('nested rows and neighboring hints are not descriptions of the parent control', () => {
  assert.deepEqual(label(`<div class="settings-field"><label data-i18n="depth"></label>
    <input id="control"><div class="settings-field"><label data-i18n="send"></label>
      <input id="other"><span class="settings-field-hint" data-i18n="help"></span>
    </div></div><div class="settings-field">
      <label data-i18n="enable"></label><input id="next">
      <span class="settings-field-hint" data-i18n="enableDesc"></span>
    </div>`), { name: text('depth') });
});

test('missing, duplicate, ambiguous, and untranslated ownership fails closed', () => {
  const invalid = [
    ['', /#control/],
    ['<input id="control"><input id="control">', /found 2/],
    [`${toggle}<input id="control">`, /no owning label/],
    [`${sendField}<div class="settings-field"><input id="control"></div>`, /found 0/],
    ['<div class="settings-field"><label>A</label><label>B</label><input id="control"></div>', /found 2/],
    ['<div class="settings-field"><label for="other">Wrong</label><input id="control"></div>', /found 0/],
    ['<div class="settings-field"><label data-i18n="absent"></label><input id="control"></div>', /Missing zh/],
    ['<input id="control" aria-labelledby="missing">', /#missing/],
    ['<div class="settings-section-title" data-i18n="prompt"></div><input><input id="control">', /no owning label/],
    ['<div class="settings-section-title" data-i18n="prompt"></div><details><div><input id="control"></div></details>', /no owning label/],
    ['<div class="settings-field"><label>A</label><input id="control"><p class="settings-desc">B</p><p class="settings-desc">C</p></div>', /description/],
  ];
  for (const [html, error] of invalid) assert.throws(() => label(html), error, html);
  assert.throws(() => label('<div class="settings-field"><label data-i18n="depth"></label><input id="control"></div>',
    'control', { zh: catalogs.zh, en: {} }), /Missing en/);
});

test('section help never crosses a direct or wrapped visible control', () => {
  for (const next of [
    '<input id="other" aria-describedby="otherHelp">',
    '<div><input id="other" aria-describedby="otherHelp"></div>',
  ]) {
    assert.deepEqual(label(`<div class="settings-section-title" data-i18n="prompt"></div>
      <textarea id="control"></textarea>${next}
      <p class="settings-desc" id="otherHelp">Other-only help</p>`), {
      name: text('prompt'),
    });
  }
});

test('implicit section help cannot borrow an explicitly owned hidden companion hint', () => {
  assert.deepEqual(label(`<div class="settings-section-title" data-i18n="prompt"></div>
    <textarea id="control"></textarea><input type="hidden" aria-describedby="otherHelp">
    <p class="settings-desc" id="otherHelp">Other-only help</p>`), { name: text('prompt') });
});

test('rich locale text excludes action, option, control, and hidden subtrees', () => {
  const translations = Object.fromEntries(['zh', 'en'].map((locale) => [locale, {
    rich: 'Default Model <button>Reset</button> <span aria-hidden="true">DEBUG</span>'
      + '<select><option>Option</option></select><textarea>Input</textarea>'
      + '<svg><text>Icon</text></svg><span> (LLM_MODEL)</span>',
  }]));
  assert.deepEqual(label(`<div class="settings-field"><label data-i18n-html="rich"></label>
    <input id="control"></div>`, 'control', translations), {
    name: { zh: 'Default Model (LLM_MODEL)', en: 'Default Model (LLM_MODEL)' },
  });
});
