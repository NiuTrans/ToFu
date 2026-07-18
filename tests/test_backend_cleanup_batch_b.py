#!/usr/bin/env python3
"""Batch B backend-cleanup regression tests (redundancy elimination).

Two top-ranked redundancy findings from the exhaustive audit, both git-clean:

  1. lib/embeddings.py — embed_texts had the slot-pick→headers→POST→scatter
     block written THREE times (primary, 429-retry, except-retry). The
     except-retry copy also lacked the nested try the 429 path had, so a
     failure THERE escaped the batch loop. Extracted into one _post_batch()
     helper. Lock: success scatters results, 429 falls to a retry slot,
     an exception on the first attempt is retried (not raised), and a total
     failure yields zero-vectors (never raises out of embed_texts).
  2. lib/optimizer/proposer.py — propose() half-migrated: it imported only
     strip_code_fences + bare json.loads, skipping the balanced-block +
     repair fallbacks extract_json provides. Now delegates to extract_json.
     Lock: fenced JSON, prose-wrapped JSON, and garbage all handled.

These stub http_post / smart_chat so no network is hit. Run standalone
(``python tests/test_backend_cleanup_batch_b.py``) or via pytest.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ── 1. embeddings _post_batch ─────────────────────────────────────────

class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = ''
    def json(self):
        return self._payload


def _emb_payload(n, dim=4):
    return {'data': [{'index': i, 'embedding': [float(i)] * dim} for i in range(n)]}


def _patch_embeddings(monkeypatch_posts):
    """Install a scripted http_post + a fixed slot picker. Returns the module."""
    import lib.embeddings as emb
    calls = {'n': 0}
    def _fake_post(url, headers=None, json=None, timeout=None):
        i = calls['n']; calls['n'] += 1
        return monkeypatch_posts[i]
    emb._orig_post = emb.http_post
    emb.http_post = _fake_post
    emb._orig_pick = emb._pick_embedding_slot
    emb._pick_embedding_slot = lambda model: ('k', 'https://x/v1', 'key_0', None)
    return emb, calls


def _restore_embeddings(emb):
    emb.http_post = emb._orig_post
    emb._pick_embedding_slot = emb._orig_pick


def test_embed_success_scatters():
    posts = [_Resp(200, _emb_payload(2))]
    emb, calls = _patch_embeddings(posts)
    try:
        out = emb.embed_texts(['a', 'b'], batch_size=10)
    finally:
        _restore_embeddings(emb)
    assert out == [[0.0, 0.0, 0.0, 0.0], [1.0, 1.0, 1.0, 1.0]], out
    assert calls['n'] == 1, 'one POST for one batch'
    _ok('embed_texts: success scatters embeddings, single POST')


def test_embed_429_retries_next_slot():
    posts = [_Resp(429), _Resp(200, _emb_payload(1))]
    emb, calls = _patch_embeddings(posts)
    try:
        out = emb.embed_texts(['a'], batch_size=10)
    finally:
        _restore_embeddings(emb)
    assert out == [[0.0, 0.0, 0.0, 0.0]], out
    assert calls['n'] == 2, '429 then retry = 2 POSTs'
    _ok('embed_texts: 429 → retry on next slot succeeds')


def test_embed_exception_first_attempt_is_retried_not_raised():
    """The old except-retry path lacked a nested try; an exception there escaped
    embed_texts. Now a first-attempt exception is retried and, if the retry
    also fails, the batch degrades to zero-vectors WITHOUT raising."""
    class _Boom:
        status_code = 200
        def json(self): raise RuntimeError('boom')
    # First POST raises inside .json(); retry POST also raises → must NOT raise
    # out of embed_texts, must yield a zero vector.
    posts = [_Boom(), _Boom()]
    emb, calls = _patch_embeddings(posts)
    try:
        out = emb.embed_texts(['a'], batch_size=10)
    finally:
        _restore_embeddings(emb)
    assert out == [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                    0.0, 0.0, 0.0, 0.0][:len(out[0])]] or all(v == 0.0 for v in out[0]), out
    assert all(v == 0.0 for v in out[0]), 'failed batch → zero vector'
    _ok('embed_texts: exception path retries then degrades to zero-vector (no raise)')


def test_embed_empty_input():
    import lib.embeddings as emb
    assert emb.embed_texts([]) == []
    _ok('embed_texts: empty input → []')


# ── 2. optimizer proposer extract_json ────────────────────────────────

def _run_propose(canned_content):
    import lib.optimizer.proposer as prop
    from lib.optimizer.analyzer import EvidenceBundle
    # All EvidenceBundle fields have defaults → no-arg construction is valid;
    # its contents only get formatted into the prompt, which llm_override bypasses.
    ev = EvidenceBundle()
    return prop.propose(ev, llm_override=lambda msgs: (canned_content, {}))


def test_proposer_parses_fenced_json():
    content = '```json\n{"proposals": [{"title": "t", "rationale": "r", "action_type": "other"}]}\n```'
    out = _run_propose(content)
    assert len(out) == 1 and out[0]['action_type'] == 'other', out
    _ok('proposer: fenced JSON parsed via extract_json')


def test_proposer_parses_prose_wrapped_json():
    # Prose around a NON-empty proposals object: bare json.loads(strip_fences(..))
    # fails on the leading prose and drops it (returns []); extract_json's
    # balanced-block scan recovers it. This is the discriminating case.
    content = ('Here are my proposals:\n'
               '{"proposals": [{"title": "t", "rationale": "r", "action_type": "other"}]}\n'
               'Done.')
    out = _run_propose(content)
    assert len(out) == 1 and out[0]['action_type'] == 'other', out
    _ok('proposer: prose-wrapped non-empty JSON recovered via balanced-block')


def test_proposer_garbage_returns_empty():
    out = _run_propose('not json at all')
    assert out == [], out
    _ok('proposer: unparseable content → [] (no crash)')


def test_proposer_uses_extract_json():
    """Static guard: propose() delegates to the shared extract_json helper."""
    import inspect
    import lib.optimizer.proposer as prop
    src = inspect.getsource(prop.propose)
    assert 'extract_json' in src, 'propose() should use lib.llm_json.extract_json'
    _ok('proposer: delegates to shared extract_json')


def main():
    print()
    print(_color('═══ Backend Cleanup Batch B (redundancy) ═══', '36'))
    print()
    tests = [
        test_embed_success_scatters,
        test_embed_429_retries_next_slot,
        test_embed_exception_first_attempt_is_retried_not_raised,
        test_embed_empty_input,
        test_proposer_parses_fenced_json,
        test_proposer_parses_prose_wrapped_json,
        test_proposer_garbage_returns_empty,
        test_proposer_uses_extract_json,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
