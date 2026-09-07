#!/usr/bin/env python3
"""Backend API-envelope drift ratchet (mirror of test_frontend_api_isolation.py).

Contract (docs/API_CONTRACT.md): every JSON response from a route MUST be
built by ``lib/api_response`` (``api_ok`` / ``api_error`` family /
``sse_response``). A bare ``jsonify(`` call in ``routes/**`` is legacy debt —
it skips the ``ok`` flag, the ``request_id`` correlation field, and the
central status-code policy.

What this suite enforces
------------------------
1. ``test_no_new_jsonify_files`` — a route file that currently has zero ad-hoc
   ``jsonify(`` must STAY at zero. New code goes through ``api_response``.
2. ``test_counts_only_decrease`` — every legacy file's ad-hoc count may only
   shrink. When you convert sites, paste the new lower counts into BASELINE
   in the SAME commit.
3. ``test_baseline_is_tight`` — a stale (too-generous) BASELINE entry FAILS,
   forcing the ratchet to be tightened as migration batches land (mirrors
   ``test_baseline_reflects_real_counts`` on the frontend guard).
4. ``test_carve_out_registry_valid`` — every carve-out file in
   ``CARVE_OUT_FILES`` must exist and still contain protocol-locked
   ``jsonify(`` calls; an entry whose file was fully converted / deleted is
   stale and must be removed (with the docs/API_CONTRACT.md §4 row).
5. ``test_baseline_and_carve_out_disjoint`` — meta: no file may be in both
   maps (an entry in one silently cancels the other).

Scanner semantics
-----------------
Counts LINES containing the substring ``jsonify(`` (same as ``grep -c``),
which intentionally excludes import lines (``from flask import jsonify`` has
no following paren). Deterministic, comment-tolerant: a comment mentioning
``jsonify(`` simply freezes into the baseline — the ratchet guards DELTA.
A line matching a ``CARVE_OUT_SITES`` snippet is SUBTRACTED from its file's
count (site-level carve-out: the one legal bare site inside an otherwise
migrated file — never a silent baseline remainder).

Established 2026-08-01 from a full-tree scan (272 ad-hoc sites across 33
files + 8 protocol-locked sites in 4 carve-out files). Same-day batches
zeroed api_v1/memory.py (10), api_v1/project.py (38), api_v1/mcp.py (21)
, api_v1/orchestrations.py (16 — its bare-array list site was MIGRATED
via the contract §4 coordinated front+back path, the first executed
instance, not registered as debt) , api_v1/desktop.py (11) , api_v1/skills.py (9) , api_v1/daily_report.py (9) , upload.py (10) , conversations.py (10, incl. TWO
bare-array list branches — wrapped backend-only, no first-party consumer)
and config.py (8 — templates bare-array via THREE-way coordination:
backend wrap + api.js unwrap + caller consumes directly)
and chat.py (11 — /chat/active bare-array, null-PRESERVING unwrap:
probe consumers distinguish zero-tasks from probe-failed)
, api_v1/artifacts.py (7) , oauth.py (7) and api_v1/translate.py (7 — poll-batch bare
array, null-preserving unwrap; poll-404 body-status collision via api_payload)
and api_v1/oauth.py (5 — provider-keyed status body verified
consumer-by-name before api_ok)
, api_v1/motion.py (5) , api_v1/browser.py (5) and api_v1/auth.py (4 — the
GLOBAL GATE rejection envelopes; 429 keeps post-build apply_headers)
and api_v1/folders.py + api_v1/paper_folders.py (3+3 twin batch —
bare-array lists wrapped; api_meta response schemas corrected to match)
and the batch-20 small sweep (15 sites / 8 files: conv_search bare
array coordinated, swarm/audio/translate/endpoint/compaction/poll_abort
plain, _task_routes api_payload; desktop.py bridge -> CARVE_OUT_FILES)
and batch 21 (push 3 + common 14 + chat_queue 3 — dispatch aggregates
verified nested-by-name before api_ok; queue bare array coordinated)
and the FINAL batch (paper.py 47 — the split-roadmap hold lifted after
verifying no sibling was splitting it; ~21 api_ok / 9 resp-passthrough
api_payload / 4 bare-ok:False api_payload / 3 custom-200 api_payload /
4 api_bad_request / 5 api_not_found / 1 api_payload(report, status)).
MIGRATION COMPLETE: BASELINE is now EMPTY — every route file is either
enveloped or a registered carve-out. The ratchet's job from here is
purely defensive (test_no_new_jsonify_files).
"""

