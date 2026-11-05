"""Auxiliary producers retain evidence and separate engine policy from user input."""
from types import SimpleNamespace

import pytest

from lib.swarm.agent import SubAgent
from lib.swarm.types import SubTaskSpec
from lib.tasks_pkg.context_composer.injections import append_injection
from lib.tasks_pkg.message_builder._prefetch import inject_prefetched_urls
from lib.tasks_pkg.conv_message_builder._load import _append_note_as_tail_user

pytestmark = pytest.mark.unit


def test_url_prefetch_retains_exact_payload_and_receipt():
    messages = [{'role': 'user', 'content': 'read this URL'}]
    task = {'toolRounds': [1]}
    assert inject_prefetched_urls(messages, [('https://a/x.pdf', '[Page 1] full evidence')], task) == 1
    assert messages[0] == {'role': 'user', 'content': 'read this URL'}
    assert messages[-1]['_isUrlPrefetch'] is True
    assert messages[-1]['content'].endswith('[Page 1] full evidence')
    assert task['_contextManifest'][-1]['id'] == 'url_prefetch'


def test_lifecycle_note_preserves_text_and_meta_classification():
    messages = [{'role': 'user', 'content': 'continue'}]
    assert _append_note_as_tail_user(messages, '  outcome unknown\n')
    assert messages[-1]['content'] == '  outcome unknown\n'
    assert messages[-1]['_isMeta'] and messages[-1]['_isLifecycleCaution']


def test_worker_policy_and_artifact_hint_do_not_rewrite_objective():
    store = SimpleNamespace(list_keys=lambda: ['x' * 1000] * 500)
    agent = SubAgent(
        SubTaskSpec(id='aux', role='general', objective='EXACT objective', context='EXACT context'),
        parent_task={'id': 'parent', 'config': {}}, all_tools=[], model='test-model',
        system_prompt_base='User-authored system', artifact_store=store,
        thinking_enabled=False)
    assert agent.messages[0] == {'role': 'system', 'content': 'User-authored system'}
    assert agent.messages[1]['role'] == 'system'
    assert 'tool catalog is your authority' in agent.messages[1]['content']
    objective = next(m for m in agent.messages if m['role'] == 'user' and not m.get('_isMeta'))
    assert objective['content'] == 'EXACT objective\n\n## Context\nEXACT context'
    assert agent.messages[-1]['_contextInjectionId'] == 'worker_artifacts'
    assert len(agent.messages[-1]['content']) < 2200
    assert 'list_artifacts' in agent.messages[-1]['content']
    assert '_contextManifest' not in agent.parent_task


def test_empty_inherited_prompt_never_emits_empty_system_message():
    # Regression: an empty inherited prompt produced messages[0] =
    # {'role': 'system', 'content': ''}, which strict providers reject with a
    # deterministic HTTP 400 (kimi-k3), killing every goal-mode round.
    agent = SubAgent(
        SubTaskSpec(id='aux-empty', role='general', objective='obj'),
        parent_task={'id': 'parent', 'config': {}}, all_tools=[],
        model='test-model', system_prompt_base='', thinking_enabled=False)
    system_messages = [m for m in agent.messages if m.get('role') == 'system']
    assert system_messages, 'worker policy must open the request as system'
    assert all(str(m.get('content') or '').strip() for m in system_messages)
    assert agent.messages[0]['_contextInjectionId'] == 'worker_policy'

def test_runtime_identity_survives_checkpoint_copy():
    messages = []
    append_injection(messages, 'worker_policy', 'scoped tools')
    assert dict(messages[0])['_contextInjectionId'] == 'worker_policy'


def test_dependency_renderer_preserves_explicit_user_context():
    from lib.swarm.scheduler import StreamingScheduler
    from lib.swarm.types import SubAgentResult
    dep = SubTaskSpec(id='dep', role='general', objective='verify')
    spec = SubTaskSpec(id='next', role='general', objective='report', context='explicit user context', depends_on=['dep'])
    scheduler = SimpleNamespace(_completed={'dep': (dep, SubAgentResult(final_answer='confirmed evidence'))})
    evidence = StreamingScheduler._inject_deps_locked(scheduler, spec)
    assert spec.context == 'explicit user context'
    assert 'confirmed evidence' in evidence


