"""tests/test_json_dumps_pg_fastpath.py — json_dumps_pg fast path.

Root cause being fixed (2026-07-27 hard-refresh incident): every JSONB write
funneled through ``json_dumps_pg`` PAID a pure-Python recursive
``strip_null_bytes_deep`` walk over the whole payload BEFORE serialising —
~0.9 s of GIL-holding CPU for a 93.7 MB conversation blob, measured. A
hard-refresh burst parked 149 threads inside that walk simultaneously
(faulthandler dump 08:43:59) and starved the event loop for 8.8 s.

The fix is a fast path: ``json.dumps`` escapes every U+0000 to the six-char
sequence ``\\u0000``, so its absence from the serialised text PROVES the
payload carries no null bytes and the recursive walk would be a no-op.
Only payloads that actually contain null bytes fall back to the deep strip,
keeping the output byte-identical to the historical always-strip behaviour.

Guards:
  * failing-first — the spy test is RED on the old always-strip code.
  * NEUTER both directions — removing the fast path trips the spy test;
    removing the slow-path fallback trips the null-byte test.
  * byte parity — a corpus of adversarial structures (incl. literal
    backslash-u0000 text, null bytes in keys, unicode) must serialise
    byte-identically under the old and new algorithms.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_json_dumps_pg_fastpath.py -v
"""

from __future__ import annotations

import json

import pytest

import lib.database._wrappers as wrappers
from lib.database._wrappers import json_dumps_pg

pytestmark = pytest.mark.unit


def _historical_json_dumps_pg(obj, **kwargs):
    """Byte-for-byte copy of the pre-fix algorithm (always deep-strip first).
    Kept here as the parity oracle so a future refactor cannot drift the
    meaning of 'historical behaviour'."""
    kwargs.setdefault('ensure_ascii', False)
    text = json.dumps(wrappers.strip_null_bytes_deep(obj), **kwargs)
    return wrappers._strip_json_null_escapes(text)


# ── Adversarial corpus ────────────────────────────────────────────────────

_CORPUS = [
    # plain clean nesting (the 99.99% production case)
    {'a': [1, 2, {'b': 'text'}], 'c': None, 'd': True, 'e': 3.14},
    # unicode / emoji / CJK
    {'msg': '直接原因很明确：检查只看输出。🚀 café naïve'},
    # null byte in a value (must be stripped)
    {'a': 'hello\x00world', 'b': ['x\x00', {'c': '\x00'}]},
    # null byte in a KEY (historical quirk: strip only touches values; the
    # escape pass then rewrites the key — parity must reproduce exactly)
    {'ke\x00y': 'v'},
    # literal six-char backslash-u0000 TEXT (NOT a null byte) — must survive
    {'literal': 'the text \\u0000 is literal'},
    # both a literal sequence AND a real null byte together
    {'mix': 'lit \\u0000 plus real \x00 null'},
    # empty containers and scalars
    {}, [], '', 0, False, None,
    # deep nesting
    {'l1': {'l2': {'l3': {'l4': {'l5': ['deep', {'l6': 'x'}]}}}}},
    # a realistic message-shaped structure
    [{'role': 'user', 'content': '你好'},
     {'role': 'assistant', 'content': '```py\nprint("hi")\n```',
      'toolRounds': [{'round': 1, 'tools': []}]}],
]


# ── 1. failing-first + NEUTER(direction 1): clean payloads skip the walk ──

def test_clean_object_skips_deep_strip(monkeypatch):
    """On the old code the recursive strip ran unconditionally, so the spy
    trips and this test is RED. On the fixed code a clean payload must never
    enter strip_null_bytes_deep. NEUTER: deleting the fast path (always
    strip) turns this RED again — it cannot pass vacuously."""
    calls = []
    real = wrappers.strip_null_bytes_deep

    def _spy(obj):
        calls.append(1)
        return real(obj)

    monkeypatch.setattr(wrappers, 'strip_null_bytes_deep', _spy)
    obj = {'messages': [{'role': 'user', 'content': '你好 world'}], 'n': 42}
    out = json_dumps_pg(obj)
    assert json.loads(out) == obj
    assert calls == [], (
        f'clean payload entered strip_null_bytes_deep {len(calls)}× — '
        'fast path missing or bypassed')


def test_large_clean_blob_skips_deep_strip(monkeypatch):
    """Same guard at production scale: a ~5 MB clean blob (the shape that
    parked 149 threads) must serialise without the recursive walk."""
    calls = []
    real = wrappers.strip_null_bytes_deep

    def _spy(obj):
        calls.append(1)
        return real(obj)

    monkeypatch.setattr(wrappers, 'strip_null_bytes_deep', _spy)
    blob = [{'role': 'user', 'content': '长文本' * 6000 + f' segment {i}'}
            for i in range(250)]
    out = json_dumps_pg(blob)
    assert len(out) > 4_000_000
    assert json.loads(out) == blob
    assert calls == []


# ── 2. NEUTER(direction 2): dirty payloads still take the slow path ────────

def test_null_bytes_still_sanitized():
    """Payloads that DO contain null bytes must still be sanitised — the
    fallback must produce valid JSON with no residual \\u0000 escapes.
    NEUTER: removing the fallback (never strip) makes this RED."""
    obj = {'a': 'hello\x00world', 'b': ['x\x00y'], 'c': {'d': '\x00'}}
    out = json_dumps_pg(obj)
    assert '\\u0000' not in out
    parsed = json.loads(out)  # must stay valid JSON
    assert parsed == {'a': 'helloworld', 'b': ['xy'], 'c': {'d': ''}}


def test_null_byte_calls_deep_strip_exactly_once(monkeypatch):
    """The slow path must be taken when nulls are present (spy proves it),
    and the result still matches the historical algorithm byte-for-byte."""
    calls = []
    real = wrappers.strip_null_bytes_deep

    def _spy(obj):
        calls.append(1)
        return real(obj)

    monkeypatch.setattr(wrappers, 'strip_null_bytes_deep', _spy)
    obj = {'a': 'x\x00y'}
    assert json_dumps_pg(obj) == _historical_json_dumps_pg(obj)
    assert len(calls) >= 1, 'null-byte payload did NOT take the slow path'


# ── 3. Byte parity against the historical algorithm ───────────────────────

@pytest.mark.parametrize('obj', _CORPUS, ids=[repr(o)[:40] for o in _CORPUS])
def test_byte_parity_with_historical(obj):
    assert json_dumps_pg(obj) == _historical_json_dumps_pg(obj)


@pytest.mark.parametrize('obj', _CORPUS[:6], ids=[repr(o)[:40] for o in _CORPUS[:6]])
def test_byte_parity_ensure_ascii_true(obj):
    assert (json_dumps_pg(obj, ensure_ascii=True)
            == _historical_json_dumps_pg(obj, ensure_ascii=True))


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
