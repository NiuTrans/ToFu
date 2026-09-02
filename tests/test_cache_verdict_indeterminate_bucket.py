"""A zero-read rebuild must not be filed alongside genuine cache hits.

WHY THIS EXISTS
===============
``no_break`` used to mean two different things: "the cache was reused fine"
AND "the detector reached no conclusion". They are not the same claim, and
conflating them let real spend hide behind a healthy-looking bucket.

MEASURED MECHANISM: all three predicates in ``_classify_break`` carry
``and not was_compaction``, so a COMPACTED round with a large zero-read write
is structurally exempt from every gate and falls through to the no-break path.
The exemption is correct for the detector's RETURN value — a compaction
legitimately rebuilds its prefix and must not be blamed on the gateway — but
wrong for the LEDGER, where those tokens were really paid for.

NOT THE MECHANISM (asserted false below, so it cannot be re-adopted): an
earlier version of this fix claimed a zero-read PREDECESSOR starved the gates,
making a run of consecutive misses collapse to one counted miss.
``prev_prefix_tokens`` is prev_read + prev_write, so a miss predecessor with a
large write still clears ``no_reuse``'s threshold — verified directly against
``_classify_break``, which returns ``no_reuse=True`` on exactly that shape.

The fix is at the LANDING SITE (how an unconcluded round is NAMED), NOT in the
detection thresholds. Loosening a gate would make a genuinely cold or
legitimately-rebuilt prefix report as a miss — a false positive traded for a
false negative.

GUARDS
  * A compacted zero-read rebuild is bucketed ``indeterminate`` and counted.
  * COMPLEMENT — a genuine cache HIT is never dragged in by the new rule.
  * COMPLEMENT — a first round (no predecessor) stays a cold start, not an
    unknown: it had nothing to read back.
  * COMPLEMENT — a small write is not an indeterminate miss.
  * The predicate is evaluated by IMPORTING the production terms, never by a
    hand-copied mirror — a mirror drifts silently and passes anyway.
  * The detector's RETURN value is unchanged (``None``). Only the recorded
    bucket moves — telemetry changes, product behaviour does not.
"""

from __future__ import annotations

import pytest

from lib.tasks_pkg.cache_tracking._detect import (
    BUCKET_INDETERMINATE,
    BUCKET_NO_BREAK,
    BUCKET_UPSTREAM,
    classify_verdict,
)

pytestmark = pytest.mark.unit


class TestIndeterminateIsItsOwnBucket:
    def test_indeterminate_verdict_maps_to_its_own_bucket(self):
        verdict = {'indeterminate': (
            'zero read-back on a substantial write, but no break gate could '
            'fire: the previous round was itself a zero-read miss')}
        assert classify_verdict(verdict) == BUCKET_INDETERMINATE

    def test_indeterminate_is_not_no_break(self):
        """The load-bearing distinction. 'We could not tell' must never render
        as 'the cache was reused fine' — that is what made a run of misses
        look like a single miss."""
        verdict = {'indeterminate': 'no gate could fire'}
        assert classify_verdict(verdict) != BUCKET_NO_BREAK

    def test_an_empty_verdict_is_still_no_break(self):
        """COMPLEMENT — a genuinely unremarkable round keeps its old bucket.
        Without this, 'everything is indeterminate' would pass the test above."""
        assert classify_verdict(None) == BUCKET_NO_BREAK
        assert classify_verdict({}) == BUCKET_NO_BREAK

    def test_a_real_upstream_miss_is_untouched(self):
        """COMPLEMENT — the bucket carrying ~84% of recoverable spend must not
        be absorbed by the new rule."""
        verdict = {'server_side': (
            'prefix not read back though the wire bytes were byte-identical '
            'to the previous round. The whole cached prefix was not reused '
            'upstream: most likely an upstream cache miss')}
        assert classify_verdict(verdict) == BUCKET_UPSTREAM


