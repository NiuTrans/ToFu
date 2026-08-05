#!/usr/bin/env python3
"""jsdom test for static/js/settings/credentials_vault.js (「凭证保管库」).

WHY
---
The credential vault has two hard privacy rules the frontend must encode:

  * LIST responses carry metadata ONLY ({name, hint, note, timestamps}) — a
    secret value must NEVER be rendered from the list payload; and
  * the ONLY plaintext egress is POST …/<name>/reveal, fired explicitly by
    the「查看」button — and whatever is revealed must auto-hide again so a
    shoulder-surfed screen doesn't keep the token on display.

This harness drives the REAL shipped credentials_vault.js under jsdom and
asserts:
  * entries render from a stubbed Api.credentials.list (name + hint + note,
    NEVER the value);
  * the add form posts exactly {name, value, note} and blocks on a blank
    value before any request leaves;
  * 查看 calls reveal, shows the value inline, schedules a ~30s auto-hide,
    and firing that timer hides it again;
  * 复制 pushes the revealed value to the clipboard;
  * 删除 asks for confirmation first and only calls remove on accept.

Run: make test-frontend  (skips cleanly when node/jsdom aren't installed)
"""

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);

// Captured calls — the wire path under test.
const posted = [];
const revealCalls = [];
const removeCalls = [];
const confirmCalls = [];
const timers = [];        // captured setTimeout(fn, ms) — fired manually
const clipboard = [];     // captured clipboard writes
let confirmAnswer = true;

const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="credentialsVaultList"></div></body>',
  // safe_html.js is the REAL template engine the module renders through;
  // eval'ing it (rather than stubbing safeHtml) keeps escaping honest.
  targets: [process.argv[4], process.argv[2]],
  globals: {
    confirm: (msg) => { confirmCalls.push(msg); return confirmAnswer; },
    // setup() neuters setTimeout to a no-op; override AFTER that so the
    // module's auto-hide schedule is capturable and manually fireable.
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    Api: {
      credentials: {
        list: async () => ({ ok: true, credentials: window.__CREDS }),
        upsert: async (body) => { posted.push(body); return { ok: true, credential: { name: body.name } }; },
        reveal: async (name) => { revealCalls.push(name); return { ok: true, name, value: 'ghp_secretvalue123' }; },
        remove: async (name) => { removeCalls.push(name); return { ok: true, name }; },
      },
    },
  },
});

// Clipboard: define on the jsdom navigator (read-only object) so the
// module's navigator.clipboard.writeText path is exercised for real.
Object.defineProperty(window.navigator, 'clipboard', {
  value: { writeText: async (v) => { clipboard.push(v); } },
  configurable: true,
});

const ROW = {
  name: 'github_pat',
  hint: 'ghp_…3V8',
  note: '主账号 token',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: new Date(Date.now() - 5 * 60000).toISOString(),
};

const $ = (id) => document.getElementById(id);
const html = () => $('credentialsVaultList').innerHTML;
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => process.nextTick(r)); };

(async () => {
  try {
    // ── 1. render from the stubbed list — metadata only, never the value ──
    window.__CREDS = [JSON.parse(JSON.stringify(ROW))];
    _renderCredentialsVault();
    await flush();

    check('row_rendered', html().indexOf('cred-vault-row') !== -1);
    check('name_shown', html().indexOf('github_pat') !== -1);
    check('hint_shown', html().indexOf('ghp_…3V8') !== -1);
    check('note_shown', html().indexOf('主账号 token') !== -1);
    // THE privacy rule: the value is not in the list payload and therefore
    // never in the DOM until 查看 is clicked.
    check('no_value_in_list_dom', html().indexOf('ghp_secretvalue123') === -1);
    check('relative_time_shown', html().indexOf('settings.credVaultMinutesAgo') !== -1);

    // ── 2. empty state ──
    window.__CREDS = [];
    _renderCredentialsVault();
    await flush();
    check('empty_state_shown', html().indexOf('settings.credVaultEmpty') !== -1);

    // ── 3. add form: blank value blocks the request entirely ──
    $('credVaultNameInput').value = 'pypi';
    $('credVaultValueInput').value = '';
    _credentialAdd();
    await flush();
    check('blank_value_blocks_post', posted.length === 0);
    check('blank_value_msg_err',
      $('credVaultMsg').textContent.indexOf('settings.credVaultNeedNameValue') !== -1 &&
      $('credVaultMsg').className.indexOf('err') !== -1);

    // ── 4. filled form posts exactly {name, value, note} ──
    $('credVaultNameInput').value = 'pypi';
    $('credVaultValueInput').value = 'pypi-token-xyz';
    $('credVaultNoteInput').value = 'release bot';
    _credentialAdd();
    await flush();
    check('add_posted_once', posted.length === 1);
    const body = posted[0] || {};
    check('add_body_exact',
      body.name === 'pypi' && body.value === 'pypi-token-xyz' && body.note === 'release bot');
    check('add_clears_inputs',
      $('credVaultNameInput').value === '' && $('credVaultValueInput').value === '');

    // ── 5. 查看 → reveal egress → inline value + ~30s auto-hide ──
    window.__CREDS = [JSON.parse(JSON.stringify(ROW))];
    _credentialReveal('github_pat');
    await flush();
    check('reveal_called', revealCalls.join(',') === 'github_pat');
    check('value_shown_after_reveal', html().indexOf('ghp_secretvalue123') !== -1);
    check('copy_button_shown', html().indexOf('_credentialCopy(') !== -1);
    check('autohide_scheduled_30s',
      timers.length === 1 && timers[0].ms === 30000 && typeof timers[0].fn === 'function');

    // ── 6. 复制 pushes the revealed value to the clipboard ──
    _credentialCopy('github_pat');
    await flush();
    check('copy_uses_clipboard', clipboard.join(',') === 'ghp_secretvalue123');

    // ── 7. auto-hide: firing the captured timer removes the value again ──
    timers[0].fn();
    await flush();
    check('autohide_hides_value', html().indexOf('ghp_secretvalue123') === -1);
    check('row_still_there_after_hide', html().indexOf('github_pat') !== -1);

    // ── 8. 删除 asks for confirmation; cancel = no request ──
    confirmAnswer = false;
    _credentialRemove('github_pat');
    await flush();
    check('delete_asks_confirmation',
      confirmCalls.length === 1 &&
      confirmCalls[0].indexOf('settings.credVaultConfirmDelete') !== -1);
    check('delete_cancel_no_call', removeCalls.length === 0);

    // ── 9. confirmed delete calls remove and re-renders ──
    confirmAnswer = true;
    window.__CREDS = [];
    _credentialRemove('github_pat');
    await flush();
    check('delete_confirmed_calls_remove', removeCalls.join(',') === 'github_pat');
    check('list_rerendered_after_delete', html().indexOf('settings.credVaultEmpty') !== -1);
  } catch (e) {
    check('harness_threw: ' + (e && e.message), false);
  } finally {
    report();
  }
})();
'''


def test_credentials_vault_frontend():
    run_harness(
        target_js=os.path.join(JS_DIR, 'settings', 'credentials_vault.js'),
        extra_targets=[os.path.join(JS_DIR, 'core', 'safe_html.js')],
        body_js=_BODY,
        expect_pass=23,
        label='credentials-vault',
    )
