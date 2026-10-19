import pytest
from html import escape
from lib.tasks_pkg.message_input_budget import select_whole_messages

pytestmark = pytest.mark.unit


def test_escaping_is_budgeted_without_truncating_original(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens._get_context_limit', lambda task: 128000)
    rows = [{'content': '&' * 4096}, {'content': 'short'}]
    selected, blocks = select_whole_messages({}, [], rows, lambda row: escape(row['content']), max_bytes=1024)
    assert selected == [rows[1]] and blocks == ['short']
    assert len(rows[0]['content']) == 4096


def test_existing_context_headroom_defers_whole_messages(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens._get_context_limit', lambda task: 1000)
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens._estimate_total_tokens', lambda messages: 800)
    assert select_whole_messages({}, [], [{'content': 'short'}], lambda row: row['content']) == ([], [])


def test_no_tail_clipping_when_a_batch_runs_out_of_tokens(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens._get_context_limit', lambda task: 128000)
    monkeypatch.setattr('lib.tasks_pkg.context_composer._render._count_tokens', lambda text, model: len(text))
    rows = [{'content': 'abc'}, {'content': 'def'}, {'content': 'g'}]
    selected, blocks = select_whole_messages({}, [], rows, lambda row: row['content'], max_tokens=4)
    assert selected == [rows[0], rows[2]] and blocks == ['abc', 'g']


@pytest.mark.parametrize('max_messages, expected', [(0, []), (1, ['a']), (2, ['a', 'b']), (None, ['a', 'b', 'c'])])
def test_count_limit_applies_to_fitting_originals_not_scanned_prefix(monkeypatch, max_messages, expected):
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens._get_context_limit', lambda task: 128000)
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens._estimate_total_tokens', lambda messages: 0)
    monkeypatch.setattr('lib.tasks_pkg.context_composer._render._count_tokens', lambda text, model: len(text))
    rows = [{'content': text} for text in ['too large'] * 8 + ['a', 'too large', 'b', 'c']]
    rendered = []
    def render(row):
        rendered.append(row)
        return row['content']
    selected, blocks = select_whole_messages({}, [], iter(rows), render, max_tokens=4, max_messages=max_messages)
    assert [row['content'] for row in selected] == blocks == expected
    assert all(any(row is original for original in rows) for row in selected)
    if max_messages == 0:
        assert rendered == []
    elif max_messages is not None:
        assert rendered[-1] is selected[-1]  # No work after the fitted count is reached.


def test_utf8_byte_sum_defers_whole_rows_and_keeps_scanning(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens._get_context_limit', lambda task: 128000)
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens._estimate_total_tokens', lambda messages: 0)
    counted = []
    monkeypatch.setattr('lib.tasks_pkg.context_composer._render._count_tokens', lambda text, model: counted.append(text) or 1)
    rows = [{'content': text} for text in ['🙂🙂', '雪', '🙂', 'a', 'b']]
    selected, blocks = select_whole_messages({}, [], rows, lambda row: row['content'], max_bytes=5, max_messages=2)
    assert selected == [rows[1], rows[3]] and blocks == ['雪', 'a']
    assert sum(len(block.encode('utf-8')) for block in blocks) <= 5
    assert '🙂🙂' not in counted  # Byte rejection does not need tokenization.


@pytest.mark.parametrize('context_limit, existing_tokens, expected', [(64, 0, ['aaaa']), (1000, 745, ['aaaa', 'b'])])
def test_context_fraction_and_remaining_headroom_still_bound_count_selection(monkeypatch, context_limit, existing_tokens, expected):
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens._get_context_limit', lambda task: context_limit)
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens._estimate_total_tokens', lambda messages: existing_tokens)
    monkeypatch.setattr('lib.tasks_pkg.context_composer._render._count_tokens', lambda text, model: len(text))
    rows = [{'content': text} for text in ['oversized', 'aaaa', 'bb', 'b']]
    selected, blocks = select_whole_messages({}, [], rows, lambda row: row['content'], max_messages=8)
    assert blocks == expected and selected == [row for row in rows if row['content'] in expected]
