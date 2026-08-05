#!/usr/bin/env python3
"""Regression test: paper report must not render twice.

Root cause (2026-06-25): ``_run_report_task`` accumulated streamed content
across EVERY dispatch round into a single ``full_content`` string. A
tool-calling model frequently emits a full interim DRAFT of the report in a
round that also issues a tool call, then rewrites the whole report in the
final (no-tool-call) round — so the persisted report contained the report
TWICE.

Fix: content emitted in a round that ends with tool calls is an interim
draft and is discarded (a ``delta_reset`` event tells pollers to clear their
buffer too). Only the terminal round's content survives.

These tests mock ``dispatch_stream`` so the bug reproduces deterministically
(the live model only triggered it intermittently).
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _make_task(tid='rpt_dedup_test'):
    from lib.paper import _new_report_task
    # See test_paper_report_abort._make_task — offline suites must not let
    # the insight second pass dispatch a real LLM (CI 401 → endless cooldown).
    return _new_report_task(tid, 'phashdedup0000000000000000000000', 'en', None,
                            client_title='Test Paper',
                            config={'paperInsightEnabled': False})


def _patch_dispatch(monkeyplan):
    """Replace report_engine.dispatch_stream with a scripted sequence.

    monkeyplan: list of (content, tool_calls) tuples, one per round. Each
    invocation pops the next plan entry, streams ``content`` via on_content,
    and returns a msg dict with the given tool_calls.
    """
    import lib.paper.report_engine as re_mod
    plan = list(monkeyplan)

    def _fake_dispatch(messages, on_content=None, on_thinking=None, **kw):
        content, tool_calls = plan.pop(0)
        if content and on_content:
            on_content(content)
        msg = {'role': 'assistant', 'content': content, 'tool_calls': tool_calls}
        usage = {'prompt_tokens': 10, 'completion_tokens': 20, '_dispatch': {}}
        finish = 'tool_calls' if tool_calls else 'stop'
        return msg, finish, usage

    re_mod.dispatch_stream = _fake_dispatch


def _restore_dispatch(orig):
    import lib.paper.report_engine as re_mod
    re_mod.dispatch_stream = orig


REPORT_BODY = (
    '## ⚡ TL;DR\nThe Transformer dispenses with recurrence.\n\n'
    '## 📋 Paper Card\n| Title | Attention |\n\n'
    '## 📝 Technical Reference\nThe end.\n'
)


def test_interim_draft_discarded():
    """A draft emitted alongside a tool call must NOT appear in the report."""
    import lib.paper.report_engine as re_mod
    orig = re_mod.dispatch_stream
    # Round 1: model emits a FULL interim draft AND a web_search tool call.
    # Round 2: model rewrites the full report, no tool call → terminal.
    tool_call = [{'id': 'tc1', 'function': {'name': 'web_search',
                                            'arguments': '{"query": "transformer"}'}}]
    _patch_dispatch([(REPORT_BODY, tool_call), (REPORT_BODY, [])])

    # Stub the actual tool execution (no network).
    orig_tool = re_mod._execute_report_tool
    re_mod._execute_report_tool = lambda *a, **k: ('search results here', [], None, None, None)
    try:
        task = _make_task('rpt_dedup_1')
        re_mod._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        report = task.get('enriched_text') or task.get('full_text') or ''
        n = report.count('## ⚡ TL;DR')
        assert n == 1, f'TL;DR heading appears {n}× — report duplicated!'
        assert report.count('## 📝 Technical Reference') == 1
        assert task['status'] == 'done'
    finally:
        re_mod._execute_report_tool = orig_tool
        _restore_dispatch(orig)
    _ok('interim draft emitted alongside tool call is discarded (no double report)')


def test_delta_reset_event_emitted():
    """A delta_reset event must be emitted when a draft is discarded."""
    import lib.paper.report_engine as re_mod
    orig = re_mod.dispatch_stream
    tool_call = [{'id': 'tc1', 'function': {'name': 'web_search',
                                            'arguments': '{"query": "x"}'}}]
    _patch_dispatch([(REPORT_BODY, tool_call), (REPORT_BODY, [])])
    orig_tool = re_mod._execute_report_tool
    re_mod._execute_report_tool = lambda *a, **k: ('results', [], None, None, None)
    try:
        task = _make_task('rpt_dedup_2')
        re_mod._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        types = [e.get('type') for e in task['events']]
        assert 'delta_reset' in types, f'no delta_reset event; got {types}'
    finally:
        re_mod._execute_report_tool = orig_tool
        _restore_dispatch(orig)
    _ok('delta_reset event emitted so pollers clear the interim draft')


def test_no_tool_call_single_pass_unaffected():
    """The common case (write report directly, no tools) is unchanged."""
    import lib.paper.report_engine as re_mod
    orig = re_mod.dispatch_stream
    _patch_dispatch([(REPORT_BODY, [])])  # one round, no tools
    try:
        task = _make_task('rpt_dedup_3')
        re_mod._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        report = task.get('enriched_text') or task.get('full_text') or ''
        assert report.count('## ⚡ TL;DR') == 1
        assert 'The end.' in report
        assert task['status'] == 'done'
    finally:
        _restore_dispatch(orig)
    _ok('single-pass (no tool) report unaffected by the fix')


def test_tool_round_no_draft_then_final():
    """Tool round with NO prose, then final report — must be clean."""
    import lib.paper.report_engine as re_mod
    orig = re_mod.dispatch_stream
    tool_call = [{'id': 'tc1', 'function': {'name': 'web_search',
                                            'arguments': '{"query": "y"}'}}]
    # Round 1: tool call, NO content (well-behaved model). Round 2: report.
    _patch_dispatch([('', tool_call), (REPORT_BODY, [])])
    orig_tool = re_mod._execute_report_tool
    re_mod._execute_report_tool = lambda *a, **k: ('results', [], None, None, None)
    try:
        task = _make_task('rpt_dedup_4')
        re_mod._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        report = task.get('enriched_text') or task.get('full_text') or ''
        assert report.count('## ⚡ TL;DR') == 1
        assert task['status'] == 'done'
    finally:
        re_mod._execute_report_tool = orig_tool
        _restore_dispatch(orig)
    _ok('well-behaved tool round (no prose) then final report is clean')


REVIEW_BODY = (
    '# Review\n\n## Summary\nThe paper proposes X.\n\n'
    '## Strengths\n- A clean idea.\n\n## Weaknesses\n- §4 lacks an ablation.\n\n'
    '## Quantitative Scores\n- Overall Rating: 5.\n'
)


def _patch_dispatch_restreaming(body):
    """Mock a TERMINAL round whose stream is emitted TWICE via on_content but
    whose RETURNED msg['content'] is the single clean copy.

    This is exactly what lib/llm/stream.py::stream_chat produces on a mid-stream
    transient retry: the first attempt pushes deltas through on_content and then
    fails; the retry re-streams the WHOLE body through on_content again; but the
    returned assistant msg carries only the LAST attempt's accumulator (one
    clean copy). The engine must treat the deltas as display-only and persist
    the returned msg['content'], so a retry can never double the written doc.
    """
    import lib.paper.report_engine as re_mod

    def _fake_dispatch(messages, on_content=None, on_thinking=None, **kw):
        if on_content:
            on_content(body)   # attempt 1 streamed these deltas, then "failed"
            on_content(body)   # retry re-streamed the WHOLE body again
        # ...but the returned message is the single clean copy (one attempt's acc).
        msg = {'role': 'assistant', 'content': body, 'tool_calls': []}
        usage = {'prompt_tokens': 10, 'completion_tokens': 20, '_dispatch': {}}
        return msg, 'stop', usage

    re_mod.dispatch_stream = _fake_dispatch


def test_midstream_retry_restream_does_not_double_persisted_body():
    """BITING TEST (#1): a mid-stream retry that re-streams the whole review
    must yield ONE copy in the persisted/enriched body.

    The persisted body is authoritative from the terminal round's returned
    msg['content'] — NOT the accumulated deltas — so however many times the
    stream re-emitted, count('# Review')==1 and count('## Summary')==1.
    """
    import lib.paper.report_engine as re_mod
    from lib.paper import _new_report_task, make_review_lang
    orig = re_mod.dispatch_stream
    _patch_dispatch_restreaming(REVIEW_BODY)
    try:
        task = _new_report_task('rvw_retry_1', 'phashretry000000000000000000retry',
                                make_review_lang('neurips', 'en'), None,
                                client_title='Paper', ui_lang='en')
        re_mod._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        body = task.get('enriched_text') or task.get('full_text') or ''
        assert task['status'] == 'done', task.get('error')
        assert body.count('# Review') == 1, \
            f"'# Review' appears {body.count('# Review')}× — review DOUBLED by re-stream!"
        assert body.count('## Summary') == 1, \
            f"'## Summary' appears {body.count('## Summary')}× — review DOUBLED by re-stream!"
    finally:
        _restore_dispatch(orig)
    _ok('mid-stream retry re-streaming the whole review does not double the persisted body')


def test_source_level_negative_control_authoritative_body_load_bearing():
    """Prove the terminal-content override is load-bearing: neutralize it (revert
    to delta-accumulation) in a source COPY exec'd in-process, and the re-stream
    doubling must reappear. The shipped file is never modified."""
    import types
    import lib.paper.report_engine as rm
    from lib.paper import _new_report_task, make_review_lang

    src = open(rm.__file__, encoding='utf-8').read()
    marker = ("        _clean = _terminal['content']\n"
              "        if _clean is not None and _clean != full_content:")
    assert marker in src, 'neuter marker not found — test is stale, update the marker'
    broken = src.replace(
        marker,
        "        _clean = _terminal['content']\n"
        "        if False and _clean is not None and _clean != full_content:",
        1)
    assert broken != src, 'negative-control patch was a no-op'

    mod = types.ModuleType('lib.paper.report_engine_nc')
    mod.__file__ = rm.__file__
    mod.__package__ = 'lib.paper'
    exec(compile(broken, rm.__file__, 'exec'), mod.__dict__)

    def _fake(messages, on_content=None, on_thinking=None, **kw):
        if on_content:
            on_content(REVIEW_BODY)
            on_content(REVIEW_BODY)
        return {'role': 'assistant', 'content': REVIEW_BODY, 'tool_calls': []}, 'stop', \
               {'prompt_tokens': 1, 'completion_tokens': 1, '_dispatch': {}}
    mod.dispatch_stream = _fake

    task = _new_report_task('rvw_nc_1', 'phashncbody0000000000000000000nc',
                            make_review_lang('neurips', 'en'), None, ui_lang='en')
    mod._run_report_task(task, [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'paper'},
    ], [])
    body = task.get('enriched_text') or task.get('full_text') or ''
    assert body.count('# Review') == 2, \
        'neutering the authoritative-body override did NOT resurface the double — ' \
        'fix is non-load-bearing'
    # Shipped file untouched (we only exec'd an in-memory copy).
    assert open(rm.__file__, encoding='utf-8').read() == src, 'shipped file was modified!'
    _ok('source-level NC: reverting to delta-accumulation resurfaces the double (fix load-bearing)')


def main():
    print()
    print(_color('═══ Paper Report De-dup Regression Tests ═══', '36'))
    print()
    tests = [
        test_interim_draft_discarded,
        test_delta_reset_event_emitted,
        test_no_tool_call_single_pass_unaffected,
        test_tool_round_no_draft_then_final,
        test_midstream_retry_restream_does_not_double_persisted_body,
        test_source_level_negative_control_authoritative_body_load_bearing,
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
