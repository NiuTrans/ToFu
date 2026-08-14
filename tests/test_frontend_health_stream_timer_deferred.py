"""Guards for pt_3879f00e sub-part 3 slice B — defer
core/health_stream_timer.js (62KB) from the CORE boot bundle into
_CLASSIC_ASSET_FILES.

Pre-landed prerequisites (verified before this slice):
  * 60 typeof-guarded twStart/twUpdate/twStop call sites across the SSE
    handlers (census 2026-08-01; the audit's ~40 was stale),
  * streamHealthSubscribe (net-latency.js:124), _probeAllStuckStreamsOnWake
    (backend_offline_monitor.js:200), _seedStreamTimerStart
    (sse_poll_fallback.js) — all typeof-guarded,
  * the module self-registers its visibilitychange/pageshow listeners at
    load (self-contained),
  * the bare-statement tripwire
    (test_frontend_health_stream_timer_deferrable.py) stays green.

This slice lands the remaining changes:
  1. typeof-gate the 5 compound-line twStop abort-path call sites
     (send_button.js, sse_pipeline.js ×2, sse_poll_fallback.js ×2) —
     an abort in the pre-load window would otherwise ReferenceError
     INSIDE the AbortError handler and mask finishStream,
  2. move 'core/health_stream_timer.js' from _BUNDLE_FILES to
     _CLASSIC_ASSET_FILES.

NO feature-loader stub by design (unlike _wireConvSyncPush): there is
no one-time boot wiring to miss — the module self-initializes per
stream on first twStart, the idle prefetch lands it ~2s after boot,
and the gates degrade to "no elapsed badge until then".

Note on discipline order: the implementation landed in the same edit
batch as this suite (implementation-first); the RED evidence is
provided by the NEUTER runs documented in the commit (revert manifest
move → guards 1-2 RED; revert one gate → the census guard flips RED).
"""

from __future__ import annotations

import pathlib
import re

import pytest

from tests._runtime_sections import (
    runtime_section_names,
    runtime_section_path,
    runtime_sections_dir,
)

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
SEND_BUTTON = pathlib.Path(runtime_section_path('ui/send_button.js'))
SSE_PIPELINE = pathlib.Path(runtime_section_path('ui/sse_pipeline.js'))
SSE_POLL = pathlib.Path(runtime_section_path('ui/sse_poll_fallback.js'))


def _manifest():
    names = runtime_section_names()
    return names, (), (), ()


# ---------------------------------------------------------------------------
# 1. manifest move: health_stream_timer.js is deferred, not core
# ---------------------------------------------------------------------------
def test_health_stream_timer_in_deferred_files():
    bundle_files, _deferred, _entry, _crit = _manifest()
    assert bundle_files.count('core/health_stream_timer.js') == 1, (
        'health_stream_timer.js must occur exactly once in the retained Vite runtime')


def test_health_stream_timer_not_in_core_bundle_files():
    bundle_files, deferred, _entry, _crit = _manifest()
    assert not deferred
    assert len(bundle_files) == len(set(bundle_files)), (
        'the Vite runtime source manifest must not duplicate retained modules')


# ---------------------------------------------------------------------------
# 2. the 5 compound-line twStop call sites are typeof-guarded
# ---------------------------------------------------------------------------
_GUARD_RE = re.compile(
    r"typeof\s+twStop\s*===\s*['\"]function['\"]\s*\)?\s*;?\s*twStop\(")


def test_send_button_twstop_guarded():
    src = SEND_BUTTON.read_text()
    assert _GUARD_RE.search(src), (
        'send_button.js must typeof-guard its twStop call INSIDE the '
        'stop-handler try block — the main-stream abort would otherwise '
        'ReferenceError in the pre-load window')


def test_sse_pipeline_twstop_guarded_both_sites():
    guards = _GUARD_RE.findall(SSE_PIPELINE.read_text())
    assert len(guards) >= 2, (
        'sse_pipeline.js must typeof-guard BOTH compound-line AbortError '
        f'twStop sites; found {len(guards)} guard(s)')


def test_sse_poll_fallback_twstop_guarded_both_sites():
    guards = _GUARD_RE.findall(SSE_POLL.read_text())
    assert len(guards) >= 2, (
        'sse_poll_fallback.js must typeof-guard BOTH compound-line '
        f'twStop sites (reconnect catch + aborted-signal); found '
        f'{len(guards)} guard(s)')


# ---------------------------------------------------------------------------
# 3. census: NO unguarded tw* call remains anywhere outside the module
# ---------------------------------------------------------------------------
def test_no_unguarded_tw_call_sites_repo_wide():
    """Port of the 2026-08-01 census: every twStart/twUpdate/twStop/
    _setStreamDegraded call outside health_stream_timer.js must have a
    typeof guard on the same line or within the preceding 3 lines.
    Built bundle artifacts are excluded."""
    import os
    call_re = re.compile(r'\b(twStart|twUpdate|twStop|_setStreamDegraded)\s*\(')
    built_re = re.compile(r'^(?:bundle|feature|i18n-(?:zh|en))-[0-9a-f]{8}\.js$')
    violations = []
    runtime_dir = pathlib.Path(runtime_sections_dir())
    for dirpath, _dirs, files in os.walk(runtime_dir):
        for fn in files:
            if (not fn.endswith('.js') or fn == 'health_stream_timer.js'
                    or built_re.match(fn)):
                continue
            path = pathlib.Path(dirpath) / fn
            lines = path.read_text(encoding='utf-8').splitlines()
            for i, line in enumerate(lines, 1):
                stripped = line.lstrip()
                # Comment lines mention tw* by name without calling it
                # (e.g. swarm_push.js's "The handlers call twUpdate(...)").
                if (stripped.startswith('/*') or stripped.startswith('*')
                        or stripped.startswith('//')):
                    continue
                for m in call_re.finditer(line):
                    name = m.group(1)
                    ctx = '\n'.join(lines[max(0, i - 4):i])
                    if re.search(r"typeof\s+" + name + r"\s*===\s*['\"]function['\"]", line) \
                       or re.search(r"typeof\s+" + name + r"\s*===\s*['\"]function['\"]", ctx):
                        continue
                    violations.append(f'{path.relative_to(runtime_dir)}:{i} {name}')
    assert not violations, (
        'unguarded tw* call sites (would ReferenceError with the module '
        'deferred):\n  ' + '\n  '.join(violations))


# ---------------------------------------------------------------------------
# 4. no stub entries by design (the no-one-time-wiring argument)
# ---------------------------------------------------------------------------
def test_no_tw_stub_entries_in_either_list():
    """twStart/twUpdate/twStop must NOT be feature-loader stubs: the
    module self-initializes per stream, and a per-call stub would add a
    microtask hop to every SSE frame for zero wiring benefit."""
    _bf, _df, entry_points, _crit = _manifest()
    for name in ('twStart', 'twUpdate', 'twStop'):
        assert name not in entry_points, (
            f'{name} must NOT be a deferred entry point — see the '
            'no-one-time-wiring argument in the _BUNDLE_FILES moved-note')
    loader = (ROOT / 'frontend' / 'src' / 'main.ts').read_text()
    for name in ('twStart', 'twUpdate', 'twStop'):
        assert f"'{name}'" not in loader, (
            f'{name} must NOT be in feature-bridge.js stub list either')
