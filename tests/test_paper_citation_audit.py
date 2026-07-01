#!/usr/bin/env python3
"""End-to-end (offline) test: paper-report citation-hallucination audit.

Drives the REAL report-engine path (`_run_report_task`) with a stubbed LLM
that writes a report citing three identifiers inline:
  - a bad DOI         (CrossRef 404)            → suspicious
  - a good arXiv id   (arXiv Atom has <entry>)  → verified
  - a book reference  (no DOI/arXiv, no hit)    → unverifiable

Asserts the `citationAudit` card payload is attached to the report meta ONLY
because of the suspicious DOI, lists exactly that one entry, and never presents
the unverifiable/verified items as hallucinations. Includes a SOURCE-LEVEL
negative control proving the gating is load-bearing: force `has_suspicious`
false → the card disappears.

NOTHING here hits the network: the verifier's HTTP seam
(`tofu_search.search.vertical.base.http_get`) is mocked, and the report
engine's `dispatch_stream` / `_execute_report_tool` are stubbed (mirrors
tests/test_paper_report_dedup.py).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ── fake HTTP for the verifier ───────────────────────────────────────────────

class _FakeResp:
    def __init__(self, *, status=200, json_data=None, text=''):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


_BAD_DOI = '10.9999/totally-made-up'
_GOOD_ARXIV = '1706.03762'
_FLAKY_DOI = '10.5555/registry-down'   # CrossRef 500 → unverifiable (NOT suspicious)

# NOTE on the extraction seam: the audit harvests only concrete inline
# identifiers (DOI / arXiv id) — never prose titles — so a book reference with
# no id produces NO citation (the safe, false-positive-free behaviour). The
# genuine `unverifiable` path for an id-only citation is a TRANSPORT failure on
# the catalogue (e.g. CrossRef 500), which is what _FLAKY_DOI exercises here.
REPORT_BODY = (
    '## ⚡ TL;DR\nThe Transformer dispenses with recurrence.\n\n'
    '## 📋 Paper Card\n| arXiv / DOI | arXiv:' + _GOOD_ARXIV + ' |\n\n'
    '## 🗺️ Research Landscape\n'
    'A follow-up study reports gains (doi:' + _BAD_DOI + ').\n'
    'A registry-flaky reference (doi:' + _FLAKY_DOI + ') could not be checked.\n'
    'See also the classic textbook Introduction to Algorithms (no id).\n\n'
    '## 📝 Technical Reference\nThe end.\n'
)


def _verifier_router(url, **kw):
    """Route the verifier's lookups deterministically, offline."""
    if 'api.crossref.org/works/' in url:           # Tier-1 DOI resolve
        if _FLAKY_DOI in url:
            return _FakeResp(status=500)           # registry down → unverifiable
        return _FakeResp(status=404)               # bad DOI → suspicious
    if 'export.arxiv.org' in url:                  # Tier-1 arXiv resolve
        return _FakeResp(text=(
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            '<title>Attention Is All You Need</title></entry></feed>'))
    if 'api.crossref.org/works' in url:            # Tier-2 title search (none here)
        return _FakeResp(json_data={'message': {'items': []}})
    if 'semanticscholar.org' in url:               # Tier-2 S2 fallback (none here)
        return _FakeResp(json_data={'data': []})
    return _FakeResp(status=404)


def _install_verifier_http(monkeypatch_target):
    from tofu_search.search.vertical import base
    _orig = base.http_get
    base.http_get = _verifier_router
    return base, _orig


def _patch_dispatch(monkeyplan):
    import lib.paper.report_engine as re_mod
    plan = list(monkeyplan)

    def _fake_dispatch(messages, on_content=None, on_thinking=None, **kw):
        content, tool_calls = plan.pop(0)
        if content and on_content:
            on_content(content)
        msg = {'role': 'assistant', 'content': content, 'tool_calls': tool_calls}
        usage = {'prompt_tokens': 10, 'completion_tokens': 20, '_dispatch': {}}
        return msg, ('tool_calls' if tool_calls else 'stop'), usage

    re_mod.dispatch_stream = _fake_dispatch


def _make_task(tid):
    from lib.paper import _new_report_task
    return _new_report_task(tid, 'phashaudit000000000000000000000', 'en', None,
                            client_title='Attention Is All You Need')


def _run(tid):
    import lib.paper.report_engine as re_mod
    _patch_dispatch([(REPORT_BODY, [])])  # single pass, no tools
    task = _make_task(tid)
    re_mod._run_report_task(task, [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'paper'},
    ], [])
    return task


