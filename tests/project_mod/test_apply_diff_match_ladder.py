"""The apply_diff matching ladder's relative-indent rungs (Tiers 5-6).

Exact-first matching already covered CRLF / trailing-whitespace /
unicode-escape normalization. These tests pin the uniform-indent rung
(aider's relative-indent tolerance): one uniform prefix offset, unique
match, and re-framing of the replacement into the file's own indentation —
plus the ambiguity guard and undo round-trip.
"""
import pytest

from lib.project_mod.write_tools import _ops
from lib.project_mod import modifications as mods

pytestmark = pytest.mark.unit


def _base(tmp_path, monkeypatch, content):
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'journals'))
    base = tmp_path / 'project'
    base.mkdir()
    target = base / 'file.txt'
    target.write_text(content, encoding='utf-8')
    return str(base), target


class TestIndentRung:
    def test_outdented_search_splices_with_file_indent(self, tmp_path, monkeypatch):
        base, target = _base(tmp_path, monkeypatch,
                             'def f():\n    x = 1\n    return x\n')
        result = _ops.tool_apply_diff(
            base, 'file.txt',
            # Model emitted the body at column 0; the file indents by 4.
            'x = 1\nreturn x',
            'x = 2\nreturn x')
        assert result['ok'], result
        assert target.read_text() == 'def f():\n    x = 2\n    return x\n'

    def test_replacement_relative_indent_is_preserved(self, tmp_path, monkeypatch):
        base, target = _base(tmp_path, monkeypatch,
                             'def f():\n    if ok:\n        go()\n')
        result = _ops.tool_apply_diff(
            base, 'file.txt',
            'if ok:\n    go()',
            'if ok:\n    go()\n    done()')
        assert result['ok'], result
        assert target.read_text() == 'def f():\n    if ok:\n        go()\n        done()\n'

    def test_overindented_search_is_stripped_to_file_frame(self, tmp_path, monkeypatch):
        base, target = _base(tmp_path, monkeypatch, 'x = 1\ny = 2\n')
        result = _ops.tool_apply_diff(
            base, 'file.txt',
            '    x = 1\n    y = 2',
            '    x = 10\n    y = 20')
        assert result['ok'], result
        assert target.read_text() == 'x = 10\ny = 20\n'

    def test_indent_rung_preserves_original_anchor_for_undo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
        monkeypatch.setattr(_ops, '_mod_attribution', lambda t, root, rel, **kw: (root, rel))
        base, target = _base(tmp_path, monkeypatch,
                             'def f():\n    x = 1\n    return x\n')
        result = _ops.tool_apply_diff(
            base, 'file.txt', 'x = 1\nreturn x', 'x = 2\nreturn x', task_id='task')
        assert result['ok'], result
        undone = mods.undo_task_modifications(base, 'task')
        assert undone['failed'] == 0, undone
        assert target.read_text() == 'def f():\n    x = 1\n    return x\n'

    def test_ambiguous_indent_match_errors_with_locations(self, tmp_path, monkeypatch):
        base, target = _base(tmp_path, monkeypatch,
                             'def f():\n    go()\n    back()\n\ndef g():\n    go()\n    back()\n')
        # Multi-line: exact and trailing-WS rungs miss (file indents every
        # line), the indent rung sees two identical stripped windows.
        result = _ops.tool_apply_diff(base, 'file.txt', 'go()\nback()', 'stop()')
        assert not result['ok']
        assert result['errorKind'] == 'ambiguous-match'
        assert 'indentation' in result['error']
        assert 'Match 1' in result['error']
        # Nothing was written.
        assert target.read_text() == 'def f():\n    go()\n    back()\n\ndef g():\n    go()\n    back()\n'

    def test_replace_all_reframes_each_match_to_its_own_indent(self, tmp_path, monkeypatch):
        base, target = _base(tmp_path, monkeypatch,
                             'def f():\n    go()\n\ndef g():\n        go()\n')
        result = _ops.tool_apply_diff(
            base, 'file.txt', 'go()', 'stop()', replace_all=True)
        assert result['ok'], result
        assert result.get('replacedCount') == 2
        assert target.read_text() == 'def f():\n    stop()\n\ndef g():\n        stop()\n'

    def test_exact_match_still_wins_over_indent_rung(self, tmp_path, monkeypatch):
        base, target = _base(tmp_path, monkeypatch, '    x = 1\n')
        result = _ops.tool_apply_diff(base, 'file.txt', '    x = 1', '    x = 2')
        assert result['ok'], result
        # Exact path: no re-indentation side effects.
        assert target.read_text() == '    x = 2\n'

    def test_combined_unicode_escape_and_indent_drift(self, tmp_path, monkeypatch):
        base, target = _base(tmp_path, monkeypatch,
                             'def f():\n    s = "\\u2014"\n')
        result = _ops.tool_apply_diff(
            base, 'file.txt', 's = "—"', 's = "—!"')
        assert result['ok'], result
        assert target.read_text() == 'def f():\n    s = "—!"\n'

    def test_clean_miss_keeps_closest_match_hint(self, tmp_path, monkeypatch):
        base, target = _base(tmp_path, monkeypatch, 'alpha\nbeta\ngamma\n')
        result = _ops.tool_apply_diff(base, 'file.txt', 'alpah\nbtea', 'x\ny')
        assert not result['ok']
        assert result['errorKind'] == 'match-not-found'
        assert 'Most similar block' in result['error']

    def test_blank_search_never_matches_indent_rung(self, tmp_path, monkeypatch):
        # Direct unit check of the rung's blank guard: a whitespace-only
        # search must never turn into an arbitrary blank-window splice.
        outcome = _ops._indent_tolerant_splice(
            'a\n\nb\n', '  \n  ', 'x\ny', False, decode=False, rel_path='f')
        assert outcome == {'ok': False}


