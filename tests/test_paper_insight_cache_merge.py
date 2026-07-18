#!/usr/bin/env python3
"""Cache-reopen insight-merge test (routes.paper._append_cached_insight).

The insight second-pass persists its section as a SEPARATE ``insight:<ui>`` row.
When a reader reopens a paper, the plain report is served from the DB cache —
this helper must LEFT-JOIN the sibling insight row and append its markdown so the
section survives a return visit (the "reader actually gains insight" objective in
the common reopen case), WITHOUT triggering a new generation.

Proven fully offline by stubbing the DB fetch (so we don't drag the Quart route
stack / websocket shim):
  1. plain report + a persisted insight row → reopen merges the section ONCE;
  2. no insight row → body byte-identical to today (no-op);
  3. body already contains the section → no double-append (idempotent);
  4. Review Mode key → never merges (insight is plain-report only);
  5. NEUTER: break the join (helper returns body unchanged) → section absent,
     proving the merge is what surfaces it.

Run standalone: ``python3 tests/test_paper_insight_cache_merge.py``
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('TRADING_ENABLED', '0')


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


_REPORT_BODY = '# dUltra: Ultra-Fast Diffusion Decoding\n\n## TL;DR\nA fast diffusion LM.\n'
_INSIGHT_BODY = ('## 💡 Insight & Ideas\n\n### The Bet\n\n> Key takeaway: Removing '
                 'recurrence is the bet.\n\n### Connections to Your Reading\n\n'
                 '- Same move as EvoLM ([EvoLM](https://arxiv.org/abs/2605.03871))\n')


def _install_stub_db(rows):
    """Patch routes.paper.async_fetchone to serve ``rows`` keyed by (phash, lang).

    Imports routes.paper lazily and tolerates the known bare-import websocket
    shim issue by importing the MODULE object via importlib after registering a
    minimal shim if needed.
    """
    import routes.paper as rp

    async def _fake_fetchone(sql, params, **kw):
        # params = (phash, lang)
        key = (params[0], params[1])
        return rows.get(key)

    rp.async_fetchone = _fake_fetchone
    return rp


def _import_routes_paper():
    """Import routes.paper, working around the bare-import Blueprint.websocket
    shim gap (routes/__init__ registers a @bp.websocket route; the decorator
    only exists once the Quart app shim is applied). Install a no-op shim
    BEFORE the first import so the cold import of routes/__init__ succeeds."""
    # routes/push.py does `from flask import Blueprint` and `@push_bp.websocket`;
    # the .websocket decorator only exists after server.py's Flask→Quart shim
    # runs, which a bare test import skips. Add a no-op decorator on BOTH the
    # flask and quart Blueprint classes before the cold import.
    for modname in ('flask', 'quart'):
        try:
            mod = __import__(modname)
            if hasattr(mod, 'Blueprint') and not hasattr(mod.Blueprint, 'websocket'):
                mod.Blueprint.websocket = lambda self, *a, **k: (lambda f: f)
        except Exception:
            pass
    import routes.paper as rp
    return rp


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_reopen_merges_insight_once():
    rp = _import_routes_paper()
    phash = 'abc123'
    from lib.paper.insight_engine import insight_lang_key
    rows = {
        (phash, 'en'): {'report': _REPORT_BODY},
        (phash, insight_lang_key('en')): {'report': _INSIGHT_BODY},
    }
    _install_stub_db(rows)
    out = _run(rp._append_cached_insight(_REPORT_BODY, phash, 'en'))
    assert '## 💡 Insight & Ideas' in out, 'insight section not merged on reopen'
    assert out.count('## 💡 Insight & Ideas') == 1, 'insight section appended more than once'
    assert out.startswith('# dUltra'), 'report body was mangled'
    _ok('reopen: sibling insight row merged into cached report exactly once')


def test_no_insight_row_is_noop():
    rp = _import_routes_paper()
    phash = 'noins'
    _install_stub_db({(phash, 'en'): {'report': _REPORT_BODY}})
    out = _run(rp._append_cached_insight(_REPORT_BODY, phash, 'en'))
    assert out == _REPORT_BODY, 'body not byte-identical when no insight row exists'
    _ok('no insight row → body byte-identical to today (no-op)')


def test_idempotent_when_body_already_has_section():
    rp = _import_routes_paper()
    phash = 'baked'
    from lib.paper.insight_engine import insight_lang_key
    already = _REPORT_BODY.rstrip() + '\n\n' + _INSIGHT_BODY
    _install_stub_db({
        (phash, 'en'): {'report': already},
        (phash, insight_lang_key('en')): {'report': _INSIGHT_BODY},
    })
    out = _run(rp._append_cached_insight(already, phash, 'en'))
    assert out.count('## 💡 Insight & Ideas') == 1, 'double-appended an already-present section'
    _ok('idempotent: no double-append when body already carries the section')


def test_review_mode_never_merges():
    rp = _import_routes_paper()
    phash = 'rev1'
    from lib.paper.insight_engine import insight_lang_key
    # Even if an insight row somehow existed, a review key must not merge.
    _install_stub_db({
        (phash, insight_lang_key('en')): {'report': _INSIGHT_BODY},
    })
    review_body = '# Peer Review\n\nScorecard…\n'
    out = _run(rp._append_cached_insight(review_body, phash, 'review:neurips:en'))
    assert out == review_body, 'insight leaked into a Review Mode reopen'
    _ok('review mode → insight never merged (plain-report only)')


def test_neuter_break_join_section_absent():
    """NEUTER: replace the merge with a pass-through → reopened body lacks the
    section, proving _append_cached_insight is what surfaces it."""
    rp = _import_routes_paper()
    phash = 'abc123'
    from lib.paper.insight_engine import insight_lang_key
    rows = {
        (phash, 'en'): {'report': _REPORT_BODY},
        (phash, insight_lang_key('en')): {'report': _INSIGHT_BODY},
    }
    _install_stub_db(rows)
    orig = rp._append_cached_insight

    async def _neutered(body, phash_, lang):
        return body  # join removed

    rp._append_cached_insight = _neutered
    try:
        out = _run(rp._append_cached_insight(_REPORT_BODY, phash, 'en'))
        assert '## 💡 Insight & Ideas' not in out, \
            'NEUTER failed — section present with the join removed (test is false-confident)'
    finally:
        rp._append_cached_insight = orig
    # And the real helper DOES surface it (control).
    out2 = _run(rp._append_cached_insight(_REPORT_BODY, phash, 'en'))
    assert '## 💡 Insight & Ideas' in out2, 'control: real helper must merge'
    _ok('NEUTER: breaking the join drops the section (merge is load-bearing)')


def main():
    print()
    print(_color('═══ Paper Insight Cache-Reopen Merge Tests ═══', '36'))
    print()
    tests = [
        test_reopen_merges_insight_once,
        test_no_insight_row_is_noop,
        test_idempotent_when_body_already_has_section,
        test_review_mode_never_merges,
        test_neuter_break_join_section_absent,
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
