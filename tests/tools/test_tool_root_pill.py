"""Tests for `_resolve_tool_root_name` — the workspace-root name attached to
a tool-call line so the frontend can render a ``rootname:`` pill.

Focus: an ABSOLUTE path under a NON-primary root must be attributed to that
root (longest-prefix match), not mislabeled as the primary / left unlabeled.
"""

import os

import pytest

from lib.project_mod import config
from lib.tasks_pkg.tool_display import _resolve_tool_root_name

pytestmark = pytest.mark.unit


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


@pytest.mark.parametrize('absolute', [False, True])
@pytest.mark.parametrize('batch_root,item_root', [
    ('FDP', None), ('FDP', ''), ('FDP', 'chatui'), ('chatui', 'FDP'),
])
def test_edit_batch_announces_inherited_and_overridden_roots(
        two_roots, absolute, batch_root, item_root):
    """Raw declarations render before execution fills any inherited paths."""
    import json
    from lib.tasks_pkg.tool_display import _build_tool_round_entry

    conv_id, primary, fdp = two_roots
    roots = {'chatui': primary, 'FDP': fdp}

    def path(root):
        return (os.path.join(roots[root], 'a.py') if absolute
                else f'{root}:a.py')

    edit = {'operation': 'replace', 'anchor': 'A', 'content': 'B'}
    if item_root is not None:
        edit['path'] = path(item_root) if item_root else ''
    args = {'path': path(batch_root), 'description': 'update the source',
            'edits': [edit]}
    declared = json.dumps(args)
    number, entry, event = _build_tool_round_entry(
        'edit_file', args, 'batch-edit', declared, 0, True, conv_id=conv_id)

    assert number == 1
    assert entry['_toolRoot'] == (item_root or batch_root)
    assert event['_toolRoot'] == entry['_toolRoot']
    assert entry['toolName'] == event['toolName'] == 'edit_file'
    assert entry['query'] != 'edit_file'
    assert event['query'] == entry['query']
    assert json.dumps(args) == declared
    assert entry['toolArgs'] == event['toolArgs'] == declared


def test_non_fs_tool_no_pill(two_roots):
    conv_id, _primary, _fdp = two_roots
    name = _resolve_tool_root_name('web_search', {'query': 'foo'}, conv_id=conv_id)
    assert name == ''
