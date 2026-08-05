"""Frontend image-cache recovery — double-neuter tests.

Symptom the fix addresses: reopening a conversation in the same browser shows
image tool rounds (read_files / inspect_image / browser_screenshot /
browser_preview_page) as bare badge-only lines — the inline thumbnail and
click-to-fullscreen are gone — even though the render worked live. Root cause
is the IndexedDB OOM guard: ``_stripToolRound`` (static/js/idb-cache.js) drops
``toolRounds[].results[].imageDataUris[].uri`` on cache write while the PUT/DB
copy keeps it, and the Phase-2 freshness check had NO disjunct for "server has
image uris the stripped cache lacks" — count, updatedAt, segments and
translation all match, so the stripped cache was judged FRESH and kept.

The fix (symmetric to the segments/translation recovery predicates):

  1. PREDICATE — static/js/core/conv_persist_helpers.js:
     ``_serverHasImagesLocalLacks(serverMsgs, localMsgs)`` — positional compare
     of aligned assistant toolRounds; true when a server round carries an
     imageDataUris uri and the aligned local round has none. Identity-guarded
     on content equality so a regenerated/edited turn aligned positionally is
     never misread as an images gap.
  2. WIRING — static/js/core/conversations.js: the predicate is a disjunct in
     the ``cacheIsStale`` OR-chain, so the stripped cache is judged STALE and
     ``conv.messages`` is replaced with the uri-carrying server copy (→ the
     thumbnails re-render).

Runs the REAL shipped predicate under node (no bundler, no DOM — it is pure).
Skips cleanly when node is absent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from tests._conv_bundle_sources import sources_defining

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
# Located by SYMBOL, not path: the persist-helper family was extracted out of
# core/conversations.js once already (pt_3879f00e slice 3).
PRED_JS = sources_defining('_serverHasImagesLocalLacks')[0]
CONV_JS = os.path.join(JS_DIR, 'core', 'conversations.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_plain_fn(src: str, name: str) -> str:
    """Extract a top-level `function <name>(...) { ... }` by brace matching."""
    m = re.search(r'(async\s+)?function %s\s*\(' % re.escape(name), src)
    assert m, f'{name} not found in source'
    i = src.index('{', m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(f'unbalanced braces extracting {name}')


def _run_node(script: str) -> dict:
    out = subprocess.run(
        ['node', '-e', script], capture_output=True, text=True, cwd=ROOT, timeout=60,
    )
    assert out.returncode == 0, f'node failed: {out.stderr}\n---\n{out.stdout}'
    last = [ln for ln in out.stdout.strip().splitlines()
            if ln.strip().startswith('{')][-1]
    return json.loads(last)


_STALE_DECISION = r"""
function cacheIsStale(cacheHit, serverMsgs, localMsgs, serverUpdatedAt, cachedUpdatedAt, USE_IMG) {
  return !cacheHit ||
    serverMsgs.length !== localMsgs.length ||
    serverUpdatedAt > (cachedUpdatedAt || 0) ||
    (USE_IMG ? _serverHasImagesLocalLacks(serverMsgs, localMsgs) : false);
}
"""

_HARNESS = r"""
'use strict';
const window = {};
__PREDICATE__
__DECISION__

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// A server copy whose image round carries the base64 uri vs the SAME round in
// the IndexedDB cache after _stripToolRound dropped every uri (format/filename
// kept). SAME message count + SAME updatedAt — the exact cache-fresh reload.
const server = [
  { role: 'user', content: 'U1', _msgId: 'm0' },
  { role: 'assistant', content: 'A1', _msgId: 'm1',
    toolRounds: [
      { roundNum: 1, status: 'done', toolName: 'browser_preview_page',
        results: [{ source: 'Browser', badge: 'captured',
          imageDataUris: [{ uri: 'data:image/jpeg;base64,AAAA', format: 'jpeg', filename: 'screenshot.jpeg' }] }] },
    ] },
];
const cache = [
  { role: 'user', content: 'U1', _msgId: 'm0' },
  { role: 'assistant', content: 'A1', _msgId: 'm1',
    toolRounds: [
      { roundNum: 1, status: 'done', toolName: 'browser_preview_page',
        results: [{ source: 'Browser', badge: 'captured',
          imageDataUris: [{ format: 'jpeg', filename: 'screenshot.jpeg' }] }] },  // ← uri stripped
    ] },
];

check('predicate_detects_stripped_images',
  _serverHasImagesLocalLacks(server, cache) === true);
const localWithUris = JSON.parse(JSON.stringify(server));
check('predicate_false_when_local_has_uris',
  _serverHasImagesLocalLacks(server, localWithUris) === false);