class TestGateStarvedPredicate:
    """The landing-site condition, asserted against the REAL suppressor.

    Measured mechanism: all three predicates in ``_classify_break`` carry
    ``and not was_compaction``, so a compacted round with a large zero-read
    write is exempt from every gate and falls through to the no-break path.

    NOT the mechanism (checked and disproved): a zero-read predecessor starving
    the gates. ``prev_prefix_tokens`` is prev_read + prev_write, so a miss
    predecessor with a large write still clears no_reuse's threshold —
    ``test_a_miss_predecessor_does_NOT_starve_the_gates`` pins that against the
    real function so the wrong story cannot be re-adopted.
    """

    @staticmethod
    def _starved(*, call_count, cache_read, cache_write):
        """Evaluate the landing-site predicate using the PRODUCTION threshold.

        The shape is mirrored here (the predicate lives inline inside
        ``detect_cache_break`` and is not separately importable), but the
        threshold is imported so a change to ``_MIN_NO_REUSE_TOKENS`` cannot
        leave this guard asserting against a stale number. The end-to-end
        class below drives the real function, so the mirror is a convenience
        for the boundary cases, never the only evidence.
        """
        from lib.tasks_pkg.cache_tracking._detect import _MIN_NO_REUSE_TOKENS
        return (call_count > 1
                and cache_read == 0
                and cache_write >= _MIN_NO_REUSE_TOKENS)

    def test_a_miss_predecessor_does_NOT_starve_the_gates(self):
        """Pins the disproved theory against the real classifier."""
        from lib.tasks_pkg.cache_tracking._detect import _classify_break
        _, no_reuse, _ = _classify_break(
            call_count=5, was_compaction=False,
            prev_cache_read=0, cache_read=0,
            prev_cache_write=588289, cache_write=497675,
            prev_prefix_tokens=588289)
        assert no_reuse is True, (
            'a zero-read predecessor does NOT starve no_reuse — '
            'prev_prefix_tokens is read+write')

    def test_compaction_is_exempt_from_every_gate(self):
        """The real suppressor, asserted against the real classifier."""
        from lib.tasks_pkg.cache_tracking._detect import _classify_break
        gates = _classify_break(
            call_count=5, was_compaction=True,
            prev_cache_read=0, cache_read=0,
            prev_cache_write=588289, cache_write=497675,
            prev_prefix_tokens=588289)
        assert gates == (False, False, False), (
            'compaction exempts a round from every break gate — that is why it '
            f'lands on the no-break path. got={gates}')

    def test_a_large_zero_read_write_is_indeterminate(self):
        assert self._starved(call_count=7, cache_read=0,
                             cache_write=497675) is True

    def test_a_cache_hit_is_not_indeterminate(self):
        """COMPLEMENT — a round that read back is never indeterminate."""
        assert self._starved(call_count=7, cache_read=226362,
                             cache_write=1134) is False

    def test_first_round_is_not_indeterminate(self):
        """COMPLEMENT — call_count is already incremented, so round 1 reads as
        1 here; a > 0 guard would mislabel every cold start."""
        assert self._starved(call_count=1, cache_read=0,
                             cache_write=122838) is False

    def test_a_small_write_is_not_indeterminate(self):
        """COMPLEMENT — trivial writes are not miss-sized spend."""
        assert self._starved(call_count=7, cache_read=0,
                             cache_write=500) is False


class TestRecordedBucketIsDecoupledFromReturnValue:
    """Drives the REAL detect_cache_break, not a local stub.

    An earlier version of this class re-implemented ``_finish`` inline and
    asserted against the copy — which passed even when the production helper
    was neutered, and would have shipped a NameError in the real predicate. A
    guard that cannot see the code it guards is not a guard.
    """

    @staticmethod
    def _drive(monkeypatch, rounds, compact_before=()):
        """Feed rounds through the real detector; return the emitted buckets.

        Each round is ``(cache_read, cache_write)``. ``compact_before`` lists
        1-based round numbers to call the real ``notify_compaction`` before —
        that is the ONLY way to reach the indeterminate branch, because
        compaction is what exempts a round from every break gate.
        """
        import lib.tasks_pkg.cache_tracking._detect as det
        from lib.tasks_pkg.cache_tracking._roi import notify_compaction

        seen = []
        real_emit = det._emit_round_record

        def _spy(conv_id, call_num, verdict, **kw):
            seen.append(det.classify_verdict(verdict))
            return real_emit(conv_id, call_num, verdict, **kw)

        monkeypatch.setattr(det, '_emit_round_record', _spy)

        conv = f'conv-indet-{id(rounds)}'
        for i, (read, write) in enumerate(rounds, start=1):
            if i in compact_before:
                notify_compaction(conv, user_id=1)
            usage = {'prompt_tokens': 1000, 'completion_tokens': 10,
                     'cache_read_input_tokens': read,
                     'cache_creation_input_tokens': write}
            det.detect_cache_break(
                conv, [{'role': 'user', 'content': 'x'}],
                None, 'claude-opus-5', usage, user_id=1)
        return seen

    def test_a_compacted_zero_read_rebuild_is_counted_not_filed_as_no_break(
            self, monkeypatch):
        """The end-to-end property this fix exists for.

        A compacted round is exempt from every break gate, so it used to land
        in ``no_break`` — filed alongside genuine cache hits despite having
        paid to rebuild a 400k-token prefix. It must now be visible as
        indeterminate: the spend is real, the cause is unresolved.
        """
        buckets = self._drive(monkeypatch, [
            (0, 200000),        # round 1 — cold start
            (300000, 5000),     # round 2 — healthy reuse
            (0, 400000),        # round 3 — compacted zero-read rebuild
        ], compact_before=(3,))
        assert buckets[2] == BUCKET_INDETERMINATE, (
            'a compacted zero-read rebuild must be counted, not filed as '
            f'no_break. buckets={buckets}')

    def test_a_healthy_reuse_run_is_never_indeterminate(self, monkeypatch):
        """COMPLEMENT — rounds that read back are untouched by the new rule."""
        buckets = self._drive(monkeypatch, [
            (0, 200000),
            (200000, 5000),
            (205000, 4000),
        ])
        assert BUCKET_INDETERMINATE not in buckets[1:], (
            f'cache hits must never be indeterminate. buckets={buckets}')

    def test_round_one_is_a_cold_start_not_an_unknown(self, monkeypatch):
        """COMPLEMENT — and a regression guard for a real off-by-one.

        ``prev.call_count`` is already incremented for the current round by the
        time the landing-site predicate runs, so a ``> 0`` guard lets round 1
        through and every cold start gets mislabelled as indeterminate. The
        first round has no predecessor and nothing to read back.
        """
        buckets = self._drive(monkeypatch, [(0, 200000)])
        assert buckets[0] != BUCKET_INDETERMINATE, (
            'round 1 has no predecessor — it is a cold start, not an unknown. '
            f'buckets={buckets}')
