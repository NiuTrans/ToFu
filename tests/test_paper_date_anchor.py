#!/usr/bin/env python3
"""Date-anchor tests for Paper Report / Review Mode.

Symptom the fix addresses: the report engine builds its OWN self-contained
``messages`` list and (unlike the main chat path) never inherits the
``Current date:`` system block. The model therefore only sees the paper's
PUBLICATION date (printed in the paper text) plus its training cutoff, assumes
"now" ≈ publication time, and writes things like "写作时尚无可检索的后续论文"
/ "no follow-up papers are searchable at the time of writing" — even though
generation happens months later, when follow-ups exist. This defeats the
prompt's own mandatory "follow-ups since publication" requirement.

Fix: ``lib.paper.prompts.date_anchor_clause(ui_lang)`` supplies today's UTC
date + an explicit note that the publication date is in the PAST, and the
report + review routes prepend it to the system message.

Coverage:
  • ``date_anchor_clause`` states today's date (EN+ZH) and the past-date note.
  • ROUTE integration: the system message the engine receives carries today's
    date for BOTH the review and the plain report path.
  • NEGATIVE CONTROL: no-op the clause → today's date vanishes from the system
    prompt and the route assertion FAILS; restored byte-identical afterwards.

dispatch_stream is mocked so the flow runs deterministically offline.
Run standalone: ``python3 tests/test_paper_date_anchor.py``
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


_TODAY = datetime.now(timezone.utc).strftime('%Y-%m-%d')

# A paper whose printed publication date is in the past. The model must NOT
# treat that date as "now".
PAPER = (
    'We propose a transformer variant. This paper was posted on 2024-01-07.\n'
    'Method: attention over tokens. Results: 90% on GLUE.\n'
)


# ─── Unit: date_anchor_clause ────────────────────────────────────

def test_date_anchor_states_today_en():
    from lib.paper.prompts import date_anchor_clause
    c = date_anchor_clause('en')
    assert _TODAY in c, f"today's date {_TODAY!r} not in clause: {c!r}"
    assert 'PAST' in c and 'now' in c.lower(), 'past-date note missing'
    assert 'web_search' in c, 'clause should steer toward web_search'
    _ok("date_anchor_clause(en) states today's date + past-date note")


def test_date_anchor_states_today_zh():
    from lib.paper.prompts import date_anchor_clause
    c = date_anchor_clause('zh')
    assert _TODAY in c, f"today's date {_TODAY!r} not in zh clause"
    assert '过去' in c and '现在' in c, 'zh past-date note missing'
    assert 'web_search' in c, 'zh clause should steer toward web_search'
    _ok("date_anchor_clause(zh) states today's date + past-date note")


# ─── Route integration (real Quart app) ──────────────────────────

def _load_app():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    return mod.app


def _capture_system_for(lang_key):
    """Drive /report/start under `lang_key` and capture the system message the
    engine received."""
    import asyncio
    import lib.paper.report_engine as re_mod
    from lib.paper import _report_runtime

    app = _load_app()
    orig = re_mod.dispatch_stream
    cap = {'system': None}

    def _fake(messages, on_content=None, on_thinking=None, **kw):
        if cap['system'] is None:
            cap['system'] = messages[0]['content']
        if on_content:
            on_content('# Review\n\n## Summary\nx\n')
        return ({'role': 'assistant', 'content': '# Review\n\n## Summary\nx\n',
                 'tool_calls': []}, 'stop', {'_dispatch': {}})

    re_mod.dispatch_stream = _fake
    paper = PAPER + '\n\n' + ('Padding sentence to clear the min-length gate. ' * 20)

    async def _t():
        async with app.test_client() as client:
            r = await client.post('/api/v1/paper/report/start', json={
                'paper_text': paper, 'lang': lang_key, 'force': True,
            })
            assert r.status_code == 200, r.status_code
            data = await r.get_json()
            assert data['ok'] and data['task_id'], data
            tid = data['task_id']
            for _ in range(80):
                t = _report_runtime.get(tid)
                if t and t['status'] in ('done', 'error'):
                    break
                await asyncio.sleep(0.05)

    try:
        asyncio.run(_t())
    finally:
        re_mod.dispatch_stream = orig
    assert cap['system'] is not None, 'dispatch never called'
    return cap['system']


def test_route_review_system_carries_today():
    system = _capture_system_for('review:neurips:en')
    assert _TODAY in system, "today's date not in review system message"
    assert 'PAST' in system, 'past-date note not in review system message'
    _ok("route (review): system message carries today's date + past-date note")


def test_route_report_system_carries_today():
    system = _capture_system_for('en')
    assert _TODAY in system, "today's date not in report system message"
    assert 'PAST' in system, 'past-date note not in report system message'
    _ok("route (report): system message carries today's date + past-date note")


def test_route_report_zh_system_carries_today():
    system = _capture_system_for('zh')
    assert _TODAY in system, "today's date not in zh report system message"
    assert '过去' in system, 'zh past-date note not in report system message'
    _ok("route (report, zh): system message carries today's date + past-date note")


# ─── SOURCE-LEVEL NEGATIVE CONTROL ──────────────────────────────

def test_source_level_negative_control_clause_noop_drops_date():
    """No-op ``date_anchor_clause`` → today's date vanishes from the system
    prompt and the route assertion FAILS. Proves the clause is load-bearing.

    routes/paper.py imported the name from the ``lib.paper`` facade, so patch
    BOTH the defining module and the facade/route bindings. Restore
    byte-identical afterwards.
    """
    import lib.paper.prompts as prompts_mod
    import lib.paper as paper_pkg
    import routes.paper as routes_paper

    orig_fn = prompts_mod.date_anchor_clause

    def _noop(ui_lang):
        return ''

    prompts_mod.date_anchor_clause = _noop
    paper_pkg.date_anchor_clause = _noop
    routes_paper.date_anchor_clause = _noop
    try:
        system = _capture_system_for('review:neurips:en')
        assert _TODAY not in system, \
            "today's date still present with clause no-op — clause not load-bearing"
    finally:
        prompts_mod.date_anchor_clause = orig_fn
        paper_pkg.date_anchor_clause = orig_fn
        routes_paper.date_anchor_clause = orig_fn

    # Restored: the date returns.
    system2 = _capture_system_for('review:neurips:en')
    assert _TODAY in system2, 'restore failed — date not reapplied'
    assert prompts_mod.date_anchor_clause is orig_fn, 'clause not restored'
    _ok('negative control: clause no-op removes today\'s date from prompt (bug reproduced), restore reapplies it')


# ─── Q&A path integration (build_qa_messages) ────────────────────

def _qa_system_message(lang='en'):
    """Drive build_qa_messages and return the system message.

    Unit-level (no HTTP): the Q&A message list is assembled by
    build_qa_messages, which self-builds a system prompt that does
    time-relative reasoning (web_search for recent follow-ups) — so it needs
    the same date anchor as the report/review engines.
    """
    from lib.paper.qa_context import build_qa_messages
    paper = PAPER + '\n\n' + ('Filler section body sentence. ' * 40)
    msgs, _diag = build_qa_messages('有没有后续工作超越了它？', paper,
                                    report_md='', lang=lang)
    return msgs[0]['content']


def test_qa_system_carries_today_en():
    system = _qa_system_message('en')
    assert _TODAY in system, "today's date not in QA system message"
    assert 'PAST' in system, 'past-date note not in QA system message'
    # Ordering: date anchor precedes the input-safety clause + fenced paper.
    assert system.index(_TODAY) < system.index('Input safety'), \
        'date anchor must precede the input-safety clause'
    assert system.index('Input safety') < system.index('BEGIN UNTRUSTED PAPER TEXT'), \
        'input-safety clause must precede the fenced paper block'
    _ok("QA path (en): system message carries today's date, ordered date \u2192 safety \u2192 paper")


def test_qa_system_carries_today_zh():
    system = _qa_system_message('zh')
    assert _TODAY in system, "today's date not in zh QA system message"
    assert '\u8fc7\u53bb' in system, 'zh past-date note not in QA system message'
    _ok("QA path (zh): system message carries today's date + past-date note")


def test_source_level_negative_control_qa_clause_noop_drops_date():
    """No-op date_anchor_clause → today's date vanishes from the Q&A system
    prompt. build_qa_messages does a LOCAL ``from .prompts import
    date_anchor_clause`` at call time, so patching the defining module is what
    the next call resolves. Restore byte-identical afterwards.
    """
    import lib.paper.prompts as prompts_mod
    orig_fn = prompts_mod.date_anchor_clause

    def _noop(ui_lang):
        return ''

    prompts_mod.date_anchor_clause = _noop
    try:
        system = _qa_system_message('en')
        assert _TODAY not in system, \
            "today's date still present with QA clause no-op — clause not load-bearing"
    finally:
        prompts_mod.date_anchor_clause = orig_fn
    assert _TODAY in _qa_system_message('en'), 'restore failed — date not reapplied'
    assert prompts_mod.date_anchor_clause is orig_fn, 'clause not restored'
    _ok('negative control (QA): clause no-op removes date from Q&A prompt (bug reproduced), restore reapplies it')


def main():
    print()
    print(_color('═══ Paper Date-Anchor Tests ═══', '36'))
    print()
    tests = [
        test_date_anchor_states_today_en,
        test_date_anchor_states_today_zh,
        test_route_review_system_carries_today,
        test_route_report_system_carries_today,
        test_route_report_zh_system_carries_today,
        test_source_level_negative_control_clause_noop_drops_date,
        test_qa_system_carries_today_en,
        test_qa_system_carries_today_zh,
        test_source_level_negative_control_qa_clause_noop_drops_date,
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
