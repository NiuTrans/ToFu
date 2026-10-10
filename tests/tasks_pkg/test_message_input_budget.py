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