def test_card_appears_only_for_suspicious_doi():
    import lib.paper.report_engine as re_mod
    orig_disp = re_mod.dispatch_stream
    base, orig_http = _install_verifier_http(None)
    try:
        task = _run('rpt_audit_1')
        assert task['status'] == 'done', task['status']
        meta = task.get('report_meta') or {}
        audit = meta.get('citationAudit')
        assert audit is not None, 'citationAudit MUST be attached (one suspicious DOI)'
        # Three states exercised: bad DOI suspicious, good arXiv verified,
        # flaky-registry DOI unverifiable. Only the suspicious one is flagged.
        assert audit['counts']['suspicious'] == 1, audit['counts']
        assert audit['counts']['verified'] == 1, audit['counts']
        assert audit['counts']['unverifiable'] == 1, audit['counts']
        susp = audit['suspicious']
        assert len(susp) == 1
        assert _BAD_DOI in susp[0]['identifier']
        assert susp[0]['kind'] == 'DOI'
        # The good arXiv id must never appear in the suspicious list.
        assert all(_GOOD_ARXIV not in s['identifier'] for s in susp)
        # The done event carries the same meta (frontend gating source).
        done = [e for e in task['events'] if e.get('type') == 'done'][-1]
        assert done['meta'].get('citationAudit') is not None
    finally:
        base.http_get = orig_http
        re_mod.dispatch_stream = orig_disp
    _ok('card attached only because of the suspicious DOI; good arXiv stays verified')


def test_all_clean_attaches_no_card():
    """When every cited id resolves, no card is attached (gating works)."""
    import lib.paper.report_engine as re_mod
    orig_disp = re_mod.dispatch_stream

    def _all_ok(url, **kw):
        if 'api.crossref.org/works/' in url:
            return _FakeResp(json_data={'message': {'title': ['Some Real Paper']}})
        if 'export.arxiv.org' in url:
            return _FakeResp(text=('<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                                   '<title>Attention Is All You Need</title></entry></feed>'))
        return _FakeResp(status=404)
    from tofu_search.search.vertical import base
    orig_http = base.http_get
    base.http_get = _all_ok
    try:
        task = _run('rpt_audit_2')
        meta = task.get('report_meta') or {}
        assert meta.get('citationAudit') is None, 'no card when nothing is suspicious'
    finally:
        base.http_get = orig_http
        re_mod.dispatch_stream = orig_disp
    _ok('all-clear run attaches NO card')


def test_negctl_force_no_suspicious_removes_card():
    """SOURCE-LEVEL negative control: monkeypatch summarize to report
    has_suspicious=False → build_citation_audit returns None → no card,
    proving the gating (not mere presence of identifiers) drives the card."""
    import lib.paper.citation_audit as ca_mod
    import lib.paper.report_engine as re_mod
    orig_disp = re_mod.dispatch_stream
    from tofu_search.search.vertical import base
    orig_http = base.http_get
    base.http_get = _verifier_router

    import tofu_search.verify as _v

    # build_citation_audit imports `summarize` from tofu_search.verify inside
    # the function body, so patch it on that module. Capture the original
    # FIRST and call it (not the rebound name) to avoid self-recursion.
    orig_v_sum = _v.summarize

    def _force_clean(results):
        s = orig_v_sum(results)
        s['has_suspicious'] = False
        s['suspicious'] = []
        return s
    _v.summarize = _force_clean
    try:
        task = _run('rpt_audit_3')
        meta = task.get('report_meta') or {}
        assert meta.get('citationAudit') is None, \
            'forcing has_suspicious=False MUST remove the card'
    finally:
        _v.summarize = orig_v_sum
        base.http_get = orig_http
        re_mod.dispatch_stream = orig_disp
    # Restore sanity: with the real summarize, the card returns.
    base.http_get = _verifier_router
    try:
        task2 = _run('rpt_audit_3b')
        assert (task2.get('report_meta') or {}).get('citationAudit') is not None, \
            'card must return once gating is restored'
    finally:
        base.http_get = orig_http
    _ok('negative control: forcing has_suspicious=False removes the card; restore brings it back')


def main():
    print()
    print(_color('═══ Paper Report Citation-Audit Tests ═══', '36'))
    print()
    tests = [
        test_card_appears_only_for_suspicious_doi,
        test_all_clean_attaches_no_card,
        test_negctl_force_no_suspicious_removes_card,
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
