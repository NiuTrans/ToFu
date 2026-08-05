"""Source guard + double-neuter: the `endpoint_iteration` dedup splice must
remove ONLY the stale worker row for that iteration (`splice(staleIdx, 1)`),
never truncate everything after it.

WHY (2026-08-05 live-view audit, writers census top-3)
------------------------------------------------------
A one-arg `splice(staleIdx)` deletes from staleIdx to the END of the array.
The endpoint_iteration handler's intent (per its own comment) is to drop the
stale DB-loaded WORKER twin for this iteration before pushing the fresh
streaming one. With a persisted user follow-up queued behind the stale worker
(reload + reconnect with a queued send), the to-end splice silently deleted
the user's message from memory — it only reappeared on the next reconcile, if
ever. The to-end contract belongs to `endpoint_new_turn` (documented: the
whole iteration is re-streamed), not to the worker-twin dedup.

Pins the exact call shape in the endpoint_iteration handler; the double-neuter
restores the one-arg splice on an in-memory copy and asserts the guard fails.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')

_SRC = os.path.join(JS_DIR, 'ui', 'sse_pipeline.js')


def _iteration_block(src: str) -> str:
    """Extract the endpoint_iteration dedup block (the findIndex + splice)."""
    m = re.search(
        r"Dedup: remove any stale DB-loaded worker for this iteration[\s\S]{0,900}?conv\.messages\.splice\(staleIdx[^)]*\);",
        src)
    assert m, 'endpoint_iteration dedup block not found — anchor drifted'
    return m.group(0)


def test_endpoint_iteration_splice_removes_single_row():
    with open(_SRC, encoding='utf-8') as f:
        src = f.read()
    block = _iteration_block(src)
    assert 'splice(staleIdx, 1)' in block, (
        'regression: endpoint_iteration dedup no longer removes exactly one row — '
        'a one-arg splice(staleIdx) truncates every message after the stale '
        'worker, including persisted user follow-ups.')
    # Belt: the bare one-arg form must not be what this block uses.
    assert not re.search(r'splice\(staleIdx\)', block), (
        'regression: one-arg splice(staleIdx) is back in endpoint_iteration — '
        'it drops everything after the stale worker.')


def test_NC_one_arg_splice_fails_the_guard():
    """DOUBLE-NEUTER: reverting to splice(staleIdx) on an in-memory copy must
    trip the guard — proving the assertion discriminates the fix."""
    with open(_SRC, encoding='utf-8') as f:
        src = f.read()
    needle = 'conv.messages.splice(staleIdx, 1);'
    assert needle in src, 'fixed call shape missing — update the neuter target'
    neutered = src.replace(needle, 'conv.messages.splice(staleIdx);', 1)
    block = _iteration_block(neutered)
    assert 'splice(staleIdx, 1)' not in block and re.search(r'splice\(staleIdx\)', block), (
        'DOUBLE-NEUTER did not bite: the one-arg splice survived — the guard '
        'does not discriminate.')
