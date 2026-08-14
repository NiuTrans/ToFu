"""Regression: `initActiveTasks` must load folders even when a SIBLING boot
fetch rejects.

WHY
---
Folder loading (`loadFolders`) used to be a third leg of a shared
``Promise.all`` in ``initActiveTasks``::

    const [, , activeResp] = await Promise.all([
      loadConversationsFromServer(prefetchTarget),
      loadFolders(),
      Api.chat.activeResponse(),
    ]);

``Promise.all`` short-circuits on the FIRST sibling rejection and discards the
other legs, then control jumps to the outer ``catch``. So a transient failure
in ``loadConversationsFromServer`` / ``Api.chat.activeResponse`` would skip
folder loading entirely — leaving ``_folders`` empty even though folders exist
server-side (the folder rail vanishes; only the "+ New folder" quick-add shows).
``loadFolders`` owns a bounded-backoff self-heal (``_scheduleFolderLoadRetry``)
but it only fires from ``loadFolders``' OWN catch, which never ran when a sibling
sank the shared ``Promise.all`` first.

THE FIX
-------
``loadFolders`` is DECOUPLED from the conversation/active-task ``Promise.all``:
kicked off in parallel (no perf loss) but with its own ``.catch`` that fires the
self-heal retry, and ``_migratePinnedToFolder`` chained after folders resolve.

This test drives the REAL shipped ``initActiveTasks`` under node with
``Api.chat.activeResponse`` REJECTING, and asserts ``loadFolders`` still ran.
DOUBLE-NEUTER: re-couple folders back into the ``Promise.all`` in a COPY and
prove ``loadFolders`` then does NOT run — i.e. the test discriminates the fix.

Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()


def _node_available() -> bool:
    return bool(shutil.which('node'))


# Drives the REAL initActiveTasks with a SIBLING (activeResponse) that REJECTS.
# The fixed, decoupled code must still call loadFolders(); the old coupled
# Promise.all would have skipped it.
_HARNESS = r"""
const fs = require('fs');
global.window = global;

// The real-world damage is NOT "was loadFolders invoked" — in the OLD coupled
// code loadFolders() is called synchronously as a Promise.all argument, so its
// fetch fires regardless. The damage is that the POST-LOAD CONTINUATION (apply
// folders + _migratePinnedToFolder + re-render) is SKIPPED when a sibling sinks
// the Promise.all. So we spy on the continuation: _migratePinnedToFolder, which
// the fixed code chains AFTER folders resolve, must run even under a sibling
// rejection. (In the coupled code it sat AFTER the awaited Promise.all, so a
// sibling rejection jumped past it to the catch.)
let foldersContinued = false;  // set true iff the post-folder continuation ran
let conversations = [];
global.conversations = conversations;
global.activeConvId = null;

