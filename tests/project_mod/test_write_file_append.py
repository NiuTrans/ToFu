"""Executable spec for write_file append=true (Amazon Q inspiration, #5).

Pins:
- append concatenates onto existing content (no implicit separator);
- append to a missing file creates it;
- the freshness token is recorded for the FINAL bytes — a second write in the
  same conversation immediately after an append must not be refused as stale;
- the success message keeps the 'File created:'/'File updated:' prefixes that
  tools/meta.py and the tool-loop breaker pattern-match;
- the remote-worktree route fails closed on append (an older desktop build
  would silently overwrite).
"""
import os
import pytest

from lib.project_mod.write_tools import tool_write_file
from lib.project_mod.tools import _exec_write_file


@pytest.fixture
def proj(tmp_path):
    return str(tmp_path)


def test_append_concatenates_existing_content(proj):
    tool_write_file(proj, 'a.txt', 'one\n', conv_id='c-append')
    res = tool_write_file(proj, 'a.txt', 'two\n', conv_id='c-append',
                          append=True)
    assert res['ok'] and res['appended'] is True
    assert res['created'] is False and res['oldLines'] == 2
    assert res['lines'] == 3
    assert res['appendedBytes'] == len('two\n'.encode())
    with open(os.path.join(proj, 'a.txt')) as f:
        assert f.read() == 'one\ntwo\n'


def test_append_missing_file_creates(proj):
    res = tool_write_file(proj, 'new.txt', 'hello\n', conv_id='c-append',
                          append=True)
    assert res['ok'] and res['created'] is True and res['appended'] is True
    with open(os.path.join(proj, 'new.txt')) as f:
        assert f.read() == 'hello\n'


def test_second_write_same_conv_after_append_not_refused(proj):
    """The freshness token must hash the FINAL bytes (old+new), else the next
    write in this conversation is refused as stale."""
    tool_write_file(proj, 'b.txt', '1\n', conv_id='c-append')
    r1 = tool_write_file(proj, 'b.txt', '2\n', conv_id='c-append', append=True)
    r2 = tool_write_file(proj, 'b.txt', '3\n', conv_id='c-append', append=True)
    assert r1['ok'] and r2['ok'], (r1.get('error'), r2.get('error'))
    with open(os.path.join(proj, 'b.txt')) as f:
        assert f.read() == '1\n2\n3\n'


def test_exec_write_file_append_message_keeps_success_prefix(proj):
    _exec_write_file({'path': 'c.txt', 'content': 'x\n', 'description': 'd'},
                     proj, 'c-append', None, {})
    msg = _exec_write_file(
        {'path': 'c.txt', 'content': 'y\n', 'description': 'd',
         'append': True},
        proj, 'c-append', None, {})
    assert msg.startswith('File updated: c.txt'), msg
    assert '[appended' in msg


def test_remote_route_fails_closed_on_append():
    """Static pin: the remote-worktree dispatcher must refuse append — an
    older desktop build would silently overwrite the file."""
    import inspect
    from lib.tasks_pkg.handlers import project as hp
    src = inspect.getsource(hp._execute_remote_project_tool)
    assert "fn_args.get('append')" in src
    assert 'append=true is not supported on the remote' in src
