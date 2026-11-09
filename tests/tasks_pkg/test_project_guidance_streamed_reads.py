"""A project read acquires its nested rules whichever executor serves it.

The streaming accumulator pre-executes read-only project tools while the model
is still streaming; the pipeline then serves that result as a prefetch hit and
never enters the project handler. Rule acquisition must not depend on that
choice: the same call acquires the same rules on both paths (batch
``searches[].path`` targets included), a read whose rules exceed the
acquisition bound is refused rather than served without them, and a file-grant
dispatch scope reads no rule files outside its grant on either path.
"""
from __future__ import annotations

import json
import threading

import pytest

from lib.project_mod.config import build_root_policy, clear_conv_state
from lib.project_mod.instructions import MAX_GUIDANCE_BYTES
from lib.tasks_pkg.project_guidance import refresh_project_guidance

pytestmark = pytest.mark.unit

ROOT_RULE = 'ROOT-RULE applies to the whole project.'
SUB_RULE = 'SUB-RULE applies within sub.'
OTHER_RULE = 'OTHER-RULE applies within other.'
FILE_TEXT = 'needle = 1'

CALLS = {
    'read_files-path': ('read_files', {'path': 'sub/module.py'}),
    'read_files-reads': ('read_files', {'reads': [{'path': 'sub/module.py'}]}),
    'search_files-path': ('search_files', {'pattern': 'needle', 'path': 'sub'}),
    'search_files-searches': ('search_files', {'searches': [{'pattern': 'needle', 'path': 'sub'}]}),
    'grep_search-path': ('grep_search', {'pattern': 'needle', 'path': 'sub'}),
    'find_files-path': ('find_files', {'pattern': '*.py', 'path': 'sub'}),
    'find_files-searches': ('find_files', {'searches': [{'pattern': '*.py', 'path': 'sub'}]}),
    'list_dir-path': ('list_dir', {'path': 'sub'}),
}


@pytest.fixture
def task(tmp_path):
    root = tmp_path / 'project'
    for directory in ('sub', 'other'):
        (root / directory).mkdir(parents=True)
    (root / 'AGENTS.md').write_text(ROOT_RULE)
    (root / 'sub' / 'AGENTS.md').write_text(SUB_RULE)
    (root / 'other' / 'AGENTS.md').write_text(OTHER_RULE)
    (root / 'sub' / 'module.py').write_text(FILE_TEXT + '\n')
    owner = {
        'id': f'streamed-guidance-{tmp_path.name}',
        'convId': f'streamed-guidance-chat-{tmp_path.name}',
        '_userId': 7, '_invocationStorageFree': True, 'aborted': False,
        'config': {'projectPath': str(root)},
        '_workspaceRootPolicy': build_root_policy(str(root)),
        'messages': [], 'toolRounds': [], 'events': [],
        'events_lock': threading.Lock(),
    }
    yield owner
    clear_conv_state(owner['id'])


def _prepare_round(task, messages):
    """Round preparation: the guidance text the next model request gains."""
    before = len(messages)
    refresh_project_guidance(task, messages, model='')
    return '\n'.join(str(message.get('content')) for message in messages[before:])


def _call(name, args):
    return {'id': 'call-1', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)}}


def _prefetch(task, call):
    """The provider streams one call; the accumulator pre-executes it."""
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    accumulator = StreamingToolAccumulator(
        task, task['config']['projectPath'], round_num=1, project_enabled=True)
    try:
        accumulator.on_tool_call_ready(call)
        published = accumulator.inject_into_cache(task)
        return published, accumulator.announced_tc_map.get(call['id'])
    finally:
        accumulator.close()


