#!/usr/bin/env python3
"""Optimizer proposer JSON-extraction regression tests."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ── optimizer proposer extract_json ──────────────────────────────────

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
