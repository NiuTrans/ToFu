#!/usr/bin/env python3
"""Headless tests: the describe-to-recommend RESEARCH searches deterministically
hit the ACADEMIC vertical (arXiv + Semantic Scholar JSON APIs), instead of
hoping the model chooses ``vertical='academic'`` itself.

Root cause this pins (see the ACL-26 "no results" trace): for a known-title
paper lookup the one dependency that was healthy the whole time — the academic
JSON APIs, which have their OWN uptime independent of the Brave/Bing/DDG/
SearXNG HTML fleet and its per-engine circuit breakers — was NEVER consulted by
``web_search``, because the prompt only *recommended* the academic vertical and
the model didn't pick it. Relying on the model to choose the robust path is the
defect. The fix makes the CODE guarantee it:

  * ``recommend_engine._RESEARCH_VERTICAL == 'academic'`` and the interpretation
    loop passes it to ``_execute_report_tool(..., force_vertical=...)``;
  * ``_execute_report_tool(force_vertical='academic')`` overrides EVERY query
    spec's vertical unconditionally — even when the model asked for ``'auto'``
    / ``'off'`` / a wrong domain — before it reaches ``_web_search_one``;
  * the default (``force_vertical=None``) is byte-identical to today, so the
    shared report / QA / insight callers are untouched.

NEUTER (``test_neuter_force_vertical_is_load_bearing``): with
``_RESEARCH_VERTICAL`` set to ``None`` the research search reverts to the
model's own vertical (``'auto'``) — proving the forced-academic constant is what
delivers the robustness, not some incidental default.

Runs fully offline: ``dispatch_stream`` is faked to emit a web_search call on
round 0 and the final JSON on round 1; ``lib.paper.tools._web_search_one`` is
faked to CAPTURE the vertical it is handed (and return no results); grounding's
``search_arxiv`` is a no-op.

Run standalone: ``python3 tests/test_paper_recommend_academic_vertical.py``
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TRADING_ENABLED', '0')

import lib.paper.recommend_engine as re_mod  # noqa: E402
import lib.paper.tools as tools_mod  # noqa: E402


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# The final-round JSON the model returns AFTER researching.
_FINAL_JSON = {
    'candidates': [
        {'title': 'Some Paper', 'arxiv_id': None, 'venue': None,
         'why': 'matches the description.'},
    ],
    'correction': None,
}


class _Patched:
    """Fake a 2-round agentic dispatch (round 0 = a web_search tool call whose
    args carry ``model_vertical``; round 1 = the final JSON) AND capture the
    ``vertical`` value that ultimately reaches ``_web_search_one`` through the
    REAL ``_execute_report_tool``.

    Only the LLM and the actual network search are faked — ``_execute_report_tool``
    runs for real, so the test exercises the whole force-vertical seam end to end.
    """

    def __init__(self, *, model_vertical=None):
        self.model_vertical = model_vertical
        self._orig = {}
        self.captured_verticals = []   # vertical handed to _web_search_one

    def __enter__(self):
        self._orig['dispatch_stream'] = re_mod.dispatch_stream
        self._orig['search_arxiv'] = re_mod.search_arxiv
        self._orig['fetch_arxiv_title'] = re_mod.fetch_arxiv_title
        self._orig['_web_search_one'] = tools_mod._web_search_one
        rec = self

        def _fake_dispatch_stream(messages, *, on_content=None, tools=None, **kw):
            n_tool_msgs = sum(1 for m in messages if m.get('role') == 'tool')
            if n_tool_msgs == 0:
                q = {'query': 'diffusion language model ICML 2025'}
                if rec.model_vertical is not None:
                    q['vertical'] = rec.model_vertical
                return ({'role': 'assistant', 'content': '',
                         'tool_calls': [{
                             'id': 'tc1', 'type': 'function',
                             'function': {'name': 'web_search',
                                          'arguments': json.dumps({'queries': [q]})},
                         }]}, 'tool_calls', {'prompt_tokens': 1, 'completion_tokens': 1})
            body = json.dumps(_FINAL_JSON)
            if on_content:
                on_content(body)
            return ({'role': 'assistant', 'content': body, 'tool_calls': None},
                    'stop', {'prompt_tokens': 1, 'completion_tokens': 1})

        def _fake_web_search_one(query, user_question, freshness='', vertical='auto'):
            rec.captured_verticals.append(vertical)
            return ([], None, None, None)

        re_mod.dispatch_stream = _fake_dispatch_stream
        re_mod.search_arxiv = lambda *a, **k: []
        re_mod.fetch_arxiv_title = lambda _id: ''
        tools_mod._web_search_one = _fake_web_search_one
        return self

    def __exit__(self, *exc):
        re_mod.dispatch_stream = self._orig['dispatch_stream']
        re_mod.search_arxiv = self._orig['search_arxiv']
        re_mod.fetch_arxiv_title = self._orig['fetch_arxiv_title']
        tools_mod._web_search_one = self._orig['_web_search_one']
        return False


def test_constant_is_academic():
    """The research vertical constant exists and is the academic domain."""
    assert getattr(re_mod, '_RESEARCH_VERTICAL', None) == 'academic', \
        f'_RESEARCH_VERTICAL is not "academic": {getattr(re_mod, "_RESEARCH_VERTICAL", None)!r}'
    _ok('recommend_engine._RESEARCH_VERTICAL == "academic"')


def test_research_search_forces_academic_when_model_omits_it():
    """Model issues a plain web_search (no vertical → 'auto'); the code forces
    the academic vertical anyway, so the arXiv/S2 JSON path is consulted."""
    with _Patched(model_vertical=None) as p:
        re_mod.recommend_papers('that diffusion LM from the last ICML', 6)
    assert p.captured_verticals, 'no web_search reached _web_search_one'
    assert all(v == 'academic' for v in p.captured_verticals), \
        f'research search did not force academic vertical: {p.captured_verticals}'
    _ok('research web_search forces vertical="academic" even when the model omits it')


def test_force_vertical_overrides_model_choice():
    """Even a model that explicitly picks a DIFFERENT vertical (e.g. 'off') is
    overridden — the robust academic path is not left to the model's choice."""
    with _Patched(model_vertical='off') as p:
        re_mod.recommend_papers('a recent dLLM paper', 6)
    assert p.captured_verticals, 'no web_search reached _web_search_one'
    assert all(v == 'academic' for v in p.captured_verticals), \
        f"model's 'off' vertical was not overridden to academic: {p.captured_verticals}"
    _ok('force_vertical overrides the model-chosen vertical (off → academic)')


