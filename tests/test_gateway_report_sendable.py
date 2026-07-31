"""The outbound gateway report must contain nothing internal-only.

WHY THIS EXISTS
===============
The report at ``docs/GATEWAY_REPORT_CACHE_NOT_REUSED.md`` is written to be
FORWARDED to the `sankuai_anthropic` gateway team — people at another org.

Its review notes originally lived at the bottom of that same file under a
heading reading "REMOVE THIS SECTION BEFORE SENDING". That made correctness
depend on a human remembering to delete a section from a document they were in
the middle of forwarding, and the content was exactly what we would least want
to hand the other side:

  * our own enumeration of the argument's weak points,
  * the admission that the CNY column is an upper bound,
  * the admission that we lack the decisive second-path control,
  * internal ticket ids and tone-check instructions.

"Remember to delete this" is not a control. The notes now live in
``docs/internal/GATEWAY_REPORT_REVIEW_NOTES.md`` and this guard asserts they
cannot drift back — so the leak is structurally impossible rather than
procedurally avoided.

GUARDS
  * The sendable report exists and contains no internal-only marker.
  * The internal companion exists, is marked internal, and still carries the
    substance (so "fix the guard by deleting the notes" is not a way out).
  * COMPLEMENT: the sendable report still contains the things that make it
    worth sending — trace ids, the ruled-out table, the reproduce command.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, '..'))
_SENDABLE = os.path.join(_ROOT, 'docs', 'GATEWAY_REPORT_CACHE_NOT_REUSED.md')
_INTERNAL = os.path.join(
    _ROOT, 'docs', 'internal', 'GATEWAY_REPORT_REVIEW_NOTES.md')

# Phrases that betray internal-only content. Matched case-insensitively.
# Deliberately includes the old heading verbatim: that exact string is what a
# re-merge would reintroduce.
_INTERNAL_MARKERS = (
    'remove this section before sending',
    'internal review notes',
    'internal only',
    'never forward',
    'attack these first',
    'tone check',
)


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class TestSendableReportIsClean:
    def test_the_report_exists(self):
        assert os.path.isfile(_SENDABLE), (
            'the outbound report is missing — the gateway epic has nothing to '
            'send')

    @pytest.mark.parametrize('marker', _INTERNAL_MARKERS)
    def test_no_internal_marker_in_the_sendable_report(self, marker):
        body = _read(_SENDABLE).lower()
        assert marker not in body, (
            f'the outbound report contains the internal-only marker '
            f'{marker!r}. This file gets FORWARDED to another org — internal '
            f'review material belongs in docs/internal/, not behind a '
            f'"remember to delete me" heading.')

    def test_no_internal_ticket_ids_leak(self):
        """Our board ids mean nothing to them and reveal our internal tracking."""
        body = _read(_SENDABLE)
        assert 'pt_' not in body, (
            'an internal board ticket id leaked into the outbound report')


class TestSendableReportStillCarriesItsSubstance:
    """COMPLEMENT — the cheap way to satisfy the class above is to gut the
    report. These assert it still says what it needs to say."""

    def test_has_trace_ids(self):
        body = _read(_SENDABLE)
        assert 'M-TraceId' in body, 'the per-request receipts are gone'
        # 11 worked-example trace ids, each a 32-char hex string in backticks.
        import re
        ids = re.findall(r'`[0-9a-f]{32}`', body)
        assert len(ids) >= 11, f'expected >=11 trace ids, found {len(ids)}'

    def test_has_the_ruled_out_table(self):
        body = _read(_SENDABLE).lower()
        assert 'what we ruled out on our side first' in body

    def test_has_the_reproduce_command(self):
        assert 'cache_waste_report.py' in _read(_SENDABLE)

    def test_has_the_interleaved_control(self):
        """The R18/R19 pair is the strongest single fact; it must survive."""
        body = _read(_SENDABLE)
        assert '100.7' in body and '14.8' in body, (
            'the hit/miss interleaving evidence is missing')


class TestInternalCompanionExists:
    def test_the_companion_exists(self):
        assert os.path.isfile(_INTERNAL), (
            'the review notes were removed from the report but not filed in '
            'docs/internal/ — the review substance was lost, not relocated')

    def test_the_companion_is_marked_internal(self):
        body = _read(_INTERNAL).lower()
        assert 'internal only' in body and 'never forward' in body

    def test_the_companion_still_carries_the_review_substance(self):
        """Guard against 'delete the notes' as a way to make the suite green."""
        body = _read(_INTERNAL).lower()
        for needle in ('cny', 'ttl_expiry', 'second', 'r18'):
            assert needle in body, (
                f'the internal companion no longer discusses {needle!r} — the '
                'review checklist has been hollowed out')


class TestTheDeferralSurvives:
    """The owner deferred sending: 暂缓，等新流量积累 (hold, accumulate traffic).

    A decision that lives only in a chat log is a decision that gets re-made by
    the next person to open the file. These assert the deferral, its reason,
    and — critically — its PREREQUISITE stay written down next to the checklist
    they gate.

    The prerequisite is the part that is easy to lose and expensive to lose:
    "wait for new traffic" reads as self-executing, but it is not. The serving
    process predates the ``model`` fix, so new rounds keep landing WITHOUT the
    field (measured: 252 post-fix rounds, 0 with a model). Waiting alone
    therefore accumulates volume without the exactness the wait exists to buy,
    and whoever resumes would re-run into the identical caveat with a larger n
    and no idea why.
    """

    def test_the_report_is_marked_deferred(self):
        body = _read(_INTERNAL).lower()
        assert 'deferred' in body and 'do not send yet' in body, (
            'the deferral decision vanished from the internal notes — the next '
            'reader will see a finished report plus a sending checklist and '
            'reasonably conclude it is ready to forward')

    def test_the_restart_prerequisite_is_recorded(self):
        """The non-obvious half: waiting is necessary but NOT sufficient."""
        body = _read(_INTERNAL).lower()
        assert 'restart' in body, (
            'the server-restart prerequisite is gone. Without it "wait for new '
            'traffic" silently accumulates rows that still lack the model '
            'field, and the re-run reproduces the same caveat at a bigger n')

    def test_the_sending_checklist_is_gated_on_the_deferral(self):
        """The two sections must not contradict each other."""
        body = _read(_INTERNAL).lower()
        i = body.find('## sending checklist')
        assert i != -1, 'the sending checklist heading moved or was removed'
        assert 'gated' in body[i:i + 400], (
            'the sending checklist no longer states it is gated, so it reads '
            'as the immediate next action despite the deferral above it')

    def test_the_deferral_did_not_leak_into_the_sendable_report(self):
        """COMPLEMENT: our internal scheduling is not their business."""
        body = _read(_SENDABLE).lower()
        for needle in ('deferred', 'do not send yet', '暂缓'):
            assert needle not in body, (
                f'internal scheduling state {needle!r} leaked into the file we '
                f'forward to another org')
