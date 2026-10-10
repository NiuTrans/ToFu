"""Regression tests for the entropy-aware heuristic token estimator.

Pins the char-class model in ``lib/token_counter/heuristic.py``:

  * CJK chars             → 1 token / char
  * dense base64/hex runs → 1 token / char (high-entropy, BPE ceiling)
  * repeated/low-entropy  → stays on the /3 prose path (BPE merges repeats)
  * prose / code          → ~1 token / 3 chars

The dense-run rule fixes the conv=mq7y3irly1r4hu failure: a transcript
full of base64-encoded files estimated at ~613K (heuristic) while the
gateway tokenizer saw ~1.19M — a 1.85x under-count that let the prompt
sail past the proactive compaction trigger straight into the fatal
reactive path.
"""
from __future__ import annotations

import base64
import os

import pytest

from lib.token_counter.heuristic import cheap_estimate_text as f


@pytest.mark.unit
def test_empty():
    assert f('') == 0


@pytest.mark.unit
def test_prose_unchanged_at_third():
    # Plain English stays on the /3 path (whitespace breaks any dense run).
    prose = 'The quick brown fox jumps over the lazy dog. ' * 100
    ratio = len(prose) / f(prose)
    assert 2.7 <= ratio <= 3.3, ratio


@pytest.mark.unit
def test_code_unchanged_at_third():
    code = 'def foo(x):\n    return x + 1\n' * 50
    ratio = len(code) / f(code)
    assert 2.7 <= ratio <= 3.3, ratio


@pytest.mark.unit
def test_cjk_one_token_per_char():
    cjk = '你好世界' * 100
    assert f(cjk) == len(cjk)


@pytest.mark.unit
def test_real_base64_charged_at_one_per_char():
    # High-entropy base64 → 1 token / char (the dense rate).
    b64 = base64.b64encode(os.urandom(3000)).decode()
    assert f(b64) == len(b64), (f(b64), len(b64))


@pytest.mark.unit
def test_repeated_char_run_is_not_dense():
    # A long run of ONE char matches the regex but is low-entropy; real BPE
    # merges it heavily, so it must stay on the /3 path — NOT 1/char.
    # (This is the test_opencode_prune_skips_below_20k_minimum invariant.)
    blob = 'x' * 12_000
    est = f(blob)
    assert est < 5_000, est
    assert abs(est - 12_000 // 3) <= 2, est


@pytest.mark.unit
def test_hex_run_is_dense():
    # 16 distinct hex chars meets the diversity floor → dense rate.
    hexrun = ('0123456789abcdef' * 10)  # 160 chars, 16 distinct
    assert f(hexrun) == len(hexrun)


@pytest.mark.unit
def test_dense_run_never_undercounts_vs_prose_model():
    # The dense path must always estimate >= the old flat /3 model would,
    # for the same text (safe direction for a compaction trigger).
    b64 = base64.b64encode(os.urandom(5000)).decode()
    old_model = len(b64) // 3 + 1
    assert f(b64) >= old_model


@pytest.mark.unit
def test_mixed_prose_and_base64():
    # Embedded blob counted dense; surrounding prose counted /3.
    blob = base64.b64encode(os.urandom(900)).decode()
    text = 'Here is the file content below:\n' + blob + '\nEnd of file.'
    # Lower bound: at least the blob length (dense) plus a bit for prose.
    assert f(text) >= len(blob)
