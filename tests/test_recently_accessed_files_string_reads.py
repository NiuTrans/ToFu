"""Regression: modified-files reminder rendered "one letter per line".

Root cause (conv mr4e8pnxbv440z): a ``read_files`` tool call was stored with
its ``reads`` argument as a JSON *string* (sometimes truncated/malformed)
instead of a list — e.g.::

    reads = '[{"path": "human_eval_web/materials/web_items.py", "end_line": 418]'

``_extract_recently_accessed_files`` iterated ``for spec in reads`` over that
string, yielding single CHARACTERS; the ``isinstance(spec, str)`` branch then
accepted each char as a "file path". They were deduped by a set and rendered
one-per-line by ``attachments._get_modified_files_attachment`` — producing the
notorious ``  - [`` / ``  - {`` / ``  - p`` … reminder in the debug panel.

The fix coerces a string-typed list arg via ``json.loads`` (skipping it when it
does not decode to a list) and drops the char-accepting ``str`` spec branch.
"""

import json

from lib.tasks_pkg.compaction._layer2 import _extract_recently_accessed_files


def _read_msg(reads_value):
    """Assistant msg with a read_files tool_call whose args JSON has `reads`."""
    return {
        'role': 'assistant',
        'tool_calls': [{
            'function': {
                'name': 'read_files',
                'arguments': json.dumps({'reads': reads_value}),
            },
        }],
    }


def test_malformed_string_reads_yields_no_char_garbage():
    # The exact production shape: a truncated JSON string (no closing brace).
    bad = '[{"path": "human_eval_web/materials/web_items.py", "end_line": 418]'
    files = _extract_recently_accessed_files([_read_msg(bad)], max_files=5)
    assert files == [], f'expected no files, got {files!r}'
    # The specific failure signature: single-character "paths".
    assert not any(len(f) == 1 for f in files), \
        f'char-by-char iteration leaked: {files!r}'


def test_valid_json_string_reads_recovers_path():
    # A well-formed list encoded as a string still yields the real path.
    good = json.dumps([{'path': 'a/b.py', 'start_line': 1}])
    files = _extract_recently_accessed_files([_read_msg(good)], max_files=5)
    assert files == ['a/b.py'], files


def test_normal_list_reads_unchanged():
    files = _extract_recently_accessed_files(
        [_read_msg([{'path': 'x/y.py'}, {'path': 'z.py'}])], max_files=5)
    assert files == ['x/y.py', 'z.py'], files


def test_bare_string_list_elements_preserved():
    # Documented Claude-Opus shape: `reads` is a LIST whose ELEMENTS are bare
    # string paths (not {"path": ...} dicts). The tool executor normalizes
    # these so the read succeeds; the compaction re-scan MUST likewise keep
    # them — each element is a real full path, never a stray character.
    files = _extract_recently_accessed_files(
        [_read_msg(['human_eval/foo.py', 'scripts/bar.py'])], max_files=5)
    assert files == ['human_eval/foo.py', 'scripts/bar.py'], files


def test_mixed_dict_and_string_list_elements():
    # A list mixing both element shapes — both must be captured, in order.
    files = _extract_recently_accessed_files(
        [_read_msg([{'path': 'a/first.py'}, 'a/second.py'])], max_files=5)
    assert files == ['a/first.py', 'a/second.py'], files


def test_non_dict_args_skipped():
    # arguments decode to a bare string (not a dict) — must not raise.
    msg = {'role': 'assistant', 'tool_calls': [{
        'function': {'name': 'read_files', 'arguments': '"just a string"'}}]}
    assert _extract_recently_accessed_files([msg], max_files=5) == []


def test_string_edits_for_apply_diffs_no_char_garbage():
    # Same defect class on the apply_diffs/insert_contents `edits` arg.
    bad = '[{"path": "lib/foo.py"'  # truncated string
    msg = {'role': 'assistant', 'tool_calls': [{
        'function': {'name': 'apply_diffs',
                     'arguments': json.dumps({'edits': bad})}}]}
    files = _extract_recently_accessed_files([msg], max_files=5)
    assert files == [], files


if __name__ == '__main__':
    test_malformed_string_reads_yields_no_char_garbage()
    test_valid_json_string_reads_recovers_path()
    test_normal_list_reads_unchanged()
    test_bare_string_list_elements_preserved()
    test_mixed_dict_and_string_list_elements()
    test_non_dict_args_skipped()
    test_string_edits_for_apply_diffs_no_char_garbage()
    print('all tests passed')
