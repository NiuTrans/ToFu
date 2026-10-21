"""Explicit file grants reach descriptor I/O, never generic/ref/remote adapters."""
from copy import deepcopy
from types import SimpleNamespace
import os

import pytest

from lib.llm.llm_errors import AbortedError
from lib.project_dispatch import admission, scope
from lib.project_mod import tools, modifications, write_intents
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit


@pytest.fixture
def granted(tmp_path, monkeypatch):
    root = tmp_path / 'owned'
    root.mkdir()
    (root / 'src').mkdir()
    (root / 'src' / 'a.txt').write_text('alpha\nbeta\n')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.txt').write_text('ungranted secret\n')
    binding = {'assignmentId': 'grant', 'projectPath': str(tmp_path / 'canonical')}
    row = {'workspace_path': str(root)}
    profile = {'mode': 'files', 'tools': ['read_files', 'write_file', 'edit_file'],
               'readPaths': ['src'], 'writePaths': ['src'], 'maxToolCalls': 40}
    assignment = {'request': {'profile': profile}}
    task = {'id': 'scope-task', 'convId': 'scope-conversation-' + root.name + str(tmp_path),
            'config': {'projectPath': str(root)}, '_fileGrant': binding}
    live = {'revoked': False, 'owner': True}
    def validate(task, **kwargs):
        if '_fileGrant' not in (task or {}):
            return None
        if live['revoked'] or task.get('aborted'):
            raise AbortedError('Revoked or cancelled test grant')
        return binding
    def owned(task, binding=None):
        if not live['owner'] or task['config']['projectPath'] != row['workspace_path']:
            raise AbortedError('Durable test workspace owner changed')
        return row
    monkeypatch.setattr(admission, 'validate_task', validate)
    monkeypatch.setattr(admission, 'owned_workspace', owned)
    monkeypatch.setattr(admission, 'repository_for', lambda binding: SimpleNamespace(validate=lambda binding: assignment))
    # All journal/history authority is local to this test, never selected live storage.
    monkeypatch.setattr(modifications, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(modifications, '_live_change_listeners', [])
    from lib import file_history
    monkeypatch.setattr(file_history, 'track_edit', lambda *a, **k: None)
    value = SimpleNamespace(root=root, outside=outside, task=task, profile=profile,
                            assignment=assignment, live=live, row=row)
    value.call = lambda name, args: tools.execute_tool(name, args, str(root),
        task=task, conv_id=task['convId'], task_id=task['id'])
    return value


def test_omitted_profile_is_consult_no_tools():
    profile = scope.profile_for({'request': {}})
    assert profile == {'mode': 'consult', 'tools': [], 'readPaths': [], 'writePaths': [], 'maxToolCalls': 0}
    profile['tools'].append('read_files')
    assert scope.profile_for({'request': {}})['tools'] == []


@pytest.mark.parametrize('path', ['../secret', 'src/../secret', '/tmp/secret', '~/secret',
    'file://secret', 'https://host/x', 'src//a.txt', './src/a.txt', 'src\\a.txt',
    'src/%2e%2e/a', 'src/\x00a', 'src/a.txt ', 'src/.git/config', 'src/.tofu/x',
    'src/modifications.json', 'src/.worktrees/x', 'src/data/secrets', 'src/logs/app.log'])
def test_rejects_aliases_traversal_and_runtime_metadata_before_io(granted, path, monkeypatch):
    monkeypatch.setattr(tools, '_exec_read_files', lambda *a, **k: pytest.fail('generic read fallback'))
    with pytest.raises((ValueError, AbortedError)):
        granted.call('read_files', {'path': path})


@pytest.mark.parametrize('name', ['run_command', 'find_files', 'grep_search', 'execute_program',
                                  'mcp_call', 'spawn_agent', 'todo', 'apply_diff', 'inspect_image'])
def test_file_grants_do_not_admit_opaque_or_recursive_tools(granted, name):
    with pytest.raises(AbortedError):
        granted.call(name, {})


@pytest.mark.parametrize('extra', [
    {'content_ref': {'tool_round': 1}}, {'allow_outside_workspace': False},
    {'_tofu_allow_outside_workspace': True}, {'remoteWorktree': 'host'},
    {'projectPaths': ['src']}, {'attachment': 'id'}, {'base_path': '/tmp'},
    {'runtime_context': {}}, {'append': 'true'}])
def test_write_unknown_control_or_ref_fields_are_refused(granted, extra):
    args = {'path': 'src/a.txt', 'content': 'replacement', **extra}
    with pytest.raises(ValueError):
        granted.call('write_file', args)
    assert (granted.root / 'src/a.txt').read_text() == 'alpha\nbeta\n'


def test_exact_tools_and_segment_prefixes(granted):
    granted.profile['tools'] = ['read_files']
    with pytest.raises(AbortedError):
        granted.call('write_file', {'path': 'src/a.txt', 'content': 'no'})
    with pytest.raises(AbortedError):
        granted.call('read_files', {'path': 'src2/a.txt'})
    assert scope.relative_path_allowed('src/a.txt', ['src'])
    assert not scope.relative_path_allowed('src2/a.txt', ['src'])
    assert not scope.relative_path_allowed('/src/a.txt', ['src'])


@pytest.mark.parametrize('path', ['.env', 'src/.env.local', '.native/state.json',
    'node_modules/a.txt', '.venv/a.txt', 'src/__pycache__/a.txt', 'target/a.txt', 'build/a.txt'])
def test_whole_project_scope_never_admits_secrets_runtime_or_caches(granted, path):
    granted.profile['readPaths'] = ['.']
    granted.profile['writePaths'] = ['.']
    scope.validate_profile({'profile': granted.profile})
    assert not scope.relative_path_allowed(path, ['.'])

    from lib.project_dispatch.publication import _path_allowed
    assert not _path_allowed(path, ['.'], granted.root)
    with pytest.raises((ValueError, AbortedError)):
        granted.call('read_files', {'path': path})


def test_whole_project_prefix_is_explicit_not_a_directory_read(granted):
    granted.profile['readPaths'] = ['.']
    granted.profile['writePaths'] = ['.']
    scope.validate_profile({'profile': granted.profile})
    assert 'alpha' in granted.call('read_files', {'path': 'src/a.txt'})
    granted.call('write_file', {'path': 'new.txt', 'content': 'explicit root scope'})
    assert (granted.root / 'new.txt').read_text() == 'explicit root scope'
    with pytest.raises(ValueError):
        granted.call('read_files', {'path': '.'})
    assert not scope.checkpoint_paths_allowed({'profile': granted.profile}, ['.env'])
    assert scope.checkpoint_paths_allowed({'profile': granted.profile}, ['new.txt'])


def test_profile_rejects_unfenced_tools_and_expanded_preimage_reads(granted):
    invalid = deepcopy(granted.profile)
    invalid['tools'] = ['run_command']
    with pytest.raises(ValueError):
        scope.validate_profile({'profile': invalid})
    invalid = deepcopy(granted.profile)
    invalid['writePaths'] = ['other']
    with pytest.raises(ValueError):
        scope.validate_profile({'profile': invalid})
    invalid = deepcopy(granted.profile)
    invalid['readPaths'] = ['/tmp']
    with pytest.raises(ValueError):
        scope.validate_profile({'profile': invalid})


def test_revocation_and_workspace_owner_rechecked_at_descriptor_io(granted):
    accepted = scope.admit_tool(granted.task, 'read_files', {'path': 'src/a.txt'}, str(granted.root))
    granted.live['revoked'] = True
    with pytest.raises(AbortedError):
        accepted.read('src/a.txt', 1024)
    granted.live['revoked'] = False
    granted.live['owner'] = False
    with pytest.raises(AbortedError):
        accepted.read('src/a.txt', 1024)


def test_requires_owned_worktree_not_canonical_or_project_expansion(granted):
    with pytest.raises(AbortedError):
        scope.admit_tool(granted.task, 'read_files', {'path': 'src/a.txt'}, str(granted.outside))
    granted.task['config']['projectPaths'] = [str(granted.outside)]
    with pytest.raises(AbortedError):
        granted.call('read_files', {'path': 'src/a.txt'})


@pytest.mark.parametrize('ancestor', [False, True])
def test_symlink_escape_refused_by_actual_read_and_write(granted, ancestor):
    if ancestor:
        path = granted.root / 'src' / 'link'
        path.symlink_to(granted.outside, target_is_directory=True)
        relative = 'src/link/secret.txt'
    else:
        path = granted.root / 'src' / 'link.txt'
        path.symlink_to(granted.outside / 'secret.txt')
        relative = 'src/link.txt'
    for name, args in [('read_files', {'path': relative}),
                        ('write_file', {'path': relative, 'content': 'escape'})]:
        with pytest.raises((OSError, ValueError)):
            granted.call(name, args)
    assert (granted.outside / 'secret.txt').read_text() == 'ungranted secret\n'


def test_hard_link_write_refused_but_regular_file_read_allowed(granted):
    os.link(granted.outside / 'secret.txt', granted.root / 'src' / 'hard.txt')
    assert 'ungranted secret' in granted.call('read_files', {'path': 'src/hard.txt'})
    with pytest.raises(ValueError, match='hard-linked'):
        granted.call('write_file', {'path': 'src/hard.txt', 'content': 'no'})
    assert (granted.outside / 'secret.txt').read_text() == 'ungranted secret\n'


def test_special_files_and_opaque_content_refused_without_converter(granted):
    os.mkfifo(granted.root / 'src' / 'fifo')
    with pytest.raises(ValueError, match='regular'):
        granted.call('read_files', {'path': 'src/fifo'})
    (granted.root / 'src' / 'binary').write_bytes(b'\x00not-text')
    with pytest.raises(ValueError, match='UTF-8 text'):
        granted.call('read_files', {'path': 'src/binary'})


def test_real_read_write_append_edit_and_exact_durable_undo(granted):
    result = granted.call('read_files', {'reads': [{'path': 'src/a.txt', 'start_line': 2, 'end_line': 2}]})
    assert 'beta' in result and 'alpha' not in result
    result = granted.call('write_file', {'path': 'src/new/deep.txt', 'content': 'one\n'})
    assert 'created' in result
    granted.call('write_file', {'path': 'src/new/deep.txt', 'content': 'two\n', 'append': True})
    assert (granted.root / 'src/new/deep.txt').read_bytes() == b'one\ntwo\n'
    result = granted.call('edit_file', {'edits': [
        {'path': 'src/a.txt', 'operation': 'replace', 'anchor': 'alpha', 'content': 'gamma'},
        {'path': 'src/a.txt', 'operation': 'insert_after', 'anchor': 'gamma\n', 'content': 'inserted\n'}]})
    assert 'Applied 2/2' in result
    assert (granted.root / 'src/a.txt').read_text() == 'gamma\n\ninserted\nbeta\n'
    rows = modifications.get_modifications(str(granted.root))
    assert len(rows) == 3
    import base64
    assert base64.b64decode(rows[-1]['originalContent']) == b'alpha\nbeta\n'
    assert rows[-1]['convId'] == granted.task['convId']
    with write_intents.locked_intents(modifications._get_session_dir(str(granted.root))) as store:
        assert store.read() == []


def test_scoped_batch_path_edit_stays_inside_the_grant(granted):
    """A scoped call may name the file once for the batch."""
    granted.call('read_files', {'path': 'src/a.txt'})
    result = granted.call('edit_file', {
        'path': 'src/a.txt',
        'edits': [
            {'operation': 'replace', 'anchor': 'alpha', 'content': 'gamma'},
            {'operation': 'insert_after', 'anchor': 'gamma\n',
             'content': 'inserted\n'}]})
    assert 'Applied 2/2' in result
    assert (granted.root / 'src/a.txt').read_text() == 'gamma\n\ninserted\nbeta\n'


def test_scoped_batch_path_cannot_escape_the_granted_prefix(granted):
    outside = granted.outside / 'secret.txt'
    with pytest.raises(ValueError):
        granted.call('edit_file', {
            'path': '../outside/secret.txt',
            'edits': [{'operation': 'replace', 'anchor': 'ungranted',
                       'content': 'owned'}]})
    assert outside.read_text() == 'ungranted secret\n'


@pytest.mark.unit
def test_scoped_batch_path_requires_a_file_for_each_edit(granted):
    with pytest.raises(ValueError, match='explicit file per edit'):
        granted.call('edit_file', {
            'edits': [{'operation': 'replace', 'anchor': 'alpha',
                       'content': 'gamma'}]})
    assert (granted.root / 'src/a.txt').read_text() == 'alpha\nbeta\n'


def test_read_before_edit_and_stale_tokens_use_confined_preimage(granted):
    edit = {'edits': [{'path': 'src/a.txt', 'operation': 'replace', 'anchor': 'alpha', 'content': 'new'}]}
    assert 'Read this scoped file' in granted.call('edit_file', edit)
    granted.call('read_files', {'path': 'src/a.txt'})
    (granted.root / 'src/a.txt').write_text('alpha\nchanged sibling\n')
    assert 'changed since your read' in granted.call('edit_file', edit)
    assert (granted.root / 'src/a.txt').read_text() == 'alpha\nchanged sibling\n'


def test_leaf_swap_between_preimage_and_publication_never_writes_outside(granted, monkeypatch):
    original = scope.FileScope.publish
    def swap(self, path, *args, **kwargs):
        target = granted.root / path
        target.unlink()
        target.symlink_to(granted.outside / 'secret.txt')
        return original(self, path, *args, **kwargs)
    monkeypatch.setattr(scope.FileScope, 'publish', swap)
    with pytest.raises(ValueError, match='unsafe link'):
        granted.call('write_file', {'path': 'src/a.txt', 'content': 'no'})
    assert (granted.outside / 'secret.txt').read_text() == 'ungranted secret\n'


def test_parent_swap_after_stage_preparation_refuses_publication(granted, monkeypatch):
    original = modifications._atomic_json_write
    def swap(path, data):
        result = original(path, data)
        if data.get('state') == 'prepared':
            (granted.root / 'src').rename(granted.root / 'old-src')
            (granted.root / 'src').symlink_to(granted.outside, target_is_directory=True)
        return result
    monkeypatch.setattr(modifications, '_atomic_json_write', swap)
    with pytest.raises((ValueError, OSError)):
        granted.call('write_file', {'path': 'src/a.txt', 'content': 'no'})
    assert (granted.root / 'old-src/a.txt').read_text() == 'alpha\nbeta\n'
    assert not (granted.outside / 'a.txt').exists()
    assert not list((granted.root / 'old-src').glob('.tofu_atomic_*'))


def test_pending_recovery_never_replays_other_granted_paths(granted, monkeypatch):
    def pending(self):
        return [{'path': 'other/not-granted.txt'}]
    monkeypatch.setattr(write_intents.IntentStore, 'read', pending)
    with pytest.raises(write_intents.IntentError, match='trusted recovery owner'):
        granted.call('write_file', {'path': 'src/a.txt', 'content': 'no'})
    assert (granted.root / 'src/a.txt').read_text() == 'alpha\nbeta\n'


def test_scoped_handler_fences_before_remote_refs_preimages(granted, monkeypatch):
    from lib.tasks_pkg.handlers import project
    calls = []
    monkeypatch.setattr(project, '_finalize_tool_round', lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(project, '_record_producer_result_metadata', lambda *a, **kw: None)
    monkeypatch.setattr(project, '_execute_remote_project_tool', lambda *a, **kw: pytest.fail('remote transport'))
    from lib.tasks_pkg import workspace_delivery
    monkeypatch.setattr(workspace_delivery, 'rebind_project_tool_arguments', lambda *a, **kw: pytest.fail('workspace expansion'))
    monkeypatch.setattr(project, '_resolve_content_ref', lambda *a, **kw: pytest.fail('content ref'))
    result = project._handle_project_tool(granted.task, {}, 'read_files', 'call-1',
        {'path': 'src/a.txt'}, 1, {}, granted.task['config'], str(granted.root), True)
    assert result[0] == 'call-1' and 'alpha' in result[1]
    assert calls[-1]['status'] == 'done'
    with pytest.raises(ValueError):
        project._handle_project_tool(granted.task, {}, 'write_file', 'call-2',
            {'path': 'src/a.txt', 'content_ref': {'tool_round': 1}}, 1, {},
            granted.task['config'], str(granted.root), True)


def test_custom_handlers_cannot_borrow_builtin_file_names():
    from lib.tasks_pkg.handlers.project import _handle_project_tool
    scope.assert_firstparty_file_handler('read_files', _handle_project_tool)
    def impostor(*a, **kw):
        return None
    impostor.__module__ = _handle_project_tool.__module__
    with pytest.raises(AbortedError):
        scope.assert_firstparty_file_handler('read_files', impostor)


def test_generic_dispatch_remains_on_original_handler(granted, monkeypatch):
    result = object()
    monkeypatch.setitem(tools._EXEC_HANDLERS, 'read_files', lambda *a, **kw: result)
    assert tools.execute_tool('read_files', {'path': str(granted.outside / 'secret.txt')},
                              str(granted.root), task={'id': 'ordinary'}) is result
    assert scope.current_scope() is None


def test_inherited_root_scope_cannot_be_dropped_by_missing_or_other_task(granted):
    with scope.tool_scope(granted.task, 'read_files', {'path': 'src/a.txt'}, str(granted.root)):
        assert 'alpha' in tools.execute_tool('read_files', {'path': 'src/a.txt'}, str(granted.root))
        with pytest.raises(AbortedError):
            tools.execute_tool('run_command', {'command': 'echo unsafe'}, str(granted.root))
        with pytest.raises(AbortedError):
            tools.execute_tool('read_files', {'path': str(granted.outside / 'secret.txt')},
                               str(granted.root), task={'id': 'child'})
    assert scope.current_scope() is None


def test_runtime_cancellation_survives_nested_scope(granted):
    from lib.project_mod.tool_admission import tool_admission_scope
    runtime = SimpleNamespace(cancellation_requested=False)
    with tool_admission_scope(granted.task, runtime, tool_name='read_files'):
        with scope.tool_scope(granted.task, 'read_files', {'path': 'src/a.txt'}, str(granted.root)):
            runtime.cancellation_requested = True
            with pytest.raises(AbortedError, match='cancelled'):
                tools.execute_tool('read_files', {'path': 'src/a.txt'}, str(granted.root))


def test_consult_profile_denies_even_explicit_read(granted):
    granted.assignment['request'].pop('profile')
    with pytest.raises(AbortedError):
        granted.call('read_files', {'path': 'src/a.txt'})


def test_creation_cannot_expand_a_missing_ancestor_outside_write_prefix(granted):
    granted.profile['readPaths'] = ['narrow/deep']
    granted.profile['writePaths'] = ['narrow/deep']
    with pytest.raises(ValueError, match='expand'):
        granted.call('write_file', {'path': 'narrow/deep/a.txt', 'content': 'no'})
    assert not (granted.root / 'narrow').exists()


def test_replaced_root_cannot_borrow_the_old_descriptor_authority(granted):
    accepted = scope.admit_tool(granted.task, 'read_files', {'path': 'src/a.txt'}, str(granted.root))
    granted.root.rename(granted.root.with_name('original-owned'))
    granted.root.symlink_to(granted.outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        accepted.read('src/a.txt', 1024)


def test_changed_stage_is_never_installed_as_a_symlink(granted, monkeypatch):
    original = modifications._atomic_json_write
    def swap(path, data):
        result = original(path, data)
        if data.get('state') == 'prepared':
            stage = granted.root / 'src' / data['stageName']
            stage.unlink()
            stage.symlink_to(granted.outside / 'secret.txt')
        return result
    monkeypatch.setattr(modifications, '_atomic_json_write', swap)
    with pytest.raises(ValueError, match='stage was replaced'):
        granted.call('write_file', {'path': 'src/a.txt', 'content': 'no'})
    assert (granted.root / 'src/a.txt').read_text() == 'alpha\nbeta\n'
    assert (granted.outside / 'secret.txt').read_text() == 'ungranted secret\n'


def test_coarse_stat_tuple_cannot_hide_changed_preimage_bytes(granted, monkeypatch):
    from lib.project_mod import snapshot_content
    monkeypatch.setattr(snapshot_content, 'file_fingerprint', lambda st: (1, 2, 11, 4, 5))
    original = modifications._atomic_json_write
    def concurrent_edit(path, data):
        result = original(path, data)
        if data.get('state') == 'prepared':
            (granted.root / 'src/a.txt').write_bytes(b'OTHER\nbeta\n')
        return result
    monkeypatch.setattr(modifications, '_atomic_json_write', concurrent_edit)
    with pytest.raises(ValueError, match='content changed'):
        granted.call('write_file', {'path': 'src/a.txt', 'content': 'no'})
    assert (granted.root / 'src/a.txt').read_bytes() == b'OTHER\nbeta\n'


def test_corrupt_journal_prevalidation_precedes_target_publication(granted):
    session = modifications._get_session_dir(str(granted.root))
    from pathlib import Path
    (Path(session) / 'modifications.json').write_text('not valid JSON')
    with pytest.raises((ValueError, OSError)):
        granted.call('write_file', {'path': 'src/a.txt', 'content': 'no'})
    assert (granted.root / 'src/a.txt').read_text() == 'alpha\nbeta\n'
    assert not list((granted.root / 'src').glob('.tofu_atomic_*'))


def test_applied_journal_failure_is_not_retryable_and_retains_recovery_evidence(granted, monkeypatch):
    monkeypatch.setattr(_ops, '_record_modification', lambda *a, **kw: False)
    with pytest.raises(_ops._UndoRecordingFailed, match='already applied'):
        granted.call('write_file', {'path': 'src/a.txt', 'content': 'applied\n'})
    assert (granted.root / 'src/a.txt').read_bytes() == b'applied\n'
    with write_intents.locked_intents(modifications._get_session_dir(str(granted.root))) as store:
        pending = store.read()
        assert len(pending) == 1 and pending[0]['path'] == 'src/a.txt'
    with pytest.raises(write_intents.IntentError, match='trusted recovery owner'):
        granted.call('write_file', {'path': 'src/a.txt', 'content': 'do not retry\n'})
    assert (granted.root / 'src/a.txt').read_bytes() == b'applied\n'


def test_complete_scoped_read_batch_past_twenty_and_file_byte_quota(granted):
    marker='LAST_COMPLETE_SCOPED_READ'
    for number in range(32):
        (granted.root/'src'/f'file-{number:02d}.txt').write_text(f'source {number}\n')
    large=granted.root/'src'/'file-31.txt'
    text='q'*(600*1024)+'\n'+marker+'\n';large.write_text(text)
    result=granted.call('read_files',{'reads':[{'path':f'src/file-{number:02d}.txt'} for number in range(32)]})
    assert text in result and marker in result
    for number in range(32):assert f'file-{number:02d}.txt' in result


def test_scoped_write_above_sixteen_mib_is_complete_and_undoable(granted):
    import hashlib
    original=b'alpha\nbeta\n'
    text='q'*(17*1024*1024)+'\nFULL_SCOPED_POST_IMAGE\n'
    result=granted.call('write_file',{'path':'src/a.txt','content':text})
    assert 'updated' in result
    expected=hashlib.sha256(text.encode()).digest()
    assert hashlib.sha256((granted.root/'src'/'a.txt').read_bytes()).digest()==expected
    undone=modifications.undo_task_modifications(str(granted.root),granted.task['id'])
    assert undone['failed']==0 and undone['undone']==1
    assert (granted.root/'src'/'a.txt').read_bytes()==original


def test_complete_scoped_edit_chain_past_thirty_retains_exact_undo(granted):
    target=granted.root/'src'/'a.txt';target.write_text('STATE_000\n')
    granted.call('read_files',{'path':'src/a.txt'})
    edits=[{'path':'src/a.txt','operation':'replace','anchor':f'STATE_{number:03d}',
            'content':f'STATE_{number+1:03d}'} for number in range(64)]
    result=granted.call('edit_file',{'description':'Apply full edit chain','edits':edits})
    assert '64/64' in result and '[64] OK' in result
    assert target.read_text()=='STATE_064\n'
    undone=modifications.undo_task_modifications(str(granted.root),granted.task['id'])
    assert undone['failed']==0 and undone['undone']==1
    assert target.read_text()=='STATE_000\n'


def test_scoped_small_append_to_large_preimage_keeps_exact_undo(granted):
    import hashlib
    target=granted.root/'src'/'a.txt'
    original=b'q'*(17*1024*1024)+b'\nORIGINAL_LARGE_PREIMAGE\n'
    target.write_bytes(original)
    addition='SMALL_COMPLETE_APPEND\n'
    result=granted.call('write_file',{'path':'src/a.txt','content':addition,'append':True})
    assert 'updated' in result
    assert hashlib.sha256(target.read_bytes()).digest()==hashlib.sha256(original+addition.encode()).digest()
    undone=modifications.undo_task_modifications(str(granted.root),granted.task['id'])
    assert undone['failed']==0 and undone['undone']==1
    assert hashlib.sha256(target.read_bytes()).digest()==hashlib.sha256(original).digest()


@pytest.mark.parametrize('atomic', [None, True, False])
def test_scoped_batch_preflights_all_files_before_any_write(granted, atomic):
    (granted.root / 'src/b.txt').write_text('second\n')
    granted.call('read_files', {'reads': [{'path': 'src/a.txt'}, {'path': 'src/b.txt'}]})
    args = {'edits': [
        {'path': 'src/a.txt', 'operation': 'replace', 'anchor': 'alpha', 'content': 'changed'},
        {'path': 'src/b.txt', 'operation': 'replace', 'anchor': 'absent', 'content': 'no'},
        {'path': 'src/a.txt', 'operation': 'replace', 'anchor': 'changed', 'content': 'final'},
    ]}
    if atomic is not None:
        args['atomic'] = atomic
    result = granted.call('edit_file', args)
    if atomic is False:
        assert 'Applied 2/3' in result
        assert (granted.root / 'src/a.txt').read_text() == 'final\nbeta\n'
        assert len(modifications.get_modifications(str(granted.root))) == 1
    else:
        assert 'Applied 0/3' in result
        assert '[1] SKIP' in result and '[2] FAIL' in result and '[3] SKIP' in result
        assert (granted.root / 'src/a.txt').read_text() == 'alpha\nbeta\n'
        assert modifications.get_modifications(str(granted.root)) == []
    assert (granted.root / 'src/b.txt').read_text() == 'second\n'


def test_scoped_later_unread_file_never_follows_an_earlier_write(granted):
    (granted.root / 'src/b.txt').write_text('second\n')
    granted.call('read_files', {'path': 'src/a.txt'})
    result = granted.call('edit_file', {'edits': [
        {'path': 'src/a.txt', 'operation': 'replace', 'anchor': 'alpha', 'content': 'changed'},
        {'path': 'src/b.txt', 'operation': 'replace', 'anchor': 'second', 'content': 'changed'},
    ]})
    assert 'Read this scoped file' in result
    assert (granted.root / 'src/a.txt').read_text() == 'alpha\nbeta\n'
    assert modifications.get_modifications(str(granted.root)) == []


@pytest.mark.parametrize('atomic', ['false', None, 0, []])
def test_scoped_atomic_argument_is_boolean_not_truthy(granted, atomic):
    with pytest.raises(ValueError, match='atomic'):
        granted.call('edit_file', {'atomic': atomic, 'edits': [
            {'path': 'src/a.txt', 'operation': 'replace', 'anchor': 'alpha', 'content': 'no'},
        ]})
    assert (granted.root / 'src/a.txt').read_text() == 'alpha\nbeta\n'


def test_scoped_out_of_bounds_range_cannot_certify_an_unseen_file(granted):
    with pytest.raises(ValueError, match='beyond EOF'):
        granted.call('read_files', {'path': 'src/a.txt', 'start_line': 500, 'end_line': 600})
    result = granted.call('edit_file', {'edits': [
        {'path': 'src/a.txt', 'operation': 'replace', 'anchor': 'alpha', 'content': 'changed'},
    ]})
    assert 'Read this scoped file' in result
    assert (granted.root / 'src/a.txt').read_bytes() == b'alpha\nbeta\n'
    assert modifications.get_modifications(str(granted.root)) == []


def test_scoped_whole_empty_file_read_remains_a_valid_observation(granted):
    from lib import write_freshness

    target = granted.root / 'src/a.txt'
    target.write_bytes(b'')
    result = granted.call('read_files', {'path': 'src/a.txt'})
    assert 'File: src/a.txt' in result
    with write_freshness._lock:
        observation = write_freshness._tokens[(granted.task['convId'], str(target))]
    assert observation[0][0] == 's' and observation[0][1] == 0
