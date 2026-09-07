"""Regression test: absolute-path writes outside the primary root must be
journalled with the CORRECT workspace-root name and a clean root-relative
path — not the primary root + the full absolute path.

The bug (reported via the file-changes bar): editing a file by ABSOLUTE path
under an extra workspace root (e.g. ``/…/overleaf-mcp/src/…/config.py`` while
the primary root is ``chatui``) showed the wrong ``chatui:`` prefix on the
file-card AND the full absolute path, because ``_resolve_write_path``
auto-registered the extra root and returned the absolute target, but
``_record_modification`` was still called with the PRIMARY ``base`` and the
absolute ``rel_path``.  ``_record_modification`` then matched the primary root
in the registry → recorded ``root='chatui'`` and stored the absolute path.

The fix (``_mod_attribution`` in ``lib/project_mod/write_tools.py``) re-derives
the owning root from the resolved target, so the journal records the deepest
matching root (the extra root) and a path relative to it.

Drives the REAL ``tool_write_file`` / ``tool_apply_diff`` / ``tool_insert_content``.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lib.project_mod import config as pm_config  # noqa: E402
from lib.project_mod.modifications import get_modifications  # noqa: E402
from lib.project_mod.write_tools import (  # noqa: E402
    tool_apply_diff,
    tool_insert_content,
    tool_write_file,
)


def _register_primary(name, abs_path):
    with pm_config._lock:
        pm_config._roots.clear()
        pm_config._roots[name] = pm_config._make_root_state(abs_path)
        pm_config._state['path'] = os.path.abspath(abs_path)


def _register_extra(name, abs_path):
    with pm_config._lock:
        pm_config._roots[name] = pm_config._make_root_state(abs_path)


def _cleanup():
    with pm_config._lock:
        pm_config._roots.clear()
        pm_config._state['path'] = ''


def _latest_mod(root_abs, conv_id):
    mods = get_modifications(root_abs, conv_id=conv_id)
    return mods[-1] if mods else None


def test_abs_write_file_attributed_to_extra_root():
    with tempfile.TemporaryDirectory() as primary, \
         tempfile.TemporaryDirectory() as extra:
        try:
            _register_primary('chatui', primary)
            _register_extra('overleaf-mcp', extra)

            rel = 'src/overleaf_mcp/config.py'
            abs_path = os.path.join(extra, rel)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)

            res = tool_write_file(primary, abs_path, 'X = 1\n',
                                  conv_id='conv-1', task_id='task-1')
            assert res['ok'], res

            # The mod must be journalled under the EXTRA root's session dir,
            # tagged with the extra root's name and a clean relative path.
            mod = _latest_mod(extra, 'conv-1')
            assert mod is not None, 'mod not recorded under extra root session'
            assert mod['root'] == 'overleaf-mcp', mod
            assert mod['path'] == rel, mod
            assert not os.path.isabs(mod['path']), mod
            assert os.path.abspath(mod['basePath']) == os.path.abspath(extra), mod

            # And NOT under the primary root's session (the old buggy behaviour).
            assert _latest_mod(primary, 'conv-1') is None, \
                'mod leaked into primary root session'
        finally:
            _cleanup()


def test_abs_apply_diff_attributed_to_extra_root():
    with tempfile.TemporaryDirectory() as primary, \
         tempfile.TemporaryDirectory() as extra:
        try:
            _register_primary('chatui', primary)
            _register_extra('overleaf-mcp', extra)

            rel = 'src/overleaf_mcp/git_client.py'
            abs_path = os.path.join(extra, rel)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, 'w') as f:
                f.write('BASE = "old"\n')

            res = tool_apply_diff(primary, abs_path, 'old', 'new',
                                  conv_id='conv-2', task_id='task-2')
            assert res['ok'], res

            mod = _latest_mod(extra, 'conv-2')
            assert mod is not None, 'mod not recorded under extra root session'
            assert mod['root'] == 'overleaf-mcp', mod
            assert mod['path'] == rel, mod
            assert _latest_mod(primary, 'conv-2') is None
        finally:
            _cleanup()


def test_abs_insert_content_attributed_to_extra_root():
    with tempfile.TemporaryDirectory() as primary, \
         tempfile.TemporaryDirectory() as extra:
        try:
            _register_primary('chatui', primary)
            _register_extra('overleaf-mcp', extra)

            rel = 'server.py'
            abs_path = os.path.join(extra, rel)
            with open(abs_path, 'w') as f:
                f.write('ANCHOR\n')

            res = tool_insert_content(primary, abs_path, 'ANCHOR', 'NEWLINE\n',
                                      conv_id='conv-3', task_id='task-3')
            assert res['ok'], res

            mod = _latest_mod(extra, 'conv-3')
            assert mod is not None
            assert mod['root'] == 'overleaf-mcp', mod
            assert mod['path'] == rel, mod
        finally:
            _cleanup()


def test_relative_path_attribution_unchanged():
    """A plain relative path under the primary root is unaffected."""
    with tempfile.TemporaryDirectory() as primary:
        try:
            _register_primary('chatui', primary)
            res = tool_write_file(primary, 'notes.txt', 'hi\n',
                                  conv_id='conv-4', task_id='task-4')
            assert res['ok'], res
            mod = _latest_mod(primary, 'conv-4')
            assert mod is not None
            assert mod['root'] == 'chatui', mod
            assert mod['path'] == 'notes.txt', mod
        finally:
            _cleanup()


if __name__ == '__main__':
    test_abs_write_file_attributed_to_extra_root()
    test_abs_apply_diff_attributed_to_extra_root()
    test_abs_insert_content_attributed_to_extra_root()
    test_relative_path_attribution_unchanged()
    print('All tests passed.')
