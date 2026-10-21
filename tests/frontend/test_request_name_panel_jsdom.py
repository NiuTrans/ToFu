#!/usr/bin/env python3
"""tests/frontend/test_request_name_panel_jsdom.py — the panel as the operator sees it.

WHY
===
The header that routes ``kunlunbedrock.gpt-6.1-sol-m17`` lives on its
Connection. The panel read only the request name's own headers, so opening
that alias showed an EMPTY form: the operator reported "we added the header and
now it is gone" while every request carried it. This suite drives the real
widget in jsdom and pins what the form must show and submit:

  * the inherited selector appears as a row, editable, marked with where it
    comes from;
  * submitting it untouched stores NOTHING on the request name (the Connection
    already sends it — a duplicate would be a second owner);
  * deleting it edits the Connection only when this request name owns that
    Connection; on a shared Connection the panel refuses and stays open.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / 'frontend/src/features/settings/provider-request-name-panel.ts'

SETUP = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!doctype html><body></body>',
  targets: [process.argv[2]],
});
const panel = globalThis;
const settle = async () => { for (let i = 0; i < 4; i++) await new Promise(r => setImmediate(r)); };
const open = (options) => panel.openRequestNamePanel({
  host: document.body,
  title: 'edit',
  initial: { wireModelId: 'kunlunbedrock.gpt-6.1-sol-m17', headers: [] },
  ...options,
});
const rows = () => [...document.querySelectorAll('.stg-rnp-header-row')];
const okButton = () => [...document.querySelectorAll('.stg-rnp-actions button')].at(-1);
const type = (input, value) => { input.value = value; input.dispatchEvent(new window.Event('input', { bubbles: true })); };
const INHERITED = { 'x-context-provider': 'kunlun' };
'''

BODY = SETUP + r'''
(async () => {
  try {
    // 1. The inherited selector is the form's opening state, not a blank row.
    let pending = open({ inherited: INHERITED, sharedWith: 1 });
    await settle();
    check('one row for the inherited header', rows().length === 1);
    const row = rows()[0];
    check('row is marked inherited', row.classList.contains('is-inherited'));
    const chip = row.querySelector('.stg-rnp-inherited');
    check('chip says where the value comes from',
          !!chip && chip.textContent === 'settings.requestNameHeaderInherited');
    const hint = document.querySelector('[data-rnp-inherited-hint]');
    check('hint explains the shared Connection',
          !!hint && hint.textContent.startsWith('settings.requestNameInheritedHintShared'));
    const nameInput = row.querySelector('.stg-rnp-header-name');
    const valueInput = row.querySelector('.stg-rnp-header-value');
    check('inherited name is prefilled', nameInput.value === 'x-context-provider');
    check('inherited value is prefilled', valueInput.value === 'kunlun');

    // 2. Editing clears the provenance mark; the row is now this name's own.
    type(valueInput, 'east');
    check('editing drops the inherited mark',
          !row.classList.contains('is-inherited') && !row.querySelector('.stg-rnp-inherited'));
    type(valueInput, 'kunlun');

    // 3. Submitting unchanged must not duplicate the Connection header.
    okButton().click();
    await settle();
    const unchanged = await pending;
    check('untouched form submits', unchanged !== null);
    check('submitted row keeps the connection value',
          unchanged && unchanged.headers.length === 1
          && unchanged.headers[0].name === 'x-context-provider'
          && unchanged.headers[0].value === 'kunlun');
    const write = panel.resolveRequestNameHeaders(unchanged.headers, INHERITED);
    check('nothing stored on the request name', Object.keys(write.deployment).length === 0);
    check('nothing removed from the connection', write.removed.length === 0);
    check('edited form closes its overlay', document.querySelector('.stg-rnp-overlay') === null);

    // 4. A shared Connection cannot lose the header for one name alone.
    pending = open({ inherited: INHERITED, sharedWith: 1 });
    await settle();
    rows()[0].querySelector('button').click();
    check('the inherited row can be deleted', rows().length === 0);
    okButton().click();
    await settle();
    const error = document.querySelector('.stg-rnp-error');
    check('removal on a shared connection is refused',
          !!error && error.textContent.startsWith('settings.requestNameInheritedShared'));
    check('refused form stays open', document.querySelector('.stg-rnp-overlay') !== null);
    document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
    await settle();
    check('escape cancels', (await pending) === null);

    // 5. On an unshared Connection the deletion reaches the Connection owner.
    pending = open({ inherited: INHERITED, sharedWith: 0 });
    await settle();
    rows()[0].querySelector('button').click();
    okButton().click();
    await settle();
    const alone = await pending;
    const aloneWrite = panel.resolveRequestNameHeaders(alone.headers, INHERITED);
    check('unshared removal is submitted for the connection',
          aloneWrite.removed.length === 1 && aloneWrite.removed[0] === 'x-context-provider');
    check('unshared removal stores no request-name header',
          Object.keys(aloneWrite.deployment).length === 0);
  } catch (failure) {
    check('unexpected_' + String(failure && failure.stack || failure), false);
  } finally {
    report();
  }
})();
'''


def test_the_panel_shows_and_edits_the_header_inherited_from_its_connection():
    module = native_module_path('request-name-panel-jsdom.js', PANEL)
    run_harness(target_js=module, body_js=BODY, expect_pass=18,
                label='request-name panel')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