def test_execute_report_tool_default_preserves_model_vertical():
    """Direct unit check of the shared seam: WITHOUT force_vertical (the report /
    QA / insight callers), the model's own vertical is preserved — the change is
    byte-identical for every other caller."""
    orig = tools_mod._web_search_one
    seen = []
    tools_mod._web_search_one = lambda q, uq, freshness='', vertical='auto': (
        seen.append(vertical) or ([], None, None, None))
    try:
        args = json.dumps({'queries': [{'query': 'q', 'vertical': 'off'}]})
        # No force_vertical → other callers' behaviour: model's vertical wins.
        tools_mod._execute_report_tool('web_search', args, user_question='q')
        assert seen == ['off'], f'default path altered the model vertical: {seen}'
        # force_vertical set → overridden.
        seen.clear()
        tools_mod._execute_report_tool('web_search', args, user_question='q',
                                       force_vertical='academic')
        assert seen == ['academic'], f'force_vertical did not override: {seen}'
    finally:
        tools_mod._web_search_one = orig
    _ok('_execute_report_tool: default preserves model vertical, force_vertical overrides it')


def test_prompt_instructs_unquoted_title_tokens():
    """Part (a): the research prompt must tell the model NOT to wrap a known
    title in exact-phrase quotes.

    Exact-quoted full-title phrase queries are the first thing a degraded HTML
    engine returns 0 for (the ACL-26 trace: three ``"..."``-quoted title
    searches, all empty). Since final grounding is arXiv-API and the research
    vertical is now forced academic, the model should issue UNQUOTED title
    tokens — higher recall, less fragile. We pin the instruction in the prompt
    (and the removal of the now-redundant 'Prefer vertical=academic' nudge,
    since the code forces it)."""
    sys_prompt = re_mod._RECOMMEND_SYSTEM.lower()
    # The prompt must explicitly steer away from exact-phrase quoting.
    assert 'unquoted' in sys_prompt or 'without quotes' in sys_prompt \
        or 'do not wrap' in sys_prompt or "don't wrap" in sys_prompt \
        or 'no exact-phrase' in sys_prompt or 'not quote' in sys_prompt, \
        'prompt does not instruct the model to issue unquoted title tokens: ' \
        f'{re_mod._RECOMMEND_SYSTEM[:400]!r}'
    # It must reference quotes / phrase so the guidance is unambiguous.
    assert 'quot' in sys_prompt or 'phrase' in sys_prompt, \
        'prompt guidance does not mention quotes/phrase'
    _ok('prompt instructs unquoted title tokens (drops fragile exact-phrase quoting)')


def test_neuter_force_vertical_is_load_bearing():
    """NEUTER: blank out the forced-academic constant → the research search
    reverts to the model's own 'auto' vertical, NOT academic. Proves the
    constant is what delivers the robust path (not an incidental default)."""
    orig_const = re_mod._RESEARCH_VERTICAL
    re_mod._RESEARCH_VERTICAL = None
    try:
        with _Patched(model_vertical=None) as p:
            re_mod.recommend_papers('recent dLLM', 6)
        assert p.captured_verticals, 'no web_search reached _web_search_one'
        assert all(v == 'auto' for v in p.captured_verticals), \
            f'NEUTER did not bite — academic still forced with the constant blanked: {p.captured_verticals}'
    finally:
        re_mod._RESEARCH_VERTICAL = orig_const
    _ok('NEUTER: with _RESEARCH_VERTICAL blanked, research reverts to model vertical (load-bearing)')


def main():
    print()
    print(_color('═══ Paper Recommend — Forced Academic Vertical Tests ═══', '36'))
    print()
    tests = [
        test_constant_is_academic,
        test_research_search_forces_academic_when_model_omits_it,
        test_force_vertical_overrides_model_choice,
        test_execute_report_tool_default_preserves_model_vertical,
        test_prompt_instructs_unquoted_title_tokens,
        test_neuter_force_vertical_is_load_bearing,
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
