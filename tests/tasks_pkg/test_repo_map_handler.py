"""Real repository tool results, project attribution and terminal settlement."""
import pytest

pytestmark = pytest.mark.unit


def drive(monkeypatch, root, arguments=None, *, scanner=None):
    from lib.tasks_pkg.handlers import project

    settled = []

    def record(task, round_number, round_entry, results, **kwargs):
        settled.append((results, kwargs.get('status', 'done')))

    monkeypatch.setattr(project, '_finalize_tool_round', record)
    result = project._handle_project_tool(
        {'id': 'repo-map-test-task', 'messages': [], 'toolRounds': []}, {},
        'repo_map', 'repo-map-call', arguments or {}, 1, {}, None,
        str(root) if root else '', True,
        repository_scanner=scanner,
    )
    assert len(settled) == 1
    return result, settled[0]


def test_multilingual_tool_uses_attached_root_and_is_read_only(tmp_path, monkeypatch):
    attached = tmp_path / 'attached'
    sibling = tmp_path / 'sibling'
    attached.mkdir()
    sibling.mkdir()
    sources = {'module.js': b'function attachedJavascript() {}\n',
               'library.rs': b'fn attachedRust() {}\n'}
    for name, content in sources.items():
        (attached / name).write_bytes(content)
    (sibling / 'private.js').write_text('function siblingPrivate() {}')
    result, (metadata, status) = drive(monkeypatch, attached)
    call_id, text, failed = result
    assert call_id == 'repo-map-call' and not failed and status == 'done'
    assert 'attachedJavascript' in text and 'attachedRust' in text
    assert 'siblingPrivate' not in text
    assert metadata[0]['stats']['files_shown'] == metadata[0]['stats']['files_total'] == 2
    assert {path.name: path.read_bytes() for path in attached.iterdir()} == sources


def test_tool_path_filter_keeps_result_and_metadata_consistent(tmp_path, monkeypatch):
    (tmp_path / 'selected').mkdir()
    (tmp_path / 'selected/module.js').write_text('function selectedSymbol() {}')
    (tmp_path / 'ignored.js').write_text('function outsideFilter() {}')
    result, (metadata, status) = drive(monkeypatch, tmp_path, {'path': 'selected', 'max_tokens': 512})
    assert not result[2] and status == 'done'
    assert 'selectedSymbol' in result[1] and 'outsideFilter' not in result[1]
    assert metadata[0]['stats']['files_total'] == metadata[0]['stats']['files_shown'] == 1


def test_unattached_tool_settles_as_error(tmp_path, monkeypatch):
    result, (_, status) = drive(monkeypatch, None)
    assert result[2] and status == 'error'
    assert 'requires an attached project root' in result[1]


def test_server_owned_scanner_reaches_public_api_and_is_borrowed(tmp_path, monkeypatch):
    from lib.repo_map.outline import scan
    (tmp_path/'module.py').write_text('def owned_symbol():\n    return 1\n')
    calls = []
    class BorrowedScanner:
        closed = False
        def __call__(self, root, **options):
            calls.append(root)
            assert root == str(tmp_path)
            return scan(root, **options)
        def close(self):
            self.closed = True
    scanner = BorrowedScanner()
    expected = drive(monkeypatch, tmp_path)
    actual = drive(monkeypatch, tmp_path, {'scanner': 'untrusted wire value'}, scanner=scanner)
    assert actual == expected
    assert calls == [str(tmp_path)] and not scanner.closed


def test_scanner_denial_uses_existing_single_error_settlement(tmp_path, monkeypatch):
    calls = []
    def denied(root, **options):
        calls.append(root)
        raise PermissionError('foreign repository scope')
    result, (_, status) = drive(monkeypatch, tmp_path, scanner=denied)
    assert result[2] and status == 'error' and calls == [str(tmp_path)]
    assert 'foreign repository scope' in result[1]
