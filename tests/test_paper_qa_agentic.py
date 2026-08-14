#!/usr/bin/env python3
"""Headless end-to-end tests for the agentic paper Q&A rebuild (2026-06-25).

Verifies the three hard requirements from the objective:

  (a) A question about a REPORT-ONLY claim is answered using the injected
      report — the report text reaches the model (legacy Q&A never showed it).
  (b) A question needing external info actually triggers web_search/fetch_url
      (the loop runs tools, not just a single stateless completion).
  (c) A LONG paper does not silently lose its tail — section-aware selection
      keeps the relevant late section instead of blind 100k head-truncation.

Plus context-builder unit checks (section split, budget keep-all, report
injection, tail retrieval) and the interim-draft discard.

dispatch_stream is mocked so the agent loop is driven deterministically with
no network. Run standalone: ``python3 tests/test_paper_qa_agentic.py``
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ─── Context builder ─────────────────────────────────────────────

def test_split_into_sections_by_heading():
    from lib.paper import split_into_sections
    text = ('Title Line\nAbstract here.\n\n'
            '# Introduction\nIntro body.\n\n'
            '## Method\nMethod body.\n\n'
            '## Limitations\nWe cannot scale past 1B params.\n')
    secs = split_into_sections(text)
    headings = [s['heading'] for s in secs]
    assert 'Introduction' in headings and 'Method' in headings and 'Limitations' in headings
    # Preamble (title+abstract) becomes section 0 with empty heading.
    assert secs[0]['heading'] == '' and 'Abstract' in secs[0]['text']
    _ok('split_into_sections splits on Markdown headings (+ preamble section 0)')


def test_split_fallback_no_headings():
    from lib.paper import split_into_sections
    text = '\n\n'.join(['Paragraph %d %s' % (i, 'x' * 2000) for i in range(6)])
    secs = split_into_sections(text)
    assert len(secs) >= 2, 'no-heading text should still chunk into pieces'
    _ok('split_into_sections falls back to chunking when no headings')


def test_select_keeps_all_when_under_budget():
    from lib.paper import split_into_sections, select_relevant_sections
    text = '# A\nshort\n\n# B\nalso short\n'
    secs = split_into_sections(text)
    sel = select_relevant_sections('anything', secs, budget_chars=100000)
    assert len(sel) == len(secs), 'whole paper fits → keep every section'
    _ok('select_relevant_sections keeps ALL sections when under budget')


def test_select_retrieves_relevant_tail_section():
    """The CORE long-paper guarantee: a relevant LATE section is retrieved."""
    from lib.paper import split_into_sections, select_relevant_sections
    # Build a long paper: a unique tail section after lots of filler so blind
    # head-truncation would drop it.
    filler = '\n\n'.join('# Filler %d\n%s' % (i, 'lorem ipsum dolor ' * 400)
                         for i in range(20))
    tail = '# Reproducibility\nWe release weights and the training seed is 1337 zebrafish.'
    text = filler + '\n\n' + tail
    secs = split_into_sections(text)
    sel = select_relevant_sections('what is the random seed zebrafish?', secs,
                                   budget_chars=8000)
    joined = '\n'.join(s['text'] for s in sel)
    assert 'zebrafish' in joined, 'relevant tail section was NOT retrieved (tail lost!)'
    assert len(joined) < len(text), 'budget should have excluded most filler'
    _ok('select_relevant_sections retrieves the relevant TAIL of a long paper')


def test_build_qa_messages_injects_report():
    from lib.paper import build_qa_messages
    report = ('# Paper X\n## Limitations\nThe method degrades on out-of-domain '
              'data and was only tested on English.')
    paper = '# Intro\nWe propose X.\n\n# Method\nX works by foo.'
    msgs, diag = build_qa_messages(
        'What did you mean in the Limitations section?', paper, report)
    sys_msg = msgs[0]['content']
    assert diag['report_present'] is True
    assert 'GENERATED ANALYSIS REPORT' in sys_msg
    assert 'degrades on out-of-domain' in sys_msg, 'report body not injected'
    assert msgs[-1]['role'] == 'user'
    _ok('build_qa_messages injects the full generated report into context')


def test_build_qa_messages_long_paper_keeps_tail():
    from lib.paper import build_qa_messages
    filler = '\n\n'.join('# Sec %d\n%s' % (i, 'padding words here ' * 500)
                         for i in range(30))
    tail = '# Appendix\nThe secret token is platypus-42.'
    paper = filler + '\n\n' + tail
    msgs, diag = build_qa_messages('what is the secret token platypus?', paper, '',
                                   section_budget=10000)
    sys_msg = msgs[0]['content']
    assert 'platypus-42' in sys_msg, 'long-paper tail lost despite relevance'
    assert diag['n_sections_selected'] < diag['n_sections_total'], \
        'budget should have dropped irrelevant filler'
    _ok('build_qa_messages keeps a relevant tail section of a long paper')


# ─── End-to-end through the Q&A engine (mocked dispatch) ─────────

def _patch_dispatch(plan):
    """Scripted dispatch_stream: plan is [(content, tool_calls), ...]."""
    import lib.paper.qa_engine as qe
    seq = list(plan)
    captured = {'messages_seen': []}

    def _fake(messages, on_content=None, on_thinking=None, **kw):
        captured['messages_seen'].append(messages)
        content, tool_calls = seq.pop(0)
        if content and on_content:
            on_content(content)
        msg = {'role': 'assistant', 'content': content, 'tool_calls': tool_calls}
        return msg, ('tool_calls' if tool_calls else 'stop'), {'_dispatch': {}}

    qe.dispatch_stream = _fake
    return captured


def test_engine_answers_report_only_question_from_injected_report():
    """(a) Report-only claim → answered using the injected report, no tools."""
    import lib.paper.qa_engine as qe
    from lib.paper import _new_qa_task, build_qa_messages
    orig = qe.dispatch_stream
    # The model answers in one shot (no tool call) — proving it had the report.
    cap = _patch_dispatch([
        ('The Limitations section means the method only works on English '
         'and degrades out-of-domain.', []),
    ])
    try:
        report = ('# Paper\n## Limitations\nThe method degrades on out-of-domain '
                  'data and was only tested on English.')
        msgs, _ = build_qa_messages('what did you mean in Limitations?',
                                    '# Intro\nfoo', report)
        task = _new_qa_task('qa_t1', 'abcdef0000000000000000000000aa10', 'en', None,
                            question='what did you mean in Limitations?')
        qe._run_qa_task(task, msgs)
        assert task['status'] == 'done', task.get('error')
        answer = task['full_text']
        assert 'English' in answer
        assert len(task['tool_rounds']) == 0, 'should NOT need tools for a report question'
        # Prove the report actually reached the model.
        first_sys = cap['messages_seen'][0][0]['content']
        assert 'degrades on out-of-domain' in first_sys
    finally:
        qe.dispatch_stream = orig
    _ok('(a) report-only question answered from injected report, no tools used')


def test_engine_triggers_web_search_for_external_question():
    """(b) External-info question → the loop actually calls web_search."""
    import lib.paper.qa_engine as qe
    from lib.paper import _new_qa_task
    orig_disp = qe.dispatch_stream
    orig_tool = qe._execute_report_tool
    tool_calls_seen = []

    # Round 1: model requests web_search. Round 2: model writes the answer.
    cap = _patch_dispatch([
        ('', [{'id': 'tc1', 'function': {'name': 'web_search',
                                         'arguments': '{"query": "Transformer follow-up 2024"}'}}]),
        ('The most cited follow-up is the Vision Transformer (ICLR 2021).', []),
    ])

    def _fake_tool(name, args, user_question='', abort=None, **_kw):
        tool_calls_seen.append(name)
        return ('Search results: ViT, Reformer, Performer …', [{'title': 'ViT'}], None, None, None)

    qe._execute_report_tool = _fake_tool
    try:
        task = _new_qa_task('qa_t2', 'abcdef0000000000000000000000aa11', 'en', None,
                            question='what built on this paper?')
        qe._run_qa_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'what built on this paper?'},
        ])
        assert task['status'] == 'done', task.get('error')
        assert 'web_search' in tool_calls_seen, 'web_search was NOT triggered'
        assert len(task['tool_rounds']) == 1
        assert task['tool_rounds'][0]['toolName'] == 'web_search'
        assert 'Vision Transformer' in task['full_text']
        # The tool result must have been fed back into the messages.
        last_msgs = cap['messages_seen'][-1]
        assert any(m.get('role') == 'tool' for m in last_msgs), 'tool result not fed back'
    finally:
        qe.dispatch_stream = orig_disp
        qe._execute_report_tool = orig_tool
    _ok('(b) external question triggers web_search and feeds the result back')


def test_engine_discards_interim_draft_with_tool_call():
    """A draft emitted alongside a tool call must not concatenate with the final answer."""
    import lib.paper.qa_engine as qe
    from lib.paper import _new_qa_task
    orig_disp, orig_tool = qe.dispatch_stream, qe._execute_report_tool
    _patch_dispatch([
        ('PARTIAL DRAFT ', [{'id': 't', 'function': {'name': 'web_search',
                                                     'arguments': '{"query":"x"}'}}]),
        ('FINAL ANSWER.', []),
    ])
    qe._execute_report_tool = lambda *a, **k: ('results', [], None, None, None)
    try:
        task = _new_qa_task('qa_t3', 'abcdef0000000000000000000000aa12', 'en', None,
                            question='q')
        qe._run_qa_task(task, [{'role': 'system', 'content': 's'},
                               {'role': 'user', 'content': 'q'}])
        assert task['full_text'] == 'FINAL ANSWER.', repr(task['full_text'])
        assert 'PARTIAL DRAFT' not in task['full_text']
        reset = [e for e in task['events'] if e.get('type') == 'delta_reset']
        assert reset, 'delta_reset event not emitted'
    finally:
        qe.dispatch_stream, qe._execute_report_tool = orig_disp, orig_tool
    _ok('engine discards interim draft emitted alongside a tool call')


# ─── HTTP endpoint wiring (real Quart app) ──────────────────────

def test_qa_http_endpoints_wired():
    """/api/v1/paper/qa/{start,poll,abort} are registered and behave."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio
    import lib.paper.qa_engine as qe
    from lib.paper import _qa_runtime

    # Mock dispatch so the spawned task answers instantly without network.
    orig = qe.dispatch_stream
    def _fake(messages, on_content=None, **kw):
        if on_content:
            on_content('Answer from the paper.')
        return ({'role': 'assistant', 'content': 'Answer from the paper.', 'tool_calls': []},
                'stop', {'_dispatch': {}})
    qe.dispatch_stream = _fake

    async def _t():
        async with app.test_client() as client:
            # start
            r = await client.post('/api/v1/paper/qa/start', json={
                'question': 'What is the main result?',
                'paper_text': '# Intro\nWe propose X with 99% accuracy.\n\n# Results\nX wins.',
                'paper_hash': 'abcdef0000000000000000000000bb01',
                'lang': 'en',
            })
            assert r.status_code == 200, r.status_code
            data = await r.get_json()
            assert data['ok'] and data['task_id'], data
            tid = data['task_id']

            # Let the spawned worker finish.
            for _ in range(50):
                t = _qa_runtime.get(tid)
                if t and t['status'] in ('done', 'error'):
                    break
                await asyncio.sleep(0.05)

            # poll
            r2 = await client.get(f'/api/v1/paper/qa/poll?task_id={tid}&cursor=0')
            assert r2.status_code == 200
            d2 = await r2.get_json()
            assert d2['ok'] and d2['status'] == 'done', d2
            assert 'Answer from the paper' in (d2.get('answer') or '')
            types = [e.get('type') for e in d2['events']]
            assert 'done' in types

            # poll unknown → 404
            r3 = await client.get('/api/v1/paper/qa/poll?task_id=nope&cursor=0')
            assert r3.status_code == 404

            # abort unknown → 404 (factory-minted route: task_id is a path segment)
            r4 = await client.post('/api/v1/paper/qa/abort/nope')
            assert r4.status_code == 404

    try:
        asyncio.run(_t())
    finally:
        qe.dispatch_stream = orig
    _ok('HTTP qa/start spawns task, qa/poll returns done+answer, 404s on unknown')


def main():
    print()
    print(_color('═══ Agentic Paper Q&A Tests ═══', '36'))
    print()
    tests = [
        test_split_into_sections_by_heading,
        test_split_fallback_no_headings,
        test_select_keeps_all_when_under_budget,
        test_select_retrieves_relevant_tail_section,
        test_build_qa_messages_injects_report,
        test_build_qa_messages_long_paper_keeps_tail,
        test_engine_answers_report_only_question_from_injected_report,
        test_engine_triggers_web_search_for_external_question,
        test_engine_discards_interim_draft_with_tool_call,
        test_qa_http_endpoints_wired,
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
