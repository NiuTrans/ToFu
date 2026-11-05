"""A dispatch call the tool contract rejects returns its next_action to the model.

Rejected calls never execute, so they are not admitted: an admission refusal
for them used to fail the whole worker. The file scope's own argument-shape rule
rejects only its call. Every call that will execute is still admitted before any
call of the batch runs. Real parse, pipeline, executor and descriptor I/O; fake
grant and transport ports only (the file-lifecycle fixture).
"""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.llm.llm_errors import ToolAdmissionRefused
from lib.project_dispatch import admission
from lib.tools.contracts import ARGUMENT_ALTERNATIVES
from tests.project_mod.test_coordinator_workers import _worker_task, base_runtime, runtime
from tests.project_mod.test_project_dispatch_file_lifecycle import file_context
from tests.project_mod.test_project_dispatch_repository import context as context_fixture
from tests.storage.test_integration_delivery import repo as repo_fixture

context = context_fixture
repo = repo_fixture
# Imported fixtures are resolved by name from this module.
__all__ = ('base_runtime', 'runtime')
pytestmark = pytest.mark.unit

READ_NEXT_ACTION = ARGUMENT_ALTERNATIVES['read_files'].next_action
SECRET = 'out-of-scope-bytes'


def worker(context, repo, monkeypatch, profile=None):
    from lib.tasks_pkg.workspace_delivery import bind_worker_workspace
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    f = file_context(context, repo, monkeypatch)
    if profile is not None:
        f.request['profile'] = profile
    row = f.repository.authorize(f.request, f.principal, 'file-command')
    f.repository.bind_execution(row['id'], 'worker-turn', 'worker-attempt')
    trusted = admission.TrustedDispatch(f.repository.admission(row['id']))
    cfg = trusted.capture({}, f.owner, trusted.principal, 'recipient')
    cfg.update(_turnOwnerUserId=f.owner, _turnId='worker-turn', _attemptId='worker-attempt')
    task = {'id': 'automatic', 'convId': 'recipient', 'config': cfg, 'status': 'running',
        'messages': [], 'usage': {}, 'toolRounds': [], 'events': [], '_userId': f.owner,
        '_principalContext': trusted.principal.to_payload(), '_attended': False,
        '_turnId': 'worker-turn', '_attemptId': 'worker-attempt', 'abort_event': threading.Event(),
        'events_lock': threading.Lock(), 'content_lock': threading.Lock(), 'model': 'model-a'}
    admission.bind_account(task)
    if f.request['profile']['mode'] == 'files':
        bind_worker_workspace(task)
    work = Path(task['config'].get('projectPath') or f.root)
    for name, text in (('allowed/a.txt', 'alpha\n'), ('other/secret.txt', SECRET + '\n')):
        (work / name).parent.mkdir(exist_ok=True)
        (work / name).write_text(text)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda _task, event: True)
    tools, _ = assemble_round_tools(task['config'], task, {})
    return SimpleNamespace(task=task, work=work, tools=tools)


def run_round(w, calls, messages=None):
    """One assistant response through the real round: parse -> pipeline -> handlers."""
    from lib.tasks_pkg.orchestrator._tool_dispatch_round import run_tool_dispatch
    assistant = {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': f'call-{index}', 'type': 'function',
         'function': {'name': name, 'arguments': json.dumps(arguments)}}
        for index, (name, arguments) in enumerate(calls)]}
    messages = [] if messages is None else messages
    messages += [{'role': 'user', 'content': 'brief'}, assistant]
    rs = SimpleNamespace(assistant_msg=assistant, tool_round_num=0, model='model-a')
    run_tool_dispatch(w.task, rs, messages, [], round_num=0, tid=w.task['id'][:8],
        cfg=w.task['config'], project_path=str(w.work), project_enabled=True,
        tool_list=w.tools, announced_tc_map={})
    rows = {row['toolCallId']: row for row in w.task['toolRounds']}
    return {m['tool_call_id']: (m['content'], rows[m['tool_call_id']])
            for m in messages if m.get('role') == 'tool'}


def assert_rejected(result, next_action=READ_NEXT_ACTION):
    content, row = result
    assert 'was NOT executed' in content and next_action in content, content
    assert row['status'] == 'rejected', row
    assert row['_contractError']['nextAction'] == next_action
    assert 'alpha' not in content and SECRET not in content


@pytest.mark.parametrize('arguments', [
    pytest.param({}, id='neither-path-nor-reads'),
    # The shared rule is at-least-one; the dispatch copy's scope admits exactly one.
    pytest.param({'path': 'allowed/a.txt', 'reads': [{'path': 'allowed/a.txt'}]},
                 id='both-path-and-reads'),
])
def test_malformed_read_returns_the_contract_next_action_and_the_worker_continues(
        context, repo, monkeypatch, arguments):
    w = worker(context, repo, monkeypatch)
    results = run_round(w, [('read_files', arguments), ('read_files', {'path': 'allowed/a.txt'})])
    assert_rejected(results['call-0'])
    content, row = results['call-1']
    assert 'alpha' in content and row['status'] == 'done', (content, row)