def _dispatch(task, name, args, monkeypatch, *, streamed):
    """One model round's call through the public pipeline.

    Returns how many prefetched results the stream published, the hit sources
    the round finalized with and the model-visible tool results.
    ``_finalize_tool_round`` is the documented recorder seam.
    """
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    from lib.tasks_pkg.handlers import project as handler
    finalized = []

    def record(_task, _rn, _row, results, **_kwargs):
        finalized.extend(results)

    monkeypatch.setattr(pipeline, '_finalize_tool_round', record)
    monkeypatch.setattr(handler, '_finalize_tool_round', record)
    call = _call(name, args)
    rn, row = 1, {'query': name, 'toolCallId': call['id']}
    published = 0
    if streamed:
        published, (rn, row) = _prefetch(task, call)
    results = []
    pipeline.execute_tool_pipeline(
        task, [(call, name, call['id'], args, rn, row, None)], task['config'],
        task['config']['projectPath'], True, None, results, [], 1, 'test-model')
    return (published, [meta.get('source') for meta in finalized],
            '\n'.join(str(message.get('content')) for message in results))


@pytest.mark.parametrize('streamed', [False, True], ids=['handler', 'streamed'])
@pytest.mark.parametrize('name, args', list(CALLS.values()), ids=list(CALLS))
def test_project_read_acquires_its_nested_rules_on_every_executor(
        task, monkeypatch, name, args, streamed):
    messages = [{'role': 'user', 'content': 'inspect sub'}]
    assert ROOT_RULE in _prepare_round(task, messages)

    published, sources, _ = _dispatch(task, name, args, monkeypatch, streamed=streamed)

    assert published == int(streamed)
    assert sources and ('Prefetch' in sources) is streamed
    delivered = _prepare_round(task, messages)
    assert SUB_RULE in delivered
    assert OTHER_RULE not in delivered
    assert ROOT_RULE not in delivered


@pytest.mark.parametrize('streamed', [False, True], ids=['handler', 'streamed'])
def test_read_whose_rules_exceed_the_bound_is_never_served_without_them(
        task, monkeypatch, streamed):
    sub_rules = task['config']['projectPath'] + '/sub/AGENTS.md'
    with open(sub_rules, 'w', encoding='utf-8') as handle:
        handle.write('x' * (MAX_GUIDANCE_BYTES + 1))
    messages = [{'role': 'user', 'content': 'inspect sub'}]
    _prepare_round(task, messages)

    published, sources, served = _dispatch(
        task, 'read_files', {'path': 'sub/module.py'}, monkeypatch, streamed=streamed)

    assert published == 0
    assert 'Prefetch' not in sources
    assert FILE_TEXT not in served
    assert 'guidance' in served.lower()


def test_file_grant_scope_reads_no_rule_files_on_the_streamed_path(task, monkeypatch):
    """The handler leaves a file grant's confined scope before guidance runs,
    so a streamed read under that grant must not read rule files either.

    The grant is a real FileScope doing descriptor-confined I/O; only its
    durable dispatch ownership lookups are synthetic.
    """
    import os
    from lib.project_dispatch import admission, scope
    root = task['config']['projectPath']
    metadata = os.stat(root)
    grant = scope.FileScope(task, root, (metadata.st_dev, metadata.st_ino), 'read_files',
                            ('sub/module.py',), ('sub',), ())
    monkeypatch.setattr(admission, 'is_dispatch_task', lambda _task: True)
    monkeypatch.setattr(admission, 'validate_task', lambda _task: {'owner': 7})
    monkeypatch.setattr(admission, 'owned_workspace',
                        lambda _task, _binding: {'workspace_path': root})
    monkeypatch.setattr(scope, 'admit_tool', lambda *_args, **_kwargs: grant)
    # The prefetch decides through admit_call, which also returns its admission.
    monkeypatch.setattr(scope, 'admit_call', lambda *_args, **_kwargs: (grant, None))
    messages = [{'role': 'user', 'content': 'inspect sub'}]
    _prepare_round(task, messages)

    published, _ = _prefetch(task, _call('read_files', {'path': 'sub/module.py'}))

    assert published == 1
    assert FILE_TEXT in str(task['_tool_result_cache'])
    assert SUB_RULE not in _prepare_round(task, messages)
