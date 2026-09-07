"""Regression: class-level content-track divergence guard.

The FloorRetry silent-loss bug (commit 6464592) belongs to a broader CLASS:
``task['content']`` is the append-only delta accumulator that ``_sync``
persists, while the returned ``assistant_msg['content']`` is the authoritative
answer. Any retry/resend/fallback path that swaps in a fresh authoritative msg
WITHOUT reconciling the accumulator re-creates the exact silent loss (billed for
a long answer, DB stores a short residue).

The point fix (converge task['content'] in _stream.py) closes the ONE live door
(FloorRetry, the unique on_content=None path). This guard is the CLASS-LEVEL
net: ``_check_suspicious_completion`` runs at finalize with BOTH the persisted
``task['content']`` and the authoritative ``assistant_msg`` in hand, so it
catches ANY future path that desyncs them — path-agnostic, loud (error.log +
audit), instead of silent.

These tests pin the guard's decision boundary:
  * fires on a genuine divergence (long authoritative, short persisted);
  * silent when the two agree (the fixed / normal path);
  * silent on marginal / footer-sized deltas (no false positives);
  * silent when aborted (a Stop legitimately truncates the accumulator).

NEUTER counter-proof: comment out the divergence block in _finalize.py and
test_guard_fires_on_divergence flips red; restore -> green.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_task(content='', thinking='', aborted=False, tid='divg-1'):
    return {
        'id': tid,
        'convId': 'cdiv',
        'content': content,
        'thinking': thinking,
        'aborted': aborted,
        'error': None,
        'created_at': time.time() - 30,   # not "too fast"
        'content_lock': threading.Lock(),
    }


@pytest.mark.unit
class TestContentTrackDivergenceGuard:

    def _reasons(self, task, assistant_msg, finish='stop', tool_happened=True):
        from lib.tasks_pkg.orchestrator._finalize import _check_suspicious_completion
        return _check_suspicious_completion(
            task, finish, 'no_tool_calls_round_3',
            tool_happened, 3, 'aws.claude-opus-4.8',
            assistant_msg=assistant_msg)

    def test_guard_fires_on_divergence(self):
        """The exact 3411→215 shape: authoritative long, persisted short residue."""
        task = _make_task(content='X' * 215, thinking='t' * 300)
        asst = {'role': 'assistant', 'content': 'Y' * 3411}
        reasons = self._reasons(task, asst)
        assert any(r.startswith('content_track_divergence') for r in reasons), reasons

    def test_guard_silent_when_tracks_agree(self):
        """Fixed / normal path: task['content'] == assistant_msg['content']."""
        full = 'Z' * 3411
        task = _make_task(content=full, thinking='t' * 300)
        asst = {'role': 'assistant', 'content': full}
        reasons = self._reasons(task, asst)
        assert not any(r.startswith('content_track_divergence') for r in reasons), reasons

    def test_guard_silent_on_marginal_delta(self):
        """A server-side footer / sanitize tweak (small shrink) must NOT trip it.

        800-char answer, persisted 780 (a 20-char footer strip): ratio 0.975,
        gap 20 — well inside both thresholds (0.6 ratio, 200-char gap)."""
        task = _make_task(content='A' * 780, thinking='t' * 100)
        asst = {'role': 'assistant', 'content': 'A' * 800}
        reasons = self._reasons(task, asst)
        assert not any(r.startswith('content_track_divergence') for r in reasons), reasons

    def test_guard_silent_when_aborted(self):
        """A user Stop legitimately leaves a truncated accumulator — not a bug."""
        task = _make_task(content='X' * 100, thinking='', aborted=True)
        asst = {'role': 'assistant', 'content': 'Y' * 3000}
        reasons = self._reasons(task, asst, finish='stop')
        assert not any(r.startswith('content_track_divergence') for r in reasons), reasons

    def test_guard_silent_on_short_authoritative(self):
        """When the authoritative answer is itself short (<=200), the accumulator
        being empty is covered by other checks, not the divergence guard (which
        needs a substantial authoritative body to have something to lose)."""
        task = _make_task(content='', thinking='t' * 300)
        asst = {'role': 'assistant', 'content': 'short answer'}
        reasons = self._reasons(task, asst)
        assert not any(r.startswith('content_track_divergence') for r in reasons), reasons

    def test_guard_emits_audit_on_divergence(self, monkeypatch):
        """The divergence path must emit a grep-able audit event (not just a log)."""
        import lib.log as _log
        captured = []
        monkeypatch.setattr(_log, 'audit_log',
                            lambda ev, **kw: captured.append((ev, kw)))
        task = _make_task(content='X' * 215)
        asst = {'role': 'assistant', 'content': 'Y' * 3411}
        self._reasons(task, asst)
        assert any(ev == 'content_track_divergence' for ev, _ in captured), captured


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