def _run_binding(agent_id):
    return {'owner': 1, 'tenant': '', 'conversationId': 'conv-1', 'projectPath': '',
            'swarmKey': 'swarm-1', 'swarmRunId': 'run-1', 'agentId': agent_id,
            'retainedAgentId': '', 'retainedRunId': '', 'retainedScope': ''}


def _sibling_messages(agent_id, objective, checkpoint=None):
    from lib.tasks_pkg.child_invocation import build_child_messages
    if checkpoint is None:
        checkpoint = build_child_messages(objective=objective, system_prompt='Inherited',
                                          role_policy='Role policy')
    return build_child_messages(objective=objective, system_prompt='Inherited',
                                role_policy='Role policy', checkpoint_messages=checkpoint,
                                swarm_message_binding=_run_binding(agent_id))


def _system_wire(messages):
    return [(m['role'], m['content']) for m in messages if m['role'] == 'system']


def test_sibling_run_identity_ends_each_workers_system_contract():
    # Concurrent same-role siblings that shared a byte-identical tools+system
    # prefix lost their own cached history to other upstream cache domains;
    # each sibling's system contract names its run identity instead
    # (docs/LLM_COST_OPTIMIZATION.md, "Sibling cache affinity").
    first = _sibling_messages('agent-a', 'Objective A')
    second = _sibling_messages('agent-bbbb', 'Objective B')

    assert _system_wire(first) != _system_wire(second)
    policy = next(m for m in first if m.get('_contextInjectionId') == 'worker_policy')
    assert policy['role'] == 'system'
    text = policy['content']
    assert 'Team communication: your assignment ID is "agent-a"' in text
    assert (text.index('Role policy') < text.index('Team communication')
            < text.index('Work only on the delegated objective'))
    assert sum('assignment ID' in str(m.get('content')) for m in first) == 1
    assert not any(m.get('_contextInjectionId') == 'worker_team' for m in first)


def test_worker_without_run_binding_keeps_the_role_system_contract():
    from lib.tasks_pkg.child_invocation import build_child_messages
    plain = build_child_messages(objective='Objective A', system_prompt='Inherited',
                                 role_policy='Role policy')
    other = build_child_messages(objective='Objective B', system_prompt='Inherited',
                                 role_policy='Role policy')
    assert _system_wire(plain) == _system_wire(other)
    assert not any('assignment ID' in str(m.get('content')) for m in plain)


def test_checkpoint_rebuild_replaces_the_run_identity():
    original = _sibling_messages('agent-a', 'Objective A')
    # A checkpoint written while the identity rode the retired worker_team carrier.
    append_injection(original, 'worker_team',
                     'Team communication: your assignment ID is "agent-a"')
    rebuilt = _sibling_messages('agent-c', 'Objective A', checkpoint=original)
    identities = [m for m in rebuilt if 'assignment ID' in str(m.get('content'))]
    assert len(identities) == 1
    assert identities[0]['_contextInjectionId'] == 'worker_policy'
    assert '"agent-c"' in identities[0]['content'] and '"agent-a"' not in identities[0]['content']


def test_worker_run_passes_identity_as_binding_not_role_policy(monkeypatch):
    captured = {}

    def capture(*_args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError('captured')

    monkeypatch.setattr('lib.tasks_pkg.child_invocation.create_child_invocation', capture)
    agent = SubAgent(SubTaskSpec(id='aux-team', role='general', objective='obj'),
                     parent_task={'id': 'parent', 'config': {}}, all_tools=[],
                     model='test-model', system_prompt_base='', thinking_enabled=False)
    agent._assignment_message_binding = _run_binding('aux-team')
    agent.run()
    assert captured['swarm_message_binding']['agentId'] == 'aux-team'
    assert 'Team communication' not in captured['role_policy']
