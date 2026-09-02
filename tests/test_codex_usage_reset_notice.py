"""Browser contracts for the Codex earned-reset prompt and Settings card."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path, runtime_section_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
NOTICE_OWNER = ROOT / "frontend/src/features/subscription-reset-notice.ts"


_NODE_HARNESS = r"""
(async () => {
  const fs = require('fs');
  eval(fs.readFileSync(process.argv[1], 'utf8'));

  const wait = () => new Promise((resolve) => setImmediate(resolve));
  const checks = [];
  const check = (name, value) => {
    checks.push([name, !!value]);
    if (!value) console.error('FAIL', name);
  };
  class MemoryStorage {
    constructor() { this.values = new Map(); }
    getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
    setItem(key, value) { this.values.set(key, String(value)); }
  }
  const response = (offer, authenticated = true) => ({
    codex: { authenticated, reset_offer: offer },
  });
  const available = (key, extra = {}) => ({
    state: 'available', available_count: 1, notification_key: key,
    captured_at: 1000, stale: false, refreshing: false, ...extra,
  });

  const storage = new MemoryStorage();
  let statuses = [response(available('a'.repeat(24)))];
  const notices = [];
  const timeouts = [];
  const intervals = [];
  let opened = 0;
  let selectedTab = '';
  const deps = {
    readStatus: async () => statuses.shift() ?? null,
    notify: (notice) => { notices.push(notice); return true; },
    translate: (key, values) => {
      if (key === 'settings.oauthResetNoticeTitle') return 'localized-title';
      if (key === 'settings.oauthResetNoticeDetailMany') return `many:${values.count}`;
      return key;
    },
    openSettings: () => { opened += 1; },
    switchSettingsTab: (tab) => { selectedTab = tab; },
    storage,
    now: () => 100000,
    setTimeout: (callback, delay) => { const row = {callback, delay}; timeouts.push(row); return row; },
    clearTimeout: () => {},
    setInterval: (callback, delay) => { const row = {callback, delay}; intervals.push(row); return row; },
    clearInterval: () => {},
    isVisible: () => true,
  };

  const controller = createSubscriptionResetNoticeController(deps);
  controller.start();
  await wait();
  check('fresh_available_prompts_once', notices.length === 1);
  check('prompt_uses_localized_keys', notices[0].title === 'localized-title');
  notices[0].onClick();
  check('prompt_click_opens_subscription_settings', opened === 1 && selectedTab === 'oauth');
  statuses = [response(available('a'.repeat(24)))];
  await controller.checkNow();
  check('same_controller_deduplicates', notices.length === 1);
  controller.destroy();

  statuses = [response(available('a'.repeat(24)))];
  const reloaded = createSubscriptionResetNoticeController(deps);
  reloaded.start();
  await wait();
  check('reload_deduplicates_from_storage', notices.length === 1);
  reloaded.destroy();

  statuses = [response(available('b'.repeat(24)))];
  const newCredit = createSubscriptionResetNoticeController(deps);
  newCredit.start();
  await wait();
  check('new_credit_key_prompts_again', notices.length === 2);
  newCredit.destroy();

  const staleStorage = new MemoryStorage();
  const staleNotices = [];
  const staleTimeouts = [];
  const staleController = createSubscriptionResetNoticeController({
    ...deps,
    storage: staleStorage,
    readStatus: async () => response(available('c'.repeat(24), {
      stale: true, retry_after_seconds: 60,
    })),
    notify: (notice) => { staleNotices.push(notice); return true; },
    setTimeout: (callback, delay) => {
      const row = {callback, delay}; staleTimeouts.push(row); return row;
    },
  });
  staleController.start();
  await wait();
  check('stale_available_never_prompts', staleNotices.length === 0);
  check('stale_retry_is_bounded_and_delayed',
    staleTimeouts.length === 1 && staleTimeouts[0].delay === 60000);
  staleController.destroy();

  const refreshNotices = [];
  const refreshTimeouts = [];
  let refreshStatuses = [
    response({ state: 'unknown', available_count: null, captured_at: null,
      stale: false, refreshing: true }),
    response(available('d'.repeat(24))),
  ];
  const refreshing = createSubscriptionResetNoticeController({
    ...deps,
    storage: new MemoryStorage(),
    readStatus: async () => refreshStatuses.shift() ?? null,
    notify: (notice) => { refreshNotices.push(notice); return true; },
    setTimeout: (callback, delay) => {
      const row = {callback, delay}; refreshTimeouts.push(row); return row;
    },
  });
  refreshing.start();
  await wait();
  check('refreshing_unknown_does_not_prompt', refreshNotices.length === 0);
  check('refreshing_unknown_schedules_short_retry',
    refreshTimeouts.length === 1 && refreshTimeouts[0].delay === 2500);
  refreshTimeouts[0].callback();
  await wait();
  check('fresh_retry_result_prompts', refreshNotices.length === 1);
  refreshing.destroy();

  const malformed = normalizeCodexResetOffer({
    state: 'available', available_count: 1, notification_key: '',
    captured_at: 1000, stale: false, refreshing: false,
  });
  check('available_requires_stable_notification_key', malformed === null);
  check('unauthenticated_status_is_ignored',
    extractAuthenticatedCodexResetOffer(response(available('e'.repeat(24)), false)) === null);

  const boundedStorage = new MemoryStorage();
  const boundedStatuses = [];
  for (let index = 0; index < 20; index += 1) {
    boundedStatuses.push(response(available(index.toString(16).padStart(24, '0'))));
  }
  const bounded = createSubscriptionResetNoticeController({
    ...deps,
    storage: boundedStorage,
    readStatus: async () => boundedStatuses.shift() ?? null,
    notify: () => true,
  });
  for (let index = 0; index < 20; index += 1) await bounded.checkNow();
  const seen = JSON.parse(boundedStorage.getItem(CODEX_RESET_NOTICE_STORAGE_KEY));
  check('seen_key_store_is_hard_bounded', seen.length === 16);
  bounded.destroy();

  const failed = checks.filter(([, ok]) => !ok);
  console.log(JSON.stringify({passed: checks.length - failed.length, failed}));
  if (failed.length) process.exitCode = 1;
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


def test_typed_notice_controller_contract():
    if not shutil.which("node"):
        pytest.skip("node is required")
    bundle = native_module_path(
        "codex-reset-notice.js", NOTICE_OWNER)
    result = subprocess.run(
        ["node", "-e", _NODE_HARNESS, bundle],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    summary = result.stdout.strip().splitlines()[-1]
    assert '"passed":14' in summary, summary
    assert '"failed":[]' in summary, summary


_SETTINGS_HARNESS = r"""
const fs = require('fs');
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!doctype html><body><div id="oauthCodexResetOffer"></div></body>',
  targets: [process.argv[2]],
  globals: {
    Api: { oauth: { status: async () => null } },
    BroadcastChannel: class BroadcastChannel {},
    escapeHtml: (value) => String(value)
      .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
    errorEnvelopeMessage: () => '',
    showAlert: () => {},
    t: (key, values) => {
      const copy = {
        'settings.oauthResetAvailableTitle': 'Earned usage reset',
        'settings.oauthResetChecking': 'Checking reset availability',
        'settings.oauthResetAvailableOne': 'One reset available',
        'settings.oauthResetAvailableMany': `${values && values.count} resets available`,
        'settings.oauthResetExpires': `Expires ${values && values.time}`,
        'settings.oauthResetStale': 'Stale result',
        'settings.oauthResetRedeemHint': 'Never auto-redeem',
      };
      return copy[key] || key;
    },
  },
});
global._i18nLang = window._i18nLang = 'en';
try {
  const el = document.getElementById('oauthCodexResetOffer');
  _renderOAuthResetOffer('codex', {
    state: 'available', available_count: 1, stale: false,
    expires_at: 1896134400,
  }, true);
  check('available_card_visible', el.style.display === '');
  check('available_card_copy', el.textContent.includes('One reset available'));
  check('available_card_says_no_auto_redeem', el.textContent.includes('Never auto-redeem'));
  check('available_card_state_class', el.classList.contains('is-available'));

  _renderOAuthResetOffer('codex', {
    state: 'available', available_count: 1, stale: true,
  }, true);
  check('stale_card_is_explicit',
    el.classList.contains('is-stale') && el.textContent.includes('Stale result'));

  _renderOAuthResetOffer('codex', {
    state: 'unknown', available_count: null, stale: false, refreshing: true,
  }, true);
  check('refreshing_card_is_honest',
    el.classList.contains('is-checking') && el.textContent.includes('Checking'));

  _renderOAuthResetOffer('codex', {
    state: 'none', available_count: 0, stale: false, refreshing: false,
  }, true);
  check('none_hides_card', el.style.display === 'none');

  _renderOAuthResetOffer('codex', {
    state: 'available', available_count: 1, stale: false,
  }, false);
  check('logout_hides_card', el.style.display === 'none');
} catch (error) {
  check('harness_threw:' + (error && error.stack || error), false);
} finally {
  report();
}
"""


_TOAST_ACCESSIBILITY_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!doctype html><body><div id="toastContainer" class="toast-container" aria-live="polite" aria-relevant="additions"></div></body>',
  targets: [process.argv[2]],
  globals: {
    escapeHtml: (value) => String(value),
    t: (key) => key,
  },
});
try {
  let acted = 0;
  showToast('', 'Reset ready', 'Open settings', 10000, {
    hint: 'Never auto-redeem',
    onClick: () => { acted += 1; },
  });
  const container = document.getElementById('toastContainer');
  const toast = container.querySelector('.toast');
  const action = toast && toast.querySelector('.toast-body');
  check('toast_container_is_polite_live_region',
    container.getAttribute('aria-live') === 'polite');
  check('actionable_toast_body_has_button_semantics',
    action && action.getAttribute('role') === 'button');
  check('actionable_toast_body_is_focusable', action && action.tabIndex === 0);
  action.dispatchEvent(new window.KeyboardEvent('keydown', {
    key: 'Enter', bubbles: true,
  }));
  check('enter_activates_custom_toast_action', acted === 1);
} catch (error) {
  check('harness_threw:' + (error && error.stack || error), false);
} finally {
  report();
}
"""


def test_reset_toast_is_keyboard_operable_and_announced():
    run_harness(
        target_js=runtime_section_path("core/toast.js"),
        body_js=_TOAST_ACCESSIBILITY_HARNESS,
        expect_pass=4,
        label="codex-reset-toast-accessibility",
    )


def test_settings_offer_renderer_is_explicit_about_reset_state():
    run_harness(
        target_js=runtime_section_path("settings/oauth.js"),
        body_js=_SETTINGS_HARNESS,
        expect_pass=8,
        label="codex-reset-settings",
    )
