"""Per-round cache records must carry the model, and costing must be per-row.

WHY THIS EXISTS
===============
``_emit_round_record`` wrote 11 keys and none of them was ``model`` — while
``detect_cache_break(conv_id, messages, tools, model, usage)`` had the value in
scope the whole time. One missing field, but the consequence was that EVERY
cost aggregation over these records had to price the whole table at a single
rate.

That is not a cosmetic gap. Measured fleet mix (model mentions in app log):
``aws.claude-opus-4.8`` 44.7 %, ``kimi-k3`` 11.4 %, ``claude-opus-5`` 5.9 %,
``yuju-claude-opus-5-evaDaily`` 5.8 %, ``gemini-3-flash-preview`` 4.5 %, long
tail after that. The rates span two orders of magnitude — Opus family 0.04525,
``kimi-k3`` 0.01998, ``gemini-3-flash-preview`` 0.00109 CNY/1k cache-write. So
a single-rate table is an approximation whose error nobody could bound, and it
forced a paragraph of caveat onto every outbound cost figure.

GUARDS
  * A new record carries ``model``, equal to what was passed to
    ``detect_cache_break`` — asserted by driving the REAL detector, not by
    hand-feeding ``_emit_round_record``.
  * HISTORICAL rows have no ``model`` (buckets and fields are stamped at write
    time, so old lines never gain one). They must still aggregate, falling back
    to ``--model``; they must NOT raise and must NOT be silently dropped.
  * Per-row pricing is real: a mixed fixture (one Opus row + one Gemini row)
    must total the sum of each row's OWN rate, not either single rate applied
    to both. This is the assertion that fails if the report only *looks* like
    it prices per row.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

pytestmark = pytest.mark.unit

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(_HERE, '..', 'scripts', 'cache_waste_report.py')


def _load_report_mod():
    spec = importlib.util.spec_from_file_location('cache_waste_report', _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cwr = _load_report_mod()

_OPUS = 'claude-opus-5'
_CHEAP = 'gemini-3-flash-preview'


class TestTheRecordCarriesTheModel:
    """Driven through the real detector — a hand-fed _emit_round_record would
    prove nothing about whether production actually passes the value."""

    @staticmethod
    def _emitted(monkeypatch, model):
        from lib.tasks_pkg.cache_tracking import _detect as det

        seen = []
        real = det._emit_round_record

        def _spy(conv_id, call_num, verdict, **kw):
            out = real(conv_id, call_num, verdict, **kw)
            seen.append(kw)
            return out

        monkeypatch.setattr(det, '_emit_round_record', _spy)

        # Capture the JSON the emitter actually serialises.
        payloads = []
        real_logger_info = det.logger.info

        def _cap(fmt, *args):
            if args and isinstance(args[0], str) and args[0].startswith('{'):
                import json as _json
                try:
                    payloads.append(_json.loads(args[0]))
                except ValueError:
                    pass
            return real_logger_info(fmt, *args)

        monkeypatch.setattr(det.logger, 'info', _cap)

        conv = f'conv-model-field-{model}'
        for read, write in [(0, 200000), (300000, 5000)]:
            det.detect_cache_break(
                conv, [{'role': 'user', 'content': 'x'}], None, model,
                {'prompt_tokens': 1000, 'completion_tokens': 10,
                 'cache_read_input_tokens': read,
                 'cache_creation_input_tokens': write})
        return payloads

    def test_record_includes_the_model(self, monkeypatch):
        payloads = self._emitted(monkeypatch, _OPUS)
        assert payloads, 'no [CacheRoundRecord] payload was emitted at all'
        assert all('model' in p for p in payloads), (
            f'emitted record has no model key: {sorted(payloads[0].keys())}')

    def test_the_model_value_is_the_one_passed_in(self, monkeypatch):
        payloads = self._emitted(monkeypatch, _OPUS)
        assert {p['model'] for p in payloads} == {_OPUS}

    def test_a_different_model_is_recorded_differently(self, monkeypatch):
        """COMPLEMENT — a hardcoded constant would pass the test above."""
        payloads = self._emitted(monkeypatch, _CHEAP)
        assert {p['model'] for p in payloads} == {_CHEAP}


class TestPerRowPricing:
    @staticmethod
    def _rec(model, write, *, call=9, gap=38.0):
        r = {'bucket': 'upstream_identical', 'call': call, 'gap_s': gap,
             'cache_write': write, 'cache_read': 0}
        if model is not None:
            r['model'] = model
        return r

    def test_a_row_is_priced_by_its_own_model(self):
        """The load-bearing assertion. One Opus row + one Gemini row of equal
        size must total the sum of their OWN rates — not either rate applied to
        both. Opus is ~41x Gemini per cache-write token, so a single-rate
        implementation cannot accidentally satisfy this."""
        w_opus, r_opus = cwr.derive_rates(_OPUS)
        w_cheap, r_cheap = cwr.derive_rates(_CHEAP)
        assert w_opus > w_cheap * 10, 'fixture assumes a wide rate gap'

        tok = 1_000_000
        rep = cwr.build_report(
            [self._rec(_OPUS, tok), self._rec(_CHEAP, tok)],
            min_write=20000, w_rate=w_opus, r_rate=r_opus, model_id=_OPUS)

        expected = tok * (w_opus - r_opus) + tok * (w_cheap - r_cheap)
        assert rep['true_recoverable_cny'] == pytest.approx(expected, rel=1e-6), (
            'the mixed fixture was not priced per row; got '
            f"{rep['true_recoverable_cny']:.4f}, expected {expected:.4f} "
            f"(single-rate would give {2 * tok * (w_opus - r_opus):.4f})")

    def test_a_row_without_model_falls_back_to_the_cli_model(self):
        """Historical rows never gain a model. They must still be priced."""
        w, r = cwr.derive_rates(_OPUS)
        tok = 1_000_000
        rep = cwr.build_report(
            [self._rec(None, tok)],
            min_write=20000, w_rate=w, r_rate=r, model_id=_OPUS)
        assert rep['true_recoverable_cny'] == pytest.approx(
            tok * (w - r), rel=1e-6)

    def test_a_row_without_model_is_not_dropped(self):
        """COMPLEMENT — 'skip rows we cannot price' would silently shrink every
        historical total. The round must still be counted."""
        w, r = cwr.derive_rates(_OPUS)
        rep = cwr.build_report(
            [self._rec(None, 1_000_000)],
            min_write=20000, w_rate=w, r_rate=r, model_id=_OPUS)
        assert rep['zero_readback_rounds'] == 1
        assert rep['rows'][0]['n'] == 1

    def test_mixed_old_and_new_rows_both_counted(self):
        w, r = cwr.derive_rates(_OPUS)
        rep = cwr.build_report(
            [self._rec(None, 1_000_000), self._rec(_CHEAP, 1_000_000)],
            min_write=20000, w_rate=w, r_rate=r, model_id=_OPUS)
        assert rep['zero_readback_rounds'] == 2

    def test_an_unknown_model_on_a_row_falls_back_rather_than_crashing(self):
        """A record could carry a model that is not in the pricing table (new
        model, gateway alias). That must degrade to the CLI rate, not raise —
        this script is run on historical logs and must never die on one row."""
        w, r = cwr.derive_rates(_OPUS)
        rep = cwr.build_report(
            [self._rec('totally-unknown-model-xyz', 1_000_000)],
            min_write=20000, w_rate=w, r_rate=r, model_id=_OPUS)
        assert rep['zero_readback_rounds'] == 1
        assert rep['true_recoverable_cny'] > 0


class TestPerModelBreakdown:
    """The payoff the ticket names: 'which model wastes the most cache' was
    previously unanswerable."""

    def test_report_exposes_a_per_model_split(self):
        w, r = cwr.derive_rates(_OPUS)
        rep = cwr.build_report(
            [TestPerRowPricing._rec(_OPUS, 1_000_000),
             TestPerRowPricing._rec(_OPUS, 500_000),
             TestPerRowPricing._rec(_CHEAP, 2_000_000)],
            min_write=20000, w_rate=w, r_rate=r, model_id=_OPUS)
        by_model = rep.get('by_model')
        assert by_model, 'no per-model breakdown in the report payload'
        got = {m['model']: m for m in by_model}
        assert got[_OPUS]['n'] == 2
        assert got[_OPUS]['wasted_tokens'] == 1_500_000
        assert got[_CHEAP]['n'] == 1
        # Opus wastes fewer tokens here but costs far more — the split must
        # rank by money, which is the question people actually ask.
        assert got[_OPUS]['recoverable_cny'] > got[_CHEAP]['recoverable_cny']
