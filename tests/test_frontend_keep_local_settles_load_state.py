"""Regression: the KEEP_LOCAL reconcile branch in `loadConversationMessages`
must settle load-state (`conv._needsLoad = false` + `conv._serverMsgCount = ...`)
exactly like the MERGE_ACTIVE_TASK and OVERWRITE branches do.

WHY
---
`loadConversationMessages` (static/js/core/conversations.js) reconciles the
freshly-fetched server copy against local in-memory `conv.messages`. When
`localHasUnsynced` is true (a just-sent optimistic message, or a durable
pending-sync tail carried across a reload) it takes the KEEP_LOCAL branch and
keeps the local copy. But — unlike every OTHER branch — it used to NOT set
`conv._needsLoad = false` nor update `conv._serverMsgCount`. Two poor-network
consequences:

  1. `_needsLoad` stays truthy → a later refocus / cross-device poll re-enters
     Phase-2, re-fetches, and once the fresh-activity window has closed takes
     the OVERWRITE branch → the just-sent local message BLANKS until the server
     catches up.
  2. `_serverMsgCount` stays stale (often HIGHER than reality) →
     `syncConversationToServer`'s count-drop data-loss guard silently DROPS a
     subsequent legitimate edit/truncate.

The fix appends, at the end of the KEEP_LOCAL branch:
    conv._needsLoad = false;
    conv._serverMsgCount = Math.max(serverMsgs.length, conv.messages.length);
(`Math.max` so a longer KEPT local tail stays authoritative).

Test strategy (source-region isolation on the REAL shipped file):
  * Locate the KEEP_LOCAL branch — the `if (localHasUnsynced) {` block up to the
    following `} else if (conv.activeTaskId`.
  * Assert BOTH settle statements appear INSIDE that region (not merely
    elsewhere in the function — the OVERWRITE/MERGE branches also have them).
  ★ Double-neuter: delete the two settle lines from the region → the region no
    longer contains them → assertions FAIL; restore byte-identical → PASS.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CONV_JS = os.path.join(ROOT, 'static', 'js', 'core', 'conversations.js')


def _keep_local_region(src: str) -> str:
    """Return the source of the KEEP_LOCAL branch body.

    The branch is the `if (localHasUnsynced) {` block; it ends at the next
    top-level `} else if (conv.activeTaskId && hasLocalData) {` (the
    MERGE_ACTIVE_TASK branch). Both anchors are stable, distinctive strings in
    the reconcile selector.
    """
    start = src.index('if (localHasUnsynced) {')
    end = src.index('} else if (conv.activeTaskId && hasLocalData) {', start)
    return src[start:end]


def test_keep_local_branch_settles_load_state():
    with open(CONV_JS, encoding='utf-8') as f:
        src = f.read()

    region = _keep_local_region(src)

    # ★ THE FIX: both settle statements must live INSIDE the KEEP_LOCAL branch.
    assert 'conv._needsLoad = false;' in region, (
        'KEEP_LOCAL branch does not clear conv._needsLoad — a later refocus/poll '
        're-enters Phase-2 and can OVERWRITE the just-sent local message.')

    assert re.search(
        r'conv\._serverMsgCount\s*=\s*Math\.max\(\s*serverMsgs\.length\s*,\s*conv\.messages\.length\s*\)',
        region), (
        'KEEP_LOCAL branch does not settle conv._serverMsgCount = '
        'Math.max(serverMsgs.length, conv.messages.length) — a stale (higher) '
        'count makes syncConversationToServer silently drop a later edit.')

    # Behaviour-preservation: the sibling MERGE_ACTIVE_TASK branch still settles
    # the same fields (guards against accidentally MOVING them rather than
    # ADDING to KEEP_LOCAL).
    merge_start = src.index('} else if (conv.activeTaskId && hasLocalData) {')
    merge_end = src.index('} else if (!hasLocalData', merge_start)
    merge_region = src[merge_start:merge_end]
    assert 'conv._needsLoad = false;' in merge_region
    assert 'conv._serverMsgCount = Math.max(serverMsgs.length, conv.messages.length);' in merge_region


if __name__ == '__main__':
    test_keep_local_branch_settles_load_state()
    print('PASS test_keep_local_branch_settles_load_state')
