"""Co-Pilot workbench ↔ shared folder browser bridge (``project.js``).

The typed browser owns navigation, rendering and the optimistic delete
(``frontend/src/features/project/folder-browser.test.ts``). This suite drives
the REAL retained bridge under jsdom: the staged workspace list, the shared
workspace rail's root rows and the browser's added markers stay one state; a
confirmed directory delete unstages its workspace row in the same task as the
confirmation and re-stages it, with its read-only flag, when the server
refuses; the file drop zone resolves rows through the browser markup; and
closing the modal aborts the in-flight read.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

_PROJECT_JS = os.path.join(JS_DIR, 'project.js')

_BODY = r"""
(async () => {
const { setup } = require(process.env.JSDOM_HARNESS);
const scenario = process.argv[4];
const calls = { browse: [], rmdir: [], toasts: [] };
let serverHasAlpha = true;
let pendingBrowse = null;
let settleDelete;
const listing = (path) => ({
  path, parent: '/', filesCount: 0,
  dirs: (serverHasAlpha ? ['alpha', 'beta'] : ['beta'])
    .map((name) => ({ name, path: path + '/' + name })),
});
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
    '<div id="projectModal" class="open"><div class="modal project-modal pm-workbench">' +
    '<span id="pmMobileCount"></span><div class="pm-body">' +
    '<div class="pm-browser-pane"><div id="remoteDevicesSection" style="display:none"></div></div>' +
    '</div></div></div></body>',
  targets: [process.argv[2]],
  globals: {
    runtimeScope: {},
    showToast: (...args) => { calls.toasts.push(args); },
    Api: { project: {
      browse: (path, showHidden, options) => {
        calls.browse.push({ path, signal: options && options.signal });
        return pendingBrowse || Promise.resolve(listing(path));
      },
      rmdir: (path) => {
        calls.rmdir.push(path);
        return new Promise((resolve) => { settleDelete = resolve; });
      },
    } },
  },
});
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const browser = () => document.getElementById('folderBrowser');
const row = (path) => document.querySelector(
  '#folderBrowser .folder-browser-row[data-dir-path="' + path + '"]');
const tags = () => [...document.querySelectorAll(
  '#projectModal .pm-rail .mp-folder-list > li.mp-row[data-path]')]
  .map((li) => ({ path: li.dataset.path, readOnly: li.classList.contains('mp-row-readonly') }));
const staged = (path) => tags().find((tag) => tag.path === path);

await browseDirectory('/root');
check('browser_mounted_in_pane', !!document.querySelector('.pm-browser-pane > #folderBrowser'));
check('remote_group_adopted_as_prelude', !!browser().querySelector('#remoteDevicesSection'));
check('rows_rendered', !!row('/root/alpha') && !!row('/root/beta'));

row('/root/alpha').querySelector('[data-action="add"]').click();
check('add_stages_workspace_tag', !!staged('/root/alpha'));
check('add_marks_row', row('/root/alpha').dataset.added === 'true');
_mpRemove(0);
check('removing_tag_clears_marker', row('/root/alpha').dataset.added === 'false');
mpAddBrowsedPath('/root/alpha');
_mpToggleReadOnly(0);
check('staged_root_read_only', !!staged('/root/alpha') && staged('/root/alpha').readOnly);

check('drop_target_row',
  _folderDropTargetDir(row('/root/beta').querySelector('.folder-browser-name')) === '/root/beta');
check('drop_target_falls_back_to_open_folder', _folderDropTargetDir(document.body) === '/root');

row('/root/alpha').querySelector('[data-action="delete"]').click();
[...browser().querySelectorAll('button')]
  .find((button) => button.textContent === 'common.delete').click();
check('rmdir_called', calls.rmdir.join(',') === '/root/alpha');
check('row_removed_instantly', !row('/root/alpha'));
check('tag_unstaged_instantly', !staged('/root/alpha'));

if (scenario === 'ok') {
  serverHasAlpha = false;
  settleDelete({ ok: true, json: async () => ({ ok: true }) });
  await flush();
  check('success_toast', calls.toasts.some((args) => args[0] === 'folder.deleted' && args[1] === 'success'));
  check('row_stays_gone', !row('/root/alpha'));
  check('tag_stays_gone', !staged('/root/alpha'));
} else {
  settleDelete({ ok: false, json: async () => ({ detail: 'boom' }) });
  await flush();
  check('row_restored_from_server', !!row('/root/alpha'));
  check('tag_restaged_read_only', !!staged('/root/alpha') && staged('/root/alpha').readOnly);
  check('server_reason_visible', browser().querySelector('.folder-browser-alert').textContent.includes('boom'));
}

let release;
pendingBrowse = new Promise((resolve) => { release = resolve; });
const pending = browseDirectory('/elsewhere');
const signal = calls.browse[calls.browse.length - 1].signal;
closeProjectModal();
check('close_aborts_read', !!signal && signal.aborted);
release(listing('/elsewhere'));
await pending; await flush();
check('late_result_ignored', !row('/elsewhere/beta'));
report();
process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
"""


@pytest.mark.parametrize('scenario', ['ok', 'refused'])
def test_copilot_bridge_keeps_workspace_and_browser_in_step(scenario):
    run_harness(
        _PROJECT_JS, _BODY, extra_targets=[scenario],
        expect_pass=17, label=f'folder browser bridge ({scenario})',
    )