from __future__ import annotations

import os

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
ROUTES_DIR = os.path.normpath(os.path.join(HERE, '..', 'routes'))

pytestmark = pytest.mark.unit

# ── Carve-out registry ───────────────────────────────────────────────
# Files whose responses are LOCKED by an external protocol — the tofu
# envelope would corrupt wire fidelity. Each entry MUST have a matching row
# in docs/API_CONTRACT.md §4 with the same reason. Keys are posix paths
# relative to routes/.
CARVE_OUT_FILES: dict[str, str] = {
    'compat_openai.py':
        'OpenAI wire-protocol emulation — an `ok` key corrupts the shape '
        'third-party SDKs parse',
    'compat_anthropic.py':
        'Anthropic wire-protocol emulation — same fidelity argument',
    'browser.py':
        'desktop-agent bridge long-poll protocol — parsed by the external '
        'desktop client binary, shape locked outside this repo',
    '_bridge_caller.py':
        'bridge caller helper — same external protocol as browser.py',
    'desktop.py':
        'desktop-agent bridge long-poll (/api/desktop/poll) — parsed by '
        'the external desktop client, same protocol family as browser.py',
}

# ── Site-level carve-out registry ────────────────────────────────────
# Individual jsonify( SITES that legally stay bare inside otherwise-migrated
# files. Keyed file → {unique snippet: reason}. A registered line is
# subtracted from the file's ad-hoc count by the scanner; every snippet MUST
# still exist in its file (stale entries fail test_carve_out_sites_valid),
# and each entry needs a matching row in docs/API_CONTRACT.md §4.
CARVE_OUT_SITES: dict[str, dict[str, str]] = {
    # The machinery is kept for FUTURE site-level carve-outs (e.g. the
    # chat_queue bare-array family when that file is migrated). Its first
    # entry — api_v1/orchestrations.py's ``return jsonify(_read_all())`` —
    # was NOT left registered: it was MIGRATED 2026-08-01 via the contract
    # §4 coordinated front+back path (backend api_ok({'items': …}) +
    # Api.orchestrations.list unwraps with an Array.isArray fallback),
    # the first executed instance of that path. Registration is for sites
    # that CANNOT migrate; elimination beats preservation wherever one can.
}

# ── Per-file ratchet baseline ────────────────────────────────────────
# Established 2026-08-01. Numbers MUST monotonically decrease toward {}.
# Do NOT raise a value here; when you migrate a file, lower its count (or
# delete the entry at zero) in the same commit.
BASELINE: dict[str, int] = {
}

_TOKEN = 'jsonify('


def _scan_all() -> dict[str, int]:
    """Walk routes/ for *.py (carve-outs excluded) → {posix_rel: line count}."""
    out: dict[str, int] = {}
    for root, dirs, files in os.walk(ROUTES_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(('.', '__'))]
        for name in sorted(files):
            if not name.endswith('.py'):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, ROUTES_DIR).replace(os.sep, '/')
            if rel in CARVE_OUT_FILES:
                continue
            registered = CARVE_OUT_SITES.get(rel, {})
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    count = sum(
                        1 for line in f
                        if _TOKEN in line
                        and not any(snip in line for snip in registered))
            except OSError:
                continue
            if count > 0:
                out[rel] = count
    return out


