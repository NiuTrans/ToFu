"""tests/test_frontend_no_client_timeouts.py — the browser half of "no timeouts".

WHY
---
The transport-level read/first-byte timeouts were removed from the backend
(lib/llm/_transport.py, commit 1db38585) on the owner's rule: "unless it
crashes, what is there that can't be waited for? If I can't wait, I will
naturally pause it myself."

That is only half the objective. "I will pause it myself" happens IN THE
BROWSER, and the browser had its own ceilings that fired without the user
ever pressing anything:

  1. ``branch_stream.js`` aborted a branch SSE stream after 45s with no first
     byte — a client-side re-implementation of the exact TTFT kill just
     deleted from the transport, and 4x more aggressive than the 180s it
     replaced. The server kept generating (and billing); the browser had
     already dropped to the poll fallback.
  2. ``api.js`` applied a blanket 30000ms ceiling to every request that did
     not opt out. The backend's ``chat(timeout=120)`` and the dispatch total
     budget were deleted, and this silently re-imposed a *stricter* one —
     surfaced to the user as ``code:'timeout'``, i.e. "it died", when it had
     not. That the default was backwards is measurable: 22 call sites had to
     write ``timeout: 0`` to be allowed to wait.
  3. ``image-gen.js`` aborted generation at 150s and labelled it "timed out",
     discarding an image the server was still rendering.

THE RULE (same on both sides of the wire)
-----------------------------------------
**A liveness PROBE may bound itself. A WAIT may not.**

Probes keep their explicit budgets (health.check 3s/5s, backend_offline_monitor,
cross_tab_sync 8s/15s) — they answer "is the server there?", and an unbounded
probe is a hung UI. Anything that waits for the model to produce work is
unbounded, and ends when the user says so.

The last test is the durable one: a source scan (comment-stripped via
tests/_source_scan.strip_comments, charter #24) that fails when a NEW
abort-timer appears on a generation path, so the rule survives the next person
who reaches for setTimeout(...abort).

Run:  pytest tests/test_frontend_no_client_timeouts.py -m unit
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from tests._runtime_sections import runtime_sections_dir

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Shared comment-stripping primitive (charter #24). Import works whether or not
# tests/ is on sys.path as a package root — mirrors the dual form already used
# by test_chromium_binary_resolution.py / test_conv_state_rev_clock_domain.py.
try:
    from tests._source_scan import strip_comments
except ImportError:  # pragma: no cover - path-layout fallback
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _source_scan import strip_comments

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = runtime_sections_dir()
PAPER_FEATURES = os.path.join(ROOT, 'frontend', 'src', 'features', 'paper')
TYPED_TRANSPORT = os.path.join(ROOT, 'frontend', 'src', 'api', 'transport.ts')
CONVERSATION_SYNC = os.path.join(
    ROOT, 'frontend', 'src', 'core', 'conversation-sync.ts')
RUNTIME_PRELUDE = os.path.join(
    ROOT, 'frontend', 'src', 'runtime', 'sections', '_prelude.js')


def _src(*parts):
    with open(os.path.join(JS, *parts), encoding='utf-8') as f:
        return f.read()


def _live(*parts):
    """Source with comments stripped — charter #24: a comment must never be
    able to satisfy OR violate a guard."""
    return strip_comments(_src(*parts), lang='js')


def _live_paper_ts(name: str) -> str:
    with open(os.path.join(PAPER_FEATURES, name), encoding='utf-8') as source:
        return strip_comments(source.read(), lang='js')


def _live_native(path: str) -> str:
    with open(path, encoding='utf-8') as source:
        return strip_comments(source.read(), lang='js')


# ═════════════════════════════════════════════════════════════════════
#  A. api.js — the default must be "no timeout"
# ═════════════════════════════════════════════════════════════════════

_DEFAULT_RE = re.compile(r'const\s+timeout\s*=\s*([^;]+);')


@pytest.mark.unit
class TestApiDefaultIsUnbounded:
    def test_default_timeout_is_zero(self):
        """The request() default must be 0 (no ceiling). NEUTER target: put
        30000 back and this goes RED."""
        live = _live_native(TYPED_TRANSPORT)
        m = _DEFAULT_RE.search(live)
        assert m, 'could not locate the `const timeout = …` default in api.js'
        expr = m.group(1)
        assert '30000' not in expr, (
            f'api.js re-imposed a blanket client-side ceiling: {expr.strip()!r} '
            '— a wait must not be bounded; a probe passes its own budget')
        # Any bare numeric literal here is a blanket ceiling by definition.
        bare = re.findall(r'\b(\d{3,})\b', expr)
        assert not bare, (
            f'api.js default carries a numeric ceiling {bare} in {expr.strip()!r}')

    def test_explicit_budget_still_arms_a_timer(self):
        """Removing the DEFAULT must not remove a PROBE's ability to bound
        itself — the opposite failure. The `if (timeout > 0)` gate and the
        arming setTimeout must both survive, so a caller that passes an
        explicit budget still gets an abort.

        Structural rather than executed: api.js is a browser IIFE that binds to
        `window` and pulls in DOM/global state, so driving it under node needs
        a full page harness — out of proportion here, and a flaky harness is
        worse than an honest structural assertion. The pairing below (default
        is 0 AND the >0 branch still arms) is what makes both directions
        non-vacuous."""
        live = _live_native(TYPED_TRANSPORT)
        assert re.search(r'if\s*\(\s*timeout\s*>\s*0\b', live), (
            'the `timeout > 0` arming branch is gone — an explicit probe '
            'budget would now be silently ignored')
        assert re.search(
            r'timeoutId\s*=\s*(?:globalThis\.)?setTimeout\s*\(', live), \
            'the arming setTimeout is gone — explicit budgets no longer abort'

    def test_probe_call_sites_still_pass_their_own_budget(self):
        """The probes must not have been silently un-bounded by the default
        change — they carry their own budgets, so they are unaffected."""
        prelude = _live_native(RUNTIME_PRELUDE)
        assert 'AbortSignal.timeout(timeoutMs)' in prelude, (
            'the backend-availability health probe lost its explicit budget')


    def test_translation_has_no_abort_timer(self):
        """Found by the ratchet, not by hand: translation is an LLM generation
        and carried a 60s/90s/120s length-scaled abort.

        Scoped to the ABORT TIMER, not to any large constant: the same file's
        ``_AT_WATCHDOG_BUDGET_MS = 90000`` is a DB-POLLING self-heal for a
        dropped push frame — it aborts no request and clears a stuck spinner,
        so it is not a ceiling on a wait and must not be flagged."""
        live = _live('translation.js')
        assert not _ABORT_TIMER_RE.search(live), \
            'translation.js still arms a client-side abort timer'
        assert 'Translation timed out' not in live, \
            'translation.js still reports a client-side timeout to the user'

    def test_pdf_fetch_has_no_abort_timer(self):
        """Also a ratchet find: a byte transfer is a wait, not a probe."""
        live = _live_paper_ts('pdf-viewer.ts')
        assert not _ABORT_TIMER_RE.search(live), \
            'pdf-viewer.ts still bounds the PDF download with an abort timer'
        assert '120000' not in live


def _run_node(harness_src, *argv):
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as f:
        f.write(harness_src)
        path = f.name
    try:
        r = subprocess.run(['node', path, *argv], capture_output=True,
                           text=True, timeout=60)
        return (r.stdout or '') + (r.stderr or '')
    finally:
        os.unlink(path)


# ═════════════════════════════════════════════════════════════════════
#  B. The three generation paths carry no abort timer
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestGenerationPathsHaveNoTimer:
    def test_branch_sse_has_no_first_byte_abort(self):
        live = _live_native(CONVERSATION_SYNC)
        assert '45000' not in live, (
            'conversation-sync.ts still carries the 45s first-byte abort — that '
            'is the TTFT kill we removed from the transport, re-implemented '
            'in the browser')
        assert 'sseTimeout' not in live

    def test_branch_sse_keeps_its_poll_fallback(self):
        """Removing the timer must NOT remove the real error path: an SSE
        failure still falls back to polling."""
        live = _live_native(CONVERSATION_SYNC)
        assert 'new EventSource' in live
        assert 'this.recover(' in live

    def test_image_gen_has_no_watchdog_abort(self):
        live = _live('image-gen.js')
        for token in ('150_000', '150000', 'abortTimer'):
            assert token not in live, (
                f'image-gen.js still carries the 150s watchdog ({token}) — '
                'generation is a wait, not a crash')

    def test_image_gen_cancel_button_still_aborts(self):
        """The USER's way out must survive — that is the whole premise of
        removing the timer."""
        live = _live('image-gen.js')
        assert '_igCancelGeneration' in live
        assert re.search(r'_igAbortController\s*\.\s*abort\(\)', live), \
            'the Cancel handler no longer aborts the in-flight request'

    def test_image_gen_no_longer_claims_a_client_timeout(self):
        """An abort can now only be a user cancel, so the CLIENT must not label
        it a timeout.

        Deliberately scoped: ``_igClassifyError`` still maps a SERVER-reported
        ``error_type: 'timeout'`` to a timeout title, and that is correct — it
        is an upstream FACT the server told us, not a ceiling this client
        imposed. Asserting the string is absent everywhere would forbid
        reporting a real upstream timeout, so we assert on the client-side
        watchdog wording only."""
        live = _live('image-gen.js')
        assert 'timed out (150s)' not in live
        assert 'Request timed out (150s)' not in live
        # The abort branch must attribute an abort to the user, not to a timer.
        assert '_igUserCancelled' not in live, (
            'the user-cancel-vs-watchdog discriminator is still present — with '
            'no watchdog left there is nothing to discriminate against')


# ═════════════════════════════════════════════════════════════════════
#  C. ★ The durable one — a source-scan ratchet.
#     Nothing above stops the next person adding a fresh setTimeout(...abort)
#     on a generation path. This scans the whole frontend source for
#     abort-timers and requires each to be an ALLOW-LISTED liveness probe.
# ═════════════════════════════════════════════════════════════════════

#: Files whose abort-timers are LIVENESS PROBES (answer "is the server
#: there?"), which legitimately bound themselves. Everything else is a wait.
#: Adding a file here is a deliberate act that must be justified in review —
#: which is the point of the ratchet.
_PROBE_FILES = frozenset({
    'core/backend_offline_monitor.js',   # offline detection probe
    'core/conversation_catalog.js',      # bounded catalog liveness/retry probe
    'core/pending_sync.js',              # queued-write flush probe
    'main/main_send_pipeline.js',        # chat-START handshake (not the stream)
    'local-control.js',                  # loopback relay discovery/long-poll;
                                         # never waits on an LLM/task stream
    'diag_collect.js',                   # diagnostics collector
    'api.js',                            # the seam itself: arms a timer ONLY
                                         # when a caller passes an explicit
                                         # budget (asserted in section A)
})

#: An abort armed by a timer: `setTimeout(... abort ...)` or
#: `AbortSignal.timeout(...)`.
_ABORT_TIMER_RE = re.compile(
    r'setTimeout\s*\([^;]{0,200}?\.abort\s*\(|AbortSignal\s*\.\s*timeout\s*\(')


def _scan_abort_timers():
    """Every abort-timer in frontend SOURCE, as {relpath: [line numbers]}.

    Bundles / hashed build artifacts are excluded — they are generated from
    these sources, so flagging them would double-count and go stale.
    Comments are stripped first (charter #24).
    """
    found = {}
    for dirpath, dirnames, filenames in os.walk(JS):
        dirnames[:] = [d for d in dirnames if d not in ('node_modules', 'vendor')]
        for fn in filenames:
            if not fn.endswith('.js'):
                continue
            # Dotfiles too: `.bundle-<hash>.<rand>.js` is the bundler's
            # atomic-rename temp — a half-written bundle copy mid-build.
            if fn.startswith(('.', 'bundle-', 'feature-')):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, JS).replace(os.sep, '/')
            with open(full, encoding='utf-8') as f:
                live = strip_comments(f.read(), lang='js')
            hits = [i for i, line in enumerate(live.splitlines(), 1)
                    if _ABORT_TIMER_RE.search(line)]
            if hits:
                found[rel] = hits
    return found


@pytest.mark.unit
class TestNoNewAbortTimersOnWaits:
    def test_every_abort_timer_lives_in_an_allowlisted_probe(self):
        """THE RATCHET. A new `setTimeout(...abort)` / `AbortSignal.timeout()`
        outside the probe allow-list means someone re-imposed a client-side
        ceiling on a wait. If the new timer really is a probe, add its file to
        _PROBE_FILES *in the same commit* — that is the review gate."""
        found = _scan_abort_timers()
        offenders = {f: lines for f, lines in found.items()
                     if f not in _PROBE_FILES}
        assert not offenders, (
            'client-side abort timer(s) on non-probe path(s):\n'
            + '\n'.join(f'  {f}: line(s) {ls}' for f, ls in sorted(offenders.items()))
            + '\n\nA liveness PROBE may bound itself; a WAIT may not. If this is '
              'genuinely a probe, add the file to _PROBE_FILES with a reason.')

    def test_the_scan_actually_finds_things(self):
        """Anti-vacuity: a regex that matches nothing would make the ratchet
        above pass forever. The probe files MUST still show up."""
        found = _scan_abort_timers()
        assert found, 'the abort-timer scan found nothing at all — it is vacuous'
        assert any(f in _PROBE_FILES for f in found), \
            'no allow-listed probe matched — the pattern likely broke'

    def test_scan_ignores_comments(self):
        """charter #24 both directions: a commented-out timer must not trip
        the ratchet."""
        sample = (
            "// const t = setTimeout(() => ctrl.abort(), 45000);\n"
            "/* AbortSignal.timeout(30000) */\n"
            "const real = 1;\n"
        )
        live = strip_comments(sample, lang='js')
        assert not _ABORT_TIMER_RE.search(live), \
            'the ratchet matches commented-out code (charter #24 violation)'

    def test_scan_excludes_generated_bundles(self):
        found = _scan_abort_timers()
        assert not [f for f in found
                    if f.split('/')[-1].startswith(('bundle-', 'feature-'))], \
            'the scan is reading generated bundles — it will go stale'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
