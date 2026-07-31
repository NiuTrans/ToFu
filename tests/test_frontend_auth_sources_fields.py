#!/usr/bin/env python3
"""jsdom test for static/js/settings/auth_sources.js (login-walled sources UI).

WHY
---
The connect panel used to be ONE free-text textarea: the user had to hand-write
``web_session=...; a1=...`` themselves. That made the delimiter syntax the
user's problem, and a mistyped separator stored a garbage cookie set that the
card then reported as "已连接" — the failure only surfaced much later as an
unexplained empty fetch. The panel now renders one labelled input per cookie
DECLARED BY THE SERVER (``src.fields``, from lib/auth_sources.py
DEFAULT_SOURCES), and refuses to submit while a ``required`` one is blank.

Drives the REAL shipped auth_sources.js under jsdom and asserts the write path
the backend depends on:

  * one input per declared field, carrying data-cookie-name + data-importance,
    and NO free-text cookie textarea remains;
  * the field spec comes from the server row — the JS holds no per-site cookie
    table of its own (neuter: drop `fields` from the row → generic single field,
    NOT a hardcoded xiaohongshu list);
  * save posts a structured ``cookie_fields`` MAP, never a hand-assembled
    header string;
  * a blank required field blocks the request entirely (this is the bug: the
    old box happily submitted anything non-empty);
  * a user who pastes ``name=value`` (or a whole header) into one box gets it
    unwrapped rather than stored as a literal value;
  * the login-page button uses the server-supplied login_url.

Run: make test-frontend  (skips cleanly when node/jsdom aren't installed)
"""

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);

// Captured Api calls — the write path under test.
const posted = [];

const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="authSourcesList"></div></body>',
  // safe_html.js is the REAL template engine the module renders through;
  // eval'ing it (rather than stubbing safeHtml) keeps escaping honest.
  targets: [process.argv[4], process.argv[2]],
  globals: {
    confirm: () => true,
    Api: {
      authSources: {
        list: async () => ({ sources: window.__SOURCES }),
        upsert: async (body) => { posted.push(body); return { source: {} }; },
        toggle: async () => ({}),
        remove: async () => ({}),
      },
    },
  },
});

// The server row shape (lib/auth_sources.py _redact): spec travels with it.
const XHS_ROW = {
  domain: 'xiaohongshu.com', label: 'Xiaohongshu / RED',
  enabled: false, has_cookies: false, cookie_count: 0,
  has_proxy: false, proxy_hint: '',
  login_url: 'https://www.xiaohongshu.com/explore',
  fields: [
    { name: 'web_session', importance: 'required' },
    { name: 'a1', importance: 'recommended' },
    { name: 'webId', importance: 'optional' },
  ],
};

const $ = (id) => document.getElementById(id);
const inputs = () =>
  Array.from(document.querySelectorAll('#authSrcPanel_xiaohongshu_com .auth-src-field-input'));

const flush = () => new Promise((r) => process.nextTick(r));

(async () => {
  try {
    // ── 1. render from the server row ──
    window.__SOURCES = [JSON.parse(JSON.stringify(XHS_ROW))];
    _renderAuthSources();
    await flush();

    const html = $('authSourcesList').innerHTML;
    check('card_rendered', html.indexOf('auth-src-card') !== -1);

    // THE fix: one input per declared cookie, not one free-text blob.
    const els = inputs();
    check('one_input_per_field', els.length === 3);
    check('field_names_from_server',
      els.map((e) => e.getAttribute('data-cookie-name')).join(',') ===
      'web_session,a1,webId');
    check('importance_carried',
      els.map((e) => e.getAttribute('data-importance')).join(',') ===
      'required,recommended,optional');

    // The old free-text affordance must be GONE — leaving it would let the
    // delimiter-typo path back in through a second door.
    check('no_free_text_cookie_box',
      html.indexOf('auth-src-cookie') === -1 && html.indexOf('<textarea') === -1);

    // Login button uses the SERVER's url (no per-site table in the JS).
    check('login_url_from_server',
      html.indexOf('https://www.xiaohongshu.com/explore') !== -1);

    // ── 2. blank required field BLOCKS the request ──
    els[1].value = 'aaa';           // fill only the recommended one
    _authSourceSavePaste('xiaohongshu.com');
    await flush();
    check('blank_required_blocks_post', posted.length === 0);
    const msg = $('authSrcMsg_xiaohongshu_com');
    check('blank_required_names_the_cookie',
      (msg.textContent || '').indexOf('web_session') !== -1);
    check('blank_required_is_an_error', msg.className.indexOf('err') !== -1);

    // ── 3. filled → posts a structured MAP, not a header string ──
    els[0].value = 'tok';
    _authSourceSavePaste('xiaohongshu.com');
    await flush();
    check('post_sent', posted.length === 1);
    const body = posted[0] || {};
    check('posts_cookie_fields_map',
      !!body.cookie_fields && typeof body.cookie_fields === 'object');
    check('no_cookie_header_string', body.cookie_header === undefined);
    check('field_values_mapped_by_name',
      body.cookie_fields.web_session === 'tok' && body.cookie_fields.a1 === 'aaa');
    check('untouched_optional_omitted', body.cookie_fields.webId === undefined);
    check('enabled_on_connect', body.enabled === true);
    check('domain_sent', body.domain === 'xiaohongshu.com');

    // ── 4. a pasted `name=value` (or whole header) is UNWRAPPED, not stored
    //       verbatim — otherwise we'd persist a value of "web_session=tok". ──
    posted.length = 0;
    _renderAuthSources();
    await flush();
    const els2 = inputs();
    els2[0].value = 'web_session=tok2; a1=bbb';
    _authSourceSavePaste('xiaohongshu.com');
    await flush();
    check('pasted_pair_unwrapped',
      posted.length === 1 && posted[0].cookie_fields.web_session === 'tok2');
    check('pasted_header_splits_all_pairs',
      posted.length === 1 && posted[0].cookie_fields.a1 === 'bbb');

    // ── 5. NEUTER-adjacent: a row with NO declared fields degrades to one
    //       generic input. If the JS secretly kept its own xiaohongshu cookie
    //       table, this would still render 3 named inputs. ──
    posted.length = 0;
    const bare = JSON.parse(JSON.stringify(XHS_ROW));
    delete bare.fields;
    bare.domain = 'unknown.example';
    bare.login_url = '';
    window.__SOURCES = [bare];
    _renderAuthSources();
    await flush();
    const els3 = Array.from(
      document.querySelectorAll('#authSrcPanel_unknown_example .auth-src-field-input'));
    check('no_spec_one_generic_field', els3.length === 1);
    check('no_spec_not_hardcoded_xhs',
      els3[0].getAttribute('data-cookie-name') !== 'web_session');

    // ── 6. connected row shows cookie count (state text still honest) ──
    const conn = JSON.parse(JSON.stringify(XHS_ROW));
    conn.enabled = true; conn.has_cookies = true; conn.cookie_count = 2;
    window.__SOURCES = [conn];
    _renderAuthSources();
    await flush();
    check('connected_state_shown',
      $('authSourcesList').innerHTML.indexOf('2 cookies') !== -1);
  } catch (e) {
    check('harness_threw: ' + (e && e.message), false);
  } finally {
    report();
  }
})();
'''


def test_auth_sources_fields_frontend():
    run_harness(
        target_js=os.path.join(JS_DIR, 'settings', 'auth_sources.js'),
        extra_targets=[os.path.join(JS_DIR, 'core', 'safe_html.js')],
        body_js=_BODY,
        min_pass=21,
        label='auth-sources-fields',
    )