@pytest.mark.parametrize('name, arguments, next_action', [
    pytest.param('read_files', {'path': 'other/secret.txt', 'bind_sources': True}, None,
                 id='contract-unknown-field'),
    pytest.param('read_files', {'path': 'other/secret.txt', 'reads': [{'path': 'other/secret.txt'}]},
                 READ_NEXT_ACTION, id='scope-both-forms'),
    pytest.param('read_files', {'reads': []}, READ_NEXT_ACTION, id='scope-empty-batch'),
    pytest.param('read_files', {'path': 'allowed/a.txt', 'start_line': 5, 'end_line': 2},
                 READ_NEXT_ACTION, id='scope-reversed-range'),
    pytest.param('edit_file', {'edits': [{'path': 'allowed/a.txt', 'operation': 'replace',
                                          'anchor': 'alpha', 'content': 'beta', 'via': 'ref'}]},
                 'Match arguments_schema and retry.', id='scope-unknown-edit-field'),
])
def test_rejected_call_never_executes_or_reads_its_target(
        context, repo, monkeypatch, name, arguments, next_action):
    w = worker(context, repo, monkeypatch)
    results = run_round(w, [(name, arguments)])
    content, row = results['call-0']
    assert 'was NOT executed' in content and row['status'] == 'rejected', (content, row)
    assert SECRET not in content and 'alpha' not in content
    assert (w.work / 'allowed' / 'a.txt').read_text() == 'alpha\n'
    if next_action is not None:
        assert_rejected(results['call-0'], next_action)


def test_every_call_that_would_execute_is_still_admitted_before_any_runs(context, repo, monkeypatch):
    w = worker(context, repo, monkeypatch)
    messages = []
    with pytest.raises(ToolAdmissionRefused, match='outside its accepted read/write prefixes'):
        run_round(w, [('read_files', {}),
                      ('read_files', {'path': 'allowed/a.txt', 'reads': [{'path': 'allowed/a.txt'}]}),
                      ('write_file', {'path': 'allowed/new.txt', 'content': 'written\n'}),
                      ('read_files', {'path': 'allowed/a.txt'}),
                      ('read_files', {'path': 'other/secret.txt'})], messages)
    outputs = ' '.join(str(m.get('content')) for m in messages if m.get('role') == 'tool')
    assert 'alpha' not in outputs and SECRET not in outputs
    assert not (w.work / 'allowed' / 'new.txt').exists()
    assert all(row.get('status') != 'done' for row in w.task['toolRounds'])


def test_ungranted_tool_returns_its_rejection_without_failing_the_worker(context, repo, monkeypatch):
    w = worker(context, repo, monkeypatch)
    results = run_round(w, [('run_command', {'command': 'cat other/secret.txt'}),
                            ('read_files', {'path': 'allowed/a.txt'})])
    content, row = results['call-0']
    assert row['status'] == 'rejected' and SECRET not in content, (content, row)
    assert 'alpha' in results['call-1'][0]


def test_project_worker_call_outside_its_ceiling_returns_its_rejection(runtime, monkeypatch):
    """A project-mode Worker's catalog omits ungranted tools; parse rejects them first."""
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tool_results.tool_rejection import stamp_tool_rejection
    from tests.tools.test_tool_settle_all_lanes import _mk_task, _mk_tc
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda *args, **kwargs: True)
    task, row = _worker_task(runtime)
    assert row['request']['profile']['mode'] == 'project'
    assert 'browser_download_url_to_server' not in row['request']['profile']['tools']
    task.update({key: value for key, value in _mk_task().items() if key not in task})
    rejection = ('Error: `browser_download_url_to_server` is not a real tool and was NOT '
                 'executed. Call only tools from the provided tool list.')
    parsed = _mk_tc('call-0', 'browser_download_url_to_server', 0, parse_err=rejection)
    stamp_tool_rejection(parsed[5], {'kind': 'hallucinated', 'attempted': parsed[1],
                                     'suggestions': []}, tool_name=parsed[1], reason=rejection)
    task['toolRounds'] = [parsed[5]]
    messages = []
    execute_tool_pipeline(task, [parsed], task['config'], '/project', True, [],
                          messages, [], 0, 'model-a')
    assert [m['tool_call_id'] for m in messages] == ['call-0']
    assert 'was NOT executed' in messages[0]['content'] and parsed[5]['status'] == 'rejected'


def test_consult_worker_tool_call_returns_its_rejection(context, repo, monkeypatch):
    w = worker(context, repo, monkeypatch, profile={
        'mode': 'consult', 'tools': [], 'readPaths': [], 'writePaths': [], 'maxToolCalls': 0})
    assert w.tools == []
    results = run_round(w, [('read_files', {'path': 'other/secret.txt'})])
    content, row = results['call-0']
    assert row['status'] == 'rejected' and SECRET not in content, (content, row)
