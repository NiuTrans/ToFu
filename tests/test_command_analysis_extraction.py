"""Guards the 2026-07-11 extraction of the pure command-analysis cluster
from ``lib/project_mod/run_command.py`` into
``lib/project_mod/command_analysis.py``.

The extraction MUST be behavior-neutral and MUST NOT break either historical
import path. This test pins:

  1. Every moved symbol is importable from ALL THREE modules
     (command_analysis, run_command re-export, tools re-export) and is the
     SAME object in each — i.e. no accidental re-definition / shadow.
  2. The relocated functions still produce their documented verdicts (a
     smoke subset; the exhaustive behavior lives in test_project_tools.py,
     which continues to import via ``lib.project_mod.tools``).

If a future refactor moves one of these back inline or drops it from a
re-export, the identity assertions fail loudly rather than silently
forking the definition.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

# Functions re-exported through ALL THREE paths (tools.py's back-compat list is
# function-only, matching every historical caller/test).
_MOVED_FUNCS = [
    '_clean_command_output', '_extract_device_ids', '_extract_progress_label',
    '_extract_progress_pct', '_extract_write_targets', '_filter_changes_by_targets',
    '_format_cuda_device_range', '_has_unquoted_shell_metachars',
    '_is_catastrophic_delete', '_is_destructive_command',
    '_line_fingerprint', '_split_pipeline',
]

# Functions exposed via command_analysis + run_command but NOT tools.py
# (tools.py's back-compat list never included these bare names).
_FUNCS_NO_TOOLS = ['_is_dangerous_command', '_mask_quoted_literals']

# Module-level constants/regexes: shared between command_analysis and the
# run_command re-export (tools.py never re-exported these bare names).
_MOVED_CONSTS = [
    '_ANSI_ESC_RE', '_DANGEROUS_RE', '_DELETE_COMMANDS', '_DEVICE_RE',
    '_FS_HEAVY_RE', '_GIT_DESTRUCTIVE_SUBCOMMANDS', '_GIT_READONLY_SUBCOMMANDS',
    '_MIN_DELETE_DEPTH', '_PROGRESS_RE', '_READONLY_COMMANDS',
    '_REDIRECT_PATTERN', '_REDIRECT_TO_DEV_NULL', '_SED_INPLACE',
    '_WRITE_TARGET_COMMANDS',
]


def test_moved_symbols_identical_across_import_paths():
    import lib.project_mod.command_analysis as ca
    import lib.project_mod.run_command as rc
    import lib.project_mod.tools as tools

    for name in _MOVED_FUNCS:
        src = getattr(ca, name)
        assert getattr(rc, name) is src, f'{name} diverged in run_command'
        assert getattr(tools, name) is src, f'{name} diverged in tools'
    for name in _MOVED_CONSTS + _FUNCS_NO_TOOLS:
        src = getattr(ca, name)
        assert getattr(rc, name) is src, f'{name} diverged in run_command'


def test_relocated_functions_behave():
    from lib.project_mod.command_analysis import (
        _is_catastrophic_delete,
        _is_dangerous_command,
        _is_destructive_command,
        _mask_quoted_literals,
        _split_pipeline,
    )
    # catastrophic-delete guard still blocks top-level + allows deep/relative
    assert _is_catastrophic_delete('rm -rf /') == '/'
    assert _is_catastrophic_delete('rm -rf /mnt') is not None
    assert _is_catastrophic_delete('rm -rf build/') is None
    # destructive classifier
    assert _is_destructive_command('rm -rf /tmp/x') is True
    assert not _is_destructive_command('git status')  # readonly → falls through (None)
    # quoted-literal masking neutralises in-string dangerous words
    assert not _is_dangerous_command('grep -E "graceful shutdown" app.log')
    assert _mask_quoted_literals("echo 'shutdown'").strip().startswith('echo')
    # pipeline split respects quotes
    assert _split_pipeline('grep -i "a|b" f | wc -l') == ['grep -i "a|b" f', 'wc -l']