@pytest.mark.parametrize(('source', 'anchor'), [
    ('    if ready:\n        run()\n    done()\n', 'if ready:\nrun()\ndone()'),
    ('    first()\n    second()\n', 'first()\n   second()'),
    ('    first()\n        second()\n', '  first()\n  second()'),
    ('\tfirst()\n\tsecond()\n', 'first()\n  \tsecond()'),
    ('\tfirst()\n    second()\n', 'first()\nsecond()'),
    ('    label = "\\u2014"\n        emit()\n', 'label = "—"\nemit()'),
    ('    label = "\\n"\n    emit()\n', 'label = "\n"\nemit()'),
])
def test_independent_line_deindent_and_escape_attacks_never_match(
        tmp_path, monkeypatch, source, anchor):
    base, target = _base(tmp_path, monkeypatch, source)
    result = _ops.tool_edit_file(base, [{
        'path': 'file.txt', 'operation': 'replace', 'anchor': anchor, 'content': 'WRONG',
    }])
    assert result.startswith('Applied 0/1 edits'), result
    assert '[match-not-found]' in result
    assert target.read_text() == source


@pytest.mark.parametrize(('source', 'anchor', 'replacement', 'expected'), [
    ('    first()\n        second()\n', '  first()\n      second()',
     '  changed()\n      second()', '    changed()\n        second()\n'),
    ('  first()\n      second()\n', '    first()\n        second()',
     '    changed()\n        second()', '  changed()\n      second()\n'),
    ('\tfirst()\n\t\tsecond()\n', 'first()\n\tsecond()',
     'changed()\n\tsecond()', '\tchanged()\n\t\tsecond()\n'),
    ('    label = "\\u2014"\n    emit()\n', 'label = "—"\nemit()',
     'label = "—!"\nemit()', '    label = "—!"\n    emit()\n'),
])
def test_one_uniform_offset_preserves_relative_structure(
        tmp_path, monkeypatch, source, anchor, replacement, expected):
    base, target = _base(tmp_path, monkeypatch, source)
    result = _ops.tool_edit_file(base, [{
        'path': 'file.txt', 'operation': 'replace', 'anchor': anchor, 'content': replacement,
    }])
    assert result.startswith('Applied 1/1 edits'), result
    assert target.read_text() == expected


def test_invalid_indentation_candidate_does_not_create_false_ambiguity(tmp_path, monkeypatch):
    source = '    first()\n        second()\n\n    first()\n    second()\n'
    base, target = _base(tmp_path, monkeypatch, source)
    result = _ops.tool_apply_diff(base, 'file.txt', 'first()\nsecond()', 'changed()')
    assert result['ok'], result
    assert target.read_text() == '    first()\n        second()\n\n    changed()\n'


def test_uniform_offset_rejects_replacement_that_cannot_keep_relative_indent(tmp_path, monkeypatch):
    source = 'first()\nsecond()\n'
    base, target = _base(tmp_path, monkeypatch, source)
    result = _ops.tool_apply_diff(base, 'file.txt', '    first()\n    second()',
                                  '    changed()\n  broken_structure()')
    assert not result['ok'], result
    assert target.read_text() == source
