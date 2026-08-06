#!/usr/bin/env python3
"""tests/test_frontend_synthetic_inject_marker_parity.py — every synthetic
inject-row filter site in the frontend must know EVERY lane marker.

THE drift class this pins
-------------------------
The backend owns the closed vocabulary of display-only inject lanes in
``lib/tasks_pkg/segments/_types.py::SYNTHETIC_INBOX_MARKERS``
(``_inboxInject`` / ``_peerInject`` / ``_userSteerInject`` / ``_stallNudge``).
The frontend has no shared predicate — by design of the jsdom/node harnesses,
which eval single files standalone, each consumer site open-codes the marker
list. That is exactly how the stall lane (added 2026-08, epic
pt_33ba079f5cea4841 / pt_5303eb3c7afb44a8) silently never joined the older
filters:

  * chat_render.js ``_realRounds`` never excluded ``_stallNudge`` → a trimmed
    turn's lone stall chip counted as a real round and suppressed the
    "Load tool activity (N)" affordance (conv msg0cop6qf64ee: 32 real rounds
    unreachable behind a 「使用了 1 个工具」 header).
  * tool_rounds.js ``_renderUnifiedGroup`` counted chips in the "N tools
    used" header while the segment-timeline path excluded them.

Since no type system links these lists across files, this guard is the
linkage: it extracts every known membership-filter site BY SYMBOL (never by
line number) and asserts each contains every marker the backend constant
declares. Adding a FIFTH backend lane turns this file red and names every
site that needs the new marker — the drift can no longer ship silently.

Run::
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \\
        tests/test_frontend_synthetic_inject_marker_parity.py -v
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS = os.path.join(ROOT, 'static', 'js')

# The four lane markers as the frontend writes them (singular form, the
# `round._X` flags). The backend tuple is parsed and compared against this
# set — a newly added backend lane fails HERE first, with instructions.
_KNOWN_LANES = ('_inboxInject', '_peerInject', '_userSteerInject', '_stallNudge')

# Display-sidecar names each lane rehydrates from (plural message fields).
_SIDECARS = ('_inboxInjects', '_peerInjects', '_userSteerInjects', '_stallNudges')


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


def _fn_span(src, name, rel):
    """Extract a top-level ``function <name>(...) {...}`` body (balanced
    braces), with a THREE-STATE diagnosis when the anchor is gone —
    "implementation deleted/renamed" must read differently from "guard
    drifted" (charter: source-anchored guards)."""
    hits = [m.start() for m in re.finditer(r'^function\s+' + name + r'\s*\(', src, re.M)]
    if not hits:
        raise AssertionError(
            f'{rel}: function {name}() not found — implementation deleted or '
            f'renamed; re-point this parity guard, do not delete it')
    if len(hits) > 1:
        raise AssertionError(
            f'{rel}: {name}() defined {len(hits)}x — single source of truth '
            f'was copied; collapse it before this guard can judge parity')
    start = hits[0]
    i = src.index('{', start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f'{rel}: unbalanced braces while slicing {name}()')


# ── The membership-filter sites (one row per consumer). ─────────────────
# (relpath, symbol, extractor) — extractor 'fn' = balanced-brace function
# body; 'const' = a top-level const derivation lifted by regex.
_SITES = (
    # anchor-placement skip-list: an inject row must never BE the anchor
    ('static/js/core.js', '_spliceInjectRow', 'fn'),
    # reload-path rehydration: every lane's sidecar must rebuild a row
    ('static/js/core.js', '_rehydrateInjectRows', 'fn'),
    # trimmed-turn affordance gate: chips must not count as real rounds
    ('static/js/ui/chat_render.js', '_realRounds', 'const'),
    # grouped panel header: chips must not inflate "N tools used"
    ('static/js/ui/tool_rounds.js', '_renderUnifiedGroup', 'fn'),
    # per-row debug entry: chips carry no round-scoped request mirror
    ('static/js/ui/tool_rounds.js', '_renderDebugEntry', 'fn'),
    # settled segment timeline: chip extraction + realRounds derivation
    ('static/js/ui/tool_rounds.js', 'renderSegmentTimelineHTML', 'fn'),
    # live DOM re-anchor pass
    ('static/js/ui/streaming_ui.js', '_repositionInjectGroups', 'fn'),
    # PUT-payload strip belt: synthetic rows must never reach the DB blob
    ('static/js/core/conv_persist_helpers.js', '_trimMsgForPersist', 'fn'),
)


def _backend_lanes():
    """Parse the backend SoT tuple → the set of lane markers it declares."""
    src = _read('lib/tasks_pkg/segments/_types.py')
    m = re.search(r'SYNTHETIC_INBOX_MARKERS\s*=\s*\(([^)]*)\)', src, re.S)
    assert m, ('lib/tasks_pkg/segments/_types.py: SYNTHETIC_INBOX_MARKERS not '
               'found — the backend lane vocabulary moved; re-point this guard')
    return set(re.findall(r"'(\w+)'", m.group(1)))


def test_backend_lane_vocabulary_is_the_known_four():
    """If the backend adds a FIFTH inject lane, this fails FIRST and names
    the work: every site in _SITES below must learn the new marker."""
    lanes = _backend_lanes()
    assert lanes == set(_KNOWN_LANES), (
        f'SYNTHETIC_INBOX_MARKERS now declares {sorted(lanes)} — the frontend '
        f'filter sites still know only {sorted(_KNOWN_LANES)}. Add the new '
        f'marker to every site in _SITES (and its rehydrate sidecar to '
        f'core.js::_rehydrateInjectRows), then update this constant.'
    )


def test_every_filter_site_knows_every_lane():
    lanes = sorted(_backend_lanes())
    failures = []
    for rel, symbol, kind in _SITES:
        src = _read(rel)
        if kind == 'fn':
            body = _fn_span(src, symbol, rel)
        else:
            m = re.search(r'const\s+' + symbol + r'\s*=.*?;', src, re.S)
            if not m:
                failures.append(f'{rel}: const {symbol} not found')
                continue
            body = m.group(0)
        missing = [lane for lane in lanes if lane not in body]
        if missing:
            failures.append(
                f'{rel}::{symbol} is missing {missing} — a chip of that lane '
                f'passes this filter as if it were a REAL tool round')
    assert not failures, (
        'synthetic-inject marker drift detected:\n' + '\n'.join(failures))


def test_rehydrate_reads_every_lane_sidecar():
    """The reload path must REBUILD a row for every lane — a sidecar no one
    reads renders the injection invisible after any refresh."""
    body = _fn_span(_read('static/js/core.js'), '_rehydrateInjectRows',
                    'static/js/core.js')
    missing = [s for s in _SIDECARS if s not in body]
    assert not missing, (
        f'core.js::_rehydrateInjectRows never reads {missing} — that lane '
        f'persists a sidecar the frontend drops on reload')
