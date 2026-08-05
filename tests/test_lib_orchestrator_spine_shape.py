"""Structural ratchet for pt_03f4cdf1 slice 38 — the delegation-comment
pointer-ization of _run.py must not re-bloat.

After slice 38 the spine carries ONE-LINE pointers
(``# ★ <what> (slice N → _leaf)``) — each leaf module's docstring is the
contract home. This guard fails when a delegation comment block grows
past 3 lines again (the pre-slice-38 shape was 4-11 lines each).

NEUTER: restoring ONE verbose delegation comment flips this RED.
"""

from __future__ import annotations

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'


def _delegation_comment_spans():
    """Yield (start_line, n_lines) for every contiguous comment block that
    directly precedes a delegation call inside run_task's body."""
    lines = RUN_PY.read_text().splitlines()
    in_fn = False
    spans = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('def run_task('):
            in_fn = True
        if in_fn and line.strip().startswith('#'):
            j = i
            while j < len(lines) and lines[j].strip().startswith('#'):
                j += 1
            # the line AFTER the block: a delegation call (indented code
            # that is not a comment) inside the function body
            if (j < len(lines) and lines[j].strip()
                    and not lines[j].strip().startswith('#')):
                spans.append((i + 1, j - i))
            i = j
        else:
            i += 1
    return spans


def test_delegation_comments_stay_pointer_sized():
    spans = _delegation_comment_spans()
    lines = RUN_PY.read_text().splitlines()
    bloated = []
    for ln, n in spans:
        if n <= 3 or ln <= 160:
            continue
        # Only DELEGATION POINTER blocks are ratcheted: the new arrow form
        # ('slice N → _leaf') OR the old verbose form (mentions the leaf
        # module path 'orchestrator._…'). WHY comments (set_req_id, the
        # WHILE-loop ceiling rationale, the rs-container note) mention
        # neither and are exempt.
        block = '\n'.join(lines[ln - 1:ln - 1 + n])
        if '→' in block or 'orchestrator._' in block:
            bloated.append((ln, n))
    assert not bloated, (
        'delegation comment blocks re-bloated past 3 lines (slice 38 '
        'pointer-ization regressed — keep a one-line pointer, the leaf '
        f'docstring is the contract home): {bloated}')


def test_run_task_function_within_owner_line():
    """Owner DONE definition: run_task as a pure orchestration spine
    ≤ ~350 L (function body, def line through final dedent)."""
    lines = RUN_PY.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith('def run_task('))
    body = len(lines) - start
    assert body <= 350, (
        f'run_task function body is {body} lines (owner line: ≤ ~350) — '
        'extract more or compress; do not let the spine re-grow')