def test_no_new_jsonify_files():
    """A route file with zero ad-hoc jsonify today must STAY at zero."""
    actual = _scan_all()
    new_violators = sorted(set(actual) - set(BASELINE))
    if new_violators:
        details = '\n'.join(f'  {n}: {actual[n]} site(s)' for n in new_violators)
        pytest.fail(
            'New ad-hoc jsonify( in route files that were clean — responses '
            'must go through lib/api_response (docs/API_CONTRACT.md §2):\n'
            + details)


def test_counts_only_decrease():
    """Each legacy file's ad-hoc jsonify count may only shrink, never grow."""
    actual = _scan_all()
    regressions = []
    for name, baseline in BASELINE.items():
        cur = actual.get(name, 0)
        if cur > baseline:
            regressions.append((name, baseline, cur))
    if regressions:
        msg = '\n'.join(
            f'  {n}: baseline={b}, now={c} (+{c - b})'
            for n, b, c in regressions)
        pytest.fail(
            'Ad-hoc jsonify( count increased — new responses must use '
            'lib/api_response helpers (docs/API_CONTRACT.md §6 checklist):\n'
            + msg)


def test_baseline_is_tight():
    """A BASELINE entry above the actual count is stale — tighten it in the
    same commit that did the conversion (mirrors the frontend guard's
    stale-baseline test: a ratchet that cannot demand tightening never
    gets tightened)."""
    actual = _scan_all()
    stale = [(n, b, actual.get(n, 0))
             for n, b in BASELINE.items() if actual.get(n, 0) < b]
    assert not stale, (
        'BASELINE in tests/test_api_contract_drift.py is too generous — '
        'tighten it to the actual counts (delete entries that hit zero) so '
        'migrated files stay migrated:\n'
        + '\n'.join(f'  {n}: BASELINE={b}, actual={c}' for n, b, c in stale))


def test_carve_out_registry_valid():
    """Every carve-out file must exist and still carry protocol-locked
    jsonify( calls; a stale entry hides new drift behind an outdated reason."""
    for rel, reason in CARVE_OUT_FILES.items():
        path = os.path.join(ROUTES_DIR, rel)
        assert os.path.isfile(path), (
            f'carve-out file routes/{rel} vanished — remove the registry '
            f'entry AND the docs/API_CONTRACT.md §4 row')
        with open(path, 'r', encoding='utf-8') as f:
            count = sum(1 for line in f if _TOKEN in line)
        assert count > 0, (
            f'carve-out routes/{rel} no longer contains jsonify( — its '
            f'reason ({reason!r}) is stale; remove the registry entry and '
            f'the docs/API_CONTRACT.md §4 row, and keep it under BASELINE '
            f'watch if new ad-hoc sites remain')


def test_carve_out_sites_valid():
    """Every registered site snippet must still exist in its file — a stale
    site entry hides new drift behind an outdated reason (and a file-level
    registry cannot see it, since the file still has other jsonify lines).
    """
    for rel, sites in CARVE_OUT_SITES.items():
        path = os.path.join(ROUTES_DIR, rel)
        assert os.path.isfile(path), (
            f'carve-out site file routes/{rel} vanished — remove its '
            f'CARVE_OUT_SITES entry AND the docs/API_CONTRACT.md §4 row')
        with open(path, 'r', encoding='utf-8') as f:
            src = f.read()
        for snip, reason in sites.items():
            assert snip in src, (
                f'registered carve-out site {snip!r} no longer exists in '
                f'routes/{rel} — its reason ({reason!r}) is stale; remove '
                f'the registry entry and the contract §4 row (the site was '
                f'converted or deleted)')


def test_baseline_and_carve_out_disjoint():
    """No file may sit in both maps — one entry would silently cancel the
    other's guard."""
    overlap = set(BASELINE) & set(CARVE_OUT_FILES)
    assert not overlap, f'a file cannot be both baseline and carve-out: {overlap}'
