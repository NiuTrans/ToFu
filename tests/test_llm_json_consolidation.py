#!/usr/bin/env python3
"""Behavior-lock tests for the LLM-JSON-extraction consolidation.

Three subsystems each hand-rolled their own "parse JSON out of an LLM reply"
logic (fence strip + balanced-brace scan + truncation repair) — a duplicate of
what ``lib/llm_json.py`` already provides. This suite pins the observable
behavior of each call site so the consolidation onto ``lib.llm_json`` is proven
byte-for-byte equivalent, and guards that the deleted clone stays deleted.

Covered call sites:
  * ``lib.daily_report.llm._extract_json_result``  (dict / list / truncated-repair)
  * ``lib.paper.terminology_backfill._parse_json_obj`` (dict-only extraction)
  * ``lib.memory.profile_consolidate._parse_actions``  (actions-list extraction)

Run directly (``python tests/test_llm_json_consolidation.py``) or via pytest.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ── daily_report._extract_json_result ────────────────────────────────

def test_daily_report_dict_format():
    from lib.daily_report.llm import _extract_json_result
    src = '{"streams": [{"title": "x"}], "tomorrow": ["y"], "yesterday_done": ["z"]}'
    streams, tomorrow, yd = _extract_json_result(src)
    assert streams == [{'title': 'x'}] and tomorrow == ['y'] and yd == ['z']
    _ok('daily_report: new dict format {streams,tomorrow,yesterday_done}')


def test_daily_report_fenced_and_prose():
    from lib.daily_report.llm import _extract_json_result
    src = 'Here you go:\n```json\n{"streams": [{"title": "a"}]}\n```\nDone!'
    streams, tomorrow, yd = _extract_json_result(src)
    assert streams == [{'title': 'a'}] and tomorrow == [] and yd == []
    _ok('daily_report: fenced + prose-wrapped dict')


def test_daily_report_legacy_list_format():
    from lib.daily_report.llm import _extract_json_result
    streams, tomorrow, yd = _extract_json_result('[{"title": "legacy"}]')
    assert streams == [{'title': 'legacy'}] and tomorrow == [] and yd == []
    _ok('daily_report: legacy top-level list format')


def test_daily_report_truncated_repair():
    from lib.daily_report.llm import _extract_json_result
    # Truncated mid-generation after a COMPLETE token (no closing brackets) →
    # the fragment up to the last complete value is salvaged.
    src = '{"streams": [{"title": "x", "done": true'
    streams, _t, _y = _extract_json_result(src)
    assert streams and streams[0].get('title') == 'x'
    _ok('daily_report: salvages truncated JSON output')


def test_daily_report_garbage_returns_empty():
    from lib.daily_report.llm import _extract_json_result
    assert _extract_json_result('not json at all') == ([], [], [])
    assert _extract_json_result('') == ([], [], [])
    assert _extract_json_result(None) == ([], [], [])
    _ok('daily_report: garbage / empty / None → ([],[],[])')


def test_daily_report_clone_deleted():
    """The local _repair_truncated_json clone must be gone (redundancy removed)."""
    import lib.daily_report.llm as m
    assert not hasattr(m, '_repair_truncated_json'), \
        '_repair_truncated_json clone should be deleted — use lib.llm_json'
    _ok('daily_report: local _repair_truncated_json clone deleted')


# ── paper.terminology_backfill._parse_json_obj ───────────────────────

def test_termfill_fenced_dict():
    from lib.paper.terminology_backfill import _parse_json_obj
    assert _parse_json_obj('```json\n{"BERT": "a model"}\n```') == {'BERT': 'a model'}
    _ok('termfill: fenced dict parsed')


def test_termfill_prose_wrapped_dict():
    from lib.paper.terminology_backfill import _parse_json_obj
    src = 'Sure:\n{"MLP": "multilayer perceptron"}\nhope that helps'
    assert _parse_json_obj(src) == {'MLP': 'multilayer perceptron'}
    _ok('termfill: prose-wrapped dict parsed')


def test_termfill_non_dict_and_garbage():
    from lib.paper.terminology_backfill import _parse_json_obj
    assert _parse_json_obj('[1, 2, 3]') == {}   # list is not a definitions object
    assert _parse_json_obj('garbage') == {}
    assert _parse_json_obj('') == {}
    _ok('termfill: non-dict / garbage / empty → {}')


# ── memory.profile_consolidate._parse_actions ────────────────────────

def test_profile_fenced_actions():
    from lib.memory.profile_consolidate import _parse_actions
    src = '```json\n{"actions": [{"kind": "new", "text": "likes Rust"}]}\n```'
    acts = _parse_actions(src)
    assert acts == [{'kind': 'new', 'text': 'likes Rust'}]
    _ok('profile_consolidate: fenced actions list')


def test_profile_prose_and_fence():
    from lib.memory.profile_consolidate import _parse_actions
    src = 'Result:\n```json\n{"actions": []}\n```'
    assert _parse_actions(src) == []
    _ok('profile_consolidate: prose + fence, empty actions')


def test_profile_missing_actions_and_garbage():
    from lib.memory.profile_consolidate import _parse_actions
    assert _parse_actions('{"other": 1}') == []   # no "actions" key
    assert _parse_actions('not json') == []
    assert _parse_actions('') == []
    _ok('profile_consolidate: missing-key / garbage / empty → []')


def main():
    print()
    print(_color('═══ LLM-JSON Consolidation Behavior-Lock Tests ═══', '36'))
    print()
    tests = [
        test_daily_report_dict_format,
        test_daily_report_fenced_and_prose,
        test_daily_report_legacy_list_format,
        test_daily_report_truncated_repair,
        test_daily_report_garbage_returns_empty,
        test_daily_report_clone_deleted,
        test_termfill_fenced_dict,
        test_termfill_prose_wrapped_dict,
        test_termfill_non_dict_and_garbage,
        test_profile_fenced_actions,
        test_profile_prose_and_fence,
        test_profile_missing_actions_and_garbage,
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