global.loadConversationsFromServer = async () => {};
global.loadFolders = async () => {};
global.loadConversationMessages = async () => {};
global.Api = {
  chat: {
    // ★ A sibling boot fetch REJECTS. Under the old coupled Promise.all this
    //   sank the whole thing (folders skipped). Decoupled, folders still load.
    activeResponse: async () => { throw new Error('simulated activeResponse failure'); },
    poll: async () => ({ ok: true, json: async () => ({ status: 'done' }) }),
    active: async () => [],
  },
  conversations: { get: async () => null, put: async () => ({ ok: true }) },
};
global.startAssistantResponse = () => {};
global.connectToTask = () => {};
global.syncConversationToServer = async () => {};
global.saveConversations = () => {};
global.renderConversationList = () => {};
global.renderChat = () => {};
global.getActiveConv = () => null;
global.ConvCache = { put() {}, remove() {} };
global.debugLog = () => {};
global.escapeHtml = (s) => String(s == null ? '' : s);
global.normalizeErrorEnvelope = (e) => e;
global.errorEnvelopeKind = () => '';
global._ensureMsgId = (m) => m;
global._migratePinnedToFolder = () => { foldersContinued = true; };
global._scheduleFolderLoadRetry = () => {};
global._refreshServerQueue = () => {};
global.isBranchTaskId = () => false;
global.initBranchReconnect = () => {};
global.config = { model: 'aws.claude-opus-4.8' };
global.serverModel = 'aws.claude-opus-4.8';
global.activeStreams = new Map();
global.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global._editingMsgIdx = null;
global.showStreamingUIForConv = () => {};
global.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.clearTimeout = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // main/main_init_tasks.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof initActiveTasks !== 'function') { console.log('FAIL fn_exposed initActiveTasks missing'); return; }
  check('fn_exposed', true);

  await initActiveTasks();
  // Let the isolated folder chain settle (it runs independently of the awaited
  // conversation/active-task Promise.all).
  for (let i = 0; i < 50; i++) { await Promise.resolve(); }

  // THE INVARIANT: a sibling rejection must NOT prevent the folder-load
  // CONTINUATION (apply + migrate + re-render) from running.
  check('folders_loaded_despite_sibling_reject', foldersContinued === true);

  console.log('FOLDERS_CONTINUED=' + foldersContinued);
  console.log(out.join('\n'));
})();
"""


def _run_harness(js_source_path: str):
    harness = os.path.join(HERE, '_folders_decoupled_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, js_source_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    return proc


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_folders_load_despite_sibling_reject():
    src_js = os.path.join(JS_DIR, 'main', 'main_init_tasks.js')
    proc = _run_harness(src_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'folder-load decoupling regression:\n' + output
    assert 'PASS folders_loaded_despite_sibling_reject' in output, (
        'expected loadFolders to run even when a sibling boot fetch rejects:\n'
        + output)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_folders_load_decoupled_double_neuter(tmp_path):
    """DOUBLE-NEUTER: re-couple folders back into the shared Promise.all in a
    COPY of main_init_tasks.js and prove loadFolders then does NOT run when a
    sibling rejects — i.e. the test genuinely discriminates the fix. Real file
    untouched."""
    src_js = os.path.join(JS_DIR, 'main', 'main_init_tasks.js')
    with open(src_js, encoding='utf-8') as f:
        src = f.read()

    # The fix has TWO parts that BOTH must be undone to reproduce the old bug:
    #   (1) the isolated `_foldersDone` chain (loadFolders + .then(migrate) +
    #       .catch(retry)) that runs independently of the awaited Promise.all;
    #   (2) the 2-leg awaited Promise.all (conversations + activeResponse only).
    # If the neuter only swaps the await line, the surviving _foldersDone block
    # still continues folders — so the neuter must remove that block entirely
    # AND restore the old 3-leg await with loadFolders as a sibling + the
    # post-await _migratePinnedToFolder call.

    # (1) Remove the whole isolated _foldersDone block (from its opening const
    #     to the closing `});` of the .catch).
    block_start = src.index("    const _foldersDone = (typeof loadFolders")
    block_end_marker = ("      if (typeof _scheduleFolderLoadRetry === 'function') "
                        "_scheduleFolderLoadRetry();\n    });\n")
    block_end = src.index(block_end_marker) + len(block_end_marker)
    assert block_start < block_end, 'could not bound the _foldersDone block'
    src_no_block = src[:block_start] + src[block_end:]
    assert '_foldersDone' not in src_no_block, 'failed to strip the isolated folder block'

    # (2) Restore the OLD coupled 3-leg await + post-await migrate.
    anchor = (
        "    const [, activeResp] = await Promise.all([\n"
        "      // Metadata only. The old prefetch parameter embeds the archived\n"
        "      // messages blob; authoritative v2 conversations hydrate their turns\n"
        "      // below, while pre-cutover conversations retain on-demand fallback.\n"
        "      loadConversationsFromServer(null),\n"
        "      Api.chat.activeResponse(),\n"
        "    ]);"
    )
    assert anchor in src_no_block, 'boot Promise.all anchor not found — update the neuter target'
    recoupled = (
        "    const [, , activeResp] = await Promise.all([\n"
        "      // Metadata only. The old prefetch parameter embeds the archived\n"
        "      // messages blob; authoritative v2 conversations hydrate their turns\n"
        "      // below, while pre-cutover conversations retain on-demand fallback.\n"
        "      loadConversationsFromServer(null),\n"
        "      typeof loadFolders === 'function' ? loadFolders() : Promise.resolve(),\n"
        "      Api.chat.activeResponse(),\n"
        "    ]);\n"
        "    if (typeof _migratePinnedToFolder === 'function') _migratePinnedToFolder();"
    )
    neutered_src = src_no_block.replace(anchor, recoupled, 1)
    assert neutered_src != src_no_block, 'neuter did not change the source'
    nfile = tmp_path / 'main_init_tasks_recoupled.js'
    nfile.write_text(neutered_src, encoding='utf-8')

    proc = _run_harness(str(nfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    # Re-coupled: a rejecting sibling sinks the Promise.all → loadFolders never
    # runs → the invariant FAILS. If it still PASSes, the test can't discriminate.
    assert lines.get('folders_loaded_despite_sibling_reject') is False, (
        'DOUBLE-NEUTER did not bite: re-coupling loadFolders into the shared '
        'Promise.all still loaded folders under a sibling rejection — the test '
        'does not discriminate the fix.\n' + output)
