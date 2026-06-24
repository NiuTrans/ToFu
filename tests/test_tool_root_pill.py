"""Tests for `_resolve_tool_root_name` — the workspace-root name attached to
a tool-call line so the frontend can render a ``rootname:`` pill.

Focus: an ABSOLUTE path under a NON-primary root must be attributed to that
root (longest-prefix match), not mislabeled as the primary / left unlabeled.
"""

import os

import pytest

from lib.project_mod import config
from lib.tasks_pkg.tool_display import _resolve_tool_root_name


@pytest.fixture
def two_roots(tmp_path):
    """Register a conv with a primary root + a non-primary 'FDP' root."""
    primary = tmp_path / 'chatui'
    fdp = tmp_path / 'FDP'
    primary.mkdir()
    fdp.mkdir()
    conv_id = 'conv-tool-root-pill'
    config.set_conv_roots(conv_id, str(primary), extras=[str(fdp)])
    yield conv_id, str(primary), str(fdp)
    config.clear_conv_state(conv_id)


def test_absolute_path_under_nonprimary_root(two_roots):
    conv_id, _primary, fdp = two_roots
    abs_path = os.path.join(fdp, 'hope', 'op2_train.sh')
    name = _resolve_tool_root_name('read_files', {'reads': [{'path': abs_path}]},
                                   conv_id=conv_id)
    assert name == os.path.basename(fdp)  # 'FDP'


def test_absolute_path_under_primary_root(two_roots):
    conv_id, primary, _fdp = two_roots
    abs_path = os.path.join(primary, 'server.py')
    name = _resolve_tool_root_name('read_files', {'reads': [{'path': abs_path}]},
                                   conv_id=conv_id)
    assert name == os.path.basename(primary)  # 'chatui'


def test_explicit_prefix_still_wins(two_roots):
    conv_id, _primary, fdp = two_roots
    fdp_name = os.path.basename(fdp)
    name = _resolve_tool_root_name('read_files',
                                   {'reads': [{'path': f'{fdp_name}:hope/op2_train.sh'}]},
                                   conv_id=conv_id)
    assert name == fdp_name


def test_longest_prefix_for_nested_roots(tmp_path):
    """A path under a nested root resolves to the DEEPEST containing root."""
    outer = tmp_path / 'outer'
    inner = outer / 'inner'
    inner.mkdir(parents=True)
    conv_id = 'conv-nested-roots'
    config.set_conv_roots(conv_id, str(outer), extras=[str(inner)])
    try:
        abs_path = os.path.join(str(inner), 'pkg', 'mod.py')
        name = _resolve_tool_root_name('read_files',
                                       {'reads': [{'path': abs_path}]},
                                       conv_id=conv_id)
        assert name == os.path.basename(str(inner))  # 'inner', not 'outer'
    finally:
        config.clear_conv_state(conv_id)


def test_single_root_no_pill(tmp_path):
    primary = tmp_path / 'solo'
    primary.mkdir()
    conv_id = 'conv-single-root'
    config.set_conv_roots(conv_id, str(primary))
    try:
        abs_path = os.path.join(str(primary), 'a.py')
        name = _resolve_tool_root_name('read_files',
                                       {'reads': [{'path': abs_path}]},
                                       conv_id=conv_id)
        assert name == ''  # single-root workspace → no pill
    finally:
        config.clear_conv_state(conv_id)


def test_absolute_path_outside_all_roots_falls_back_to_primary(two_roots):
    """A path under NO registered root falls back to the primary's name."""
    conv_id, primary, _fdp = two_roots
    name = _resolve_tool_root_name('read_files',
                                   {'reads': [{'path': '/tmp/somewhere/else.py'}]},
                                   conv_id=conv_id)
    assert name == os.path.basename(primary)  # primary fallback unchanged


def test_non_fs_tool_no_pill(two_roots):
    conv_id, _primary, _fdp = two_roots
    name = _resolve_tool_root_name('web_search', {'query': 'foo'}, conv_id=conv_id)
    assert name == ''
