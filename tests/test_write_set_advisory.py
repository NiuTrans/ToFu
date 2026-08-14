"""Tests for the write-set advisory (``lib/write_set_advisory.py``).

Advisory-only sibling of the freshness gate: when a conversation writes a
path that matches NONE of its claimed epics' write_set entries, the drift
must become VISIBLE (WARNING + audit + project-feed note) — never blocked.
Fail-open everywhere else: no claims → silence; tag-only write_set →
silence; board failure → silence; same (conv, path) warns once.
"""
from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'docs/a.md', 'lib/bar.py', 'lib/foo.py', 'lib/sub/deep/y.py',
    'tests/test_x1.py', 'tests/test_z.py',
}

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    from lib import write_set_advisory as wsa
    wsa._reset_caches()
    feed = []
    board = {'tasks': []}
    monkeypatch.setattr(
        'lib.conversations.project_board.read_board', lambda p: board)
    monkeypatch.setattr(
        'lib.conversations.project_feed.emit_project_event',
        lambda *a, **k: feed.append((a, k)) or {'seq': 1})
    yield {'feed': feed, 'board': board}
    wsa._reset_caches()


def _claim(board, conv='convA', write_set=None, status='claimed'):
    board['tasks'] = [{
        'id': 'pt_test', 'kind': 'epic', 'status': status,
        'owner_conv_id': conv, 'write_set': list(write_set or []),
    }]


@pytest.mark.unit
def test_write_inside_write_set_is_silent(tmp_path, _isolate, caplog):
    from lib import write_set_advisory as wsa
    _claim(_isolate['board'], write_set=['lib/foo.py', 'docs/'])
    with caplog.at_level('WARNING'):
        assert wsa.note_project_write('convA', str(tmp_path), 'lib/foo.py') is False
        assert wsa.note_project_write('convA', str(tmp_path), 'docs/a.md') is False
    assert _isolate['feed'] == []
    assert 'WriteSetAdvisory' not in caplog.text


@pytest.mark.unit
def test_write_outside_warns_and_feeds_once(tmp_path, _isolate, caplog):
    from lib import write_set_advisory as wsa
    _claim(_isolate['board'], write_set=['lib/foo.py'])
    with caplog.at_level('WARNING'):
        assert wsa.note_project_write('convA', str(tmp_path), 'lib/bar.py') is True
        # Second write of the same path is throttled (warn-once).
        assert wsa.note_project_write('convA', str(tmp_path), 'lib/bar.py') is False
    assert len(_isolate['feed']) == 1
    args, kwargs = _isolate['feed'][0]
    assert args[1] == 'convA' and args[2] == 'note'
    assert 'lib/bar.py' in args[3]
    assert 'OUTSIDE' in caplog.text
    assert 'lib/bar.py' in caplog.text


@pytest.mark.unit
def test_no_claims_is_silent(tmp_path, _isolate):
    from lib import write_set_advisory as wsa
    # board empty (fixture default)
    assert wsa.note_project_write('convA', str(tmp_path), 'lib/bar.py') is False
    assert _isolate['feed'] == []


@pytest.mark.unit
def test_other_convs_claims_dont_bind_me(tmp_path, _isolate):
    from lib import write_set_advisory as wsa
    _claim(_isolate['board'], conv='convB', write_set=['lib/foo.py'])
    # convA claims nothing → silence even for an 'outside' path.
    assert wsa.note_project_write('convA', str(tmp_path), 'lib/bar.py') is False
    assert _isolate['feed'] == []


@pytest.mark.unit
def test_tag_only_write_set_is_silent(tmp_path, _isolate):
    from lib import write_set_advisory as wsa
    _claim(_isolate['board'], write_set=['frontend', 'billing'])
    assert wsa.note_project_write('convA', str(tmp_path), 'lib/bar.py') is False
    assert _isolate['feed'] == []


@pytest.mark.unit
def test_glob_and_dir_entries_match(tmp_path, _isolate):
    from lib import write_set_advisory as wsa
    _claim(_isolate['board'], write_set=['tests/test_x*.py', 'lib/sub/'])
    assert wsa.note_project_write('convA', str(tmp_path), 'tests/test_x1.py') is False
    assert wsa.note_project_write('convA', str(tmp_path), 'lib/sub/deep/y.py') is False
    assert wsa.note_project_write('convA', str(tmp_path), 'tests/test_z.py') is True


@pytest.mark.unit
def test_abs_path_inside_project_judged_outside_project_skipped(tmp_path, _isolate):
    from lib import write_set_advisory as wsa
    _claim(_isolate['board'], write_set=['lib/foo.py'])
    proj = str(tmp_path)
    inside = os.path.join(proj, 'lib', 'bar.py')
    outside = os.path.join(os.sep, 'elsewhere', 'baz.py')
    assert wsa.note_project_write('convA', proj, inside) is True
    assert wsa.note_project_write('convA', proj, outside) is False
    assert len(_isolate['feed']) == 1


@pytest.mark.unit
def test_board_failure_is_silence(tmp_path, monkeypatch):
    from lib import write_set_advisory as wsa
    wsa._reset_caches()

    def boom(p):
        raise RuntimeError('db down')

    monkeypatch.setattr('lib.conversations.project_board.read_board', boom)
    # Must not raise, must not warn-as-drift (fail-open).
    assert wsa.note_project_write('convA', str(tmp_path), 'lib/bar.py') is False


@pytest.mark.unit
def test_done_epics_dont_bind(tmp_path, _isolate):
    from lib import write_set_advisory as wsa
    _claim(_isolate['board'], write_set=['lib/foo.py'], status='done')
    assert wsa.note_project_write('convA', str(tmp_path), 'lib/bar.py') is False
    assert _isolate['feed'] == []


@pytest.mark.unit
def test_ops_hook_fires_on_real_write(tmp_path, _isolate, caplog):
    """End-to-end through the real write path: tool_write_file with a conv
    that claims an epic whose write_set does NOT cover the target."""
    from lib.project_mod.write_tools import tool_write_file
    proj = tmp_path / 'proj'
    proj.mkdir()
    _claim(_isolate['board'], write_set=['allowed.py'])
    with caplog.at_level('WARNING'):
        r = tool_write_file(str(proj), 'other.py', 'x = 1\n', conv_id='convA')
    assert r['ok'], r
    assert 'OUTSIDE' in caplog.text
    assert 'other.py' in caplog.text
    assert len(_isolate['feed']) == 1


@pytest.mark.unit
def test_ops_hook_silent_when_path_covered(tmp_path, _isolate, caplog):
    from lib.project_mod.write_tools import tool_write_file
    proj = tmp_path / 'proj'
    proj.mkdir()
    _claim(_isolate['board'], write_set=['allowed.py'])
    with caplog.at_level('WARNING'):
        r = tool_write_file(str(proj), 'allowed.py', 'x = 1\n', conv_id='convA')
    assert r['ok'], r
    assert 'OUTSIDE' not in caplog.text
    assert _isolate['feed'] == []