check('predicate_false_when_neither_has_images',
  _serverHasImagesLocalLacks(
    [{ role: 'assistant', content: 'x', toolRounds: [{ roundNum: 1, results: [{ source: 'x' }] }] }],
    [{ role: 'assistant', content: 'x', toolRounds: [{ roundNum: 1, results: [{ source: 'x' }] }] }]) === false);
check('predicate_ignores_non_assistant',
  _serverHasImagesLocalLacks(
    [{ role: 'user', toolRounds: [{ results: [{ imageDataUris: [{ uri: 'data:x' }] }] }] }],
    [{ role: 'user', toolRounds: [{ results: [{ imageDataUris: [{}] }] }] }]) === false);
// Identity guard: a DIFFERENT turn aligned positionally (e.g. a local regen
// whose content diverged) is not an images gap — only the SAME turn compares.
check('predicate_identity_guard_skips_different_turn',
  _serverHasImagesLocalLacks(server, [
    { role: 'user', content: 'U1', _msgId: 'm0' },
    { role: 'assistant', content: 'REGENERATED', _msgId: 'm1', toolRounds: [] },
  ]) === false);

// THE FIX: same count + same updatedAt, cacheHit=true → WITH the predicate the
// decision is STALE (server copy adopted → thumbnails re-render).
check('decision_stale_with_predicate',
  cacheIsStale(true, server, cache, 1000, 1000, true) === true);
// NC (biting): drop the images clause → the SAME inputs are judged FRESH.
check('NC_decision_fresh_without_predicate',
  cacheIsStale(true, server, cache, 1000, 1000, false) === false);
// Sanity: a real count change is still stale regardless (no regression).
check('count_change_still_stale',
  cacheIsStale(true, server.concat([{ role: 'user' }]), cache, 1000, 1000, false) === true);

console.log(JSON.stringify({ out }));
"""


def _run_recovery(use_real_predicate: bool = True) -> dict:
    src = open(PRED_JS, encoding='utf-8').read()
    if use_real_predicate:
        predicate = _extract_plain_fn(src, '_serverHasImagesLocalLacks')
    else:
        predicate = 'function _serverHasImagesLocalLacks() { return false; }'
    script = (_HARNESS
              .replace('__PREDICATE__', predicate)
              .replace('__DECISION__', _STALE_DECISION))
    return _run_node(script)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_recovery_predicate_flips_cacheisstale():
    """REAL `_serverHasImagesLocalLacks`: uri-stripped cache + uri server copy
    at equal count/updatedAt → cacheIsStale true (server copy wins, image
    thumbnails re-render instead of degrading to badge-only lines)."""
    r = _run_recovery(use_real_predicate=True)
    fails = [ln for ln in r['out'] if ln.startswith('FAIL')]
    assert not fails, 'image recovery predicate failures:\n' + '\n'.join(r['out'])
    for must in (
        'PASS predicate_detects_stripped_images',
        'PASS predicate_false_when_local_has_uris',
        'PASS predicate_false_when_neither_has_images',
        'PASS predicate_ignores_non_assistant',
        'PASS predicate_identity_guard_skips_different_turn',
        'PASS decision_stale_with_predicate',
        'PASS NC_decision_fresh_without_predicate',
        'PASS count_change_still_stale',
    ):
        assert must in r['out'], '\n'.join(r['out'])


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_nc_without_predicate_cache_stays_fresh():
    """NC: with the images clause removed, the uri-stripped cache is judged
    FRESH → the server's uri-carrying copy would be discarded (the bug)."""
    r = _run_recovery(use_real_predicate=False)
    assert 'FAIL predicate_detects_stripped_images' in r['out'], (
        'NC did not bite — a null predicate should fail detection:\n'
        + '\n'.join(r['out']))
    assert 'FAIL decision_stale_with_predicate' in r['out'], (
        'NC did not bite — without the predicate the decision is not stale:\n'
        + '\n'.join(r['out']))


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_wiring_disjunct_present_in_conversations():
    """WIRING: conversations.js's cacheIsStale OR-chain must actually CALL the
    predicate (a defined-but-unwired predicate renders the whole fix inert)."""
    src = open(CONV_JS, encoding='utf-8').read()
    assert '_serverHasImagesLocalLacks(serverMsgs, conv.messages)' in src, (
        'conversations.js cacheIsStale lost the _serverHasImagesLocalLacks '
        'disjunct — the predicate exists but nothing calls it')


if __name__ == '__main__':
    print(_run_recovery(True))
    print(_run_recovery(False))
