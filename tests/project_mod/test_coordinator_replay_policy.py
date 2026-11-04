"""Coordinator continuations keep the warm model-replay prefix of their request.

Incident 2026-10-26/27, conv coordinator-1a390889aa954da2b18551d1b4a32625:
every Coordinator request logged ``[ModelReplay] restore=policy_changed
fallback=full_history`` (324 -> 539 rebuilt messages, 0% first-round cache
hit, 315K-484K prompt tokens written per request). Human Coordinator turns pin
their replay policy from the browser config; continuations were admitted from
the frozen worker capability policy alone, and the Coordinator role's tool
setting reached human turns only after their policy was pinned. Every
human/continuation alternation therefore drifted on chatMode, memoryEnabled,
modelRef, preferredProviderId, preset, routing, tools and
contextExperimentFlags although nobody changed a setting.
"""
from copy import deepcopy

import pytest

from tests.project_mod.test_coordinator_continuation import controls, ready, run
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from tests.model_routing.test_model_replay_stability import _turn

pytestmark = pytest.mark.unit

# Keep the imported fixtures' registered aliases visible to static tooling.
__all__ = ('base_runtime', 'context', 'run', 'runtime')

MODEL = 'claude-opus-5.5'
MODEL_REF = {'creator_id': 'anthropic', 'model_id': MODEL}
# Durable settings of the incident Coordinator (inspector metadata; MCP wire
# blob and display-only counters dropped, project path mapped to the fixture).
SETTINGS = {
    'activeFlow': '', 'autoApply': True, 'autoTranslate': True, 'autopilotEnabled': False,
    'browserEnabled': False, 'chatMode': 'chat', 'codeExecEnabled': True, 'delegateMode': False,
    'desktopEnabled': False, 'fetchEnabled': True, 'humanGuidanceEnabled': False,
    'imageGenEnabled': False, 'imageGenMode': False, 'mcpToolExposure': 'auto',
    'memoryEnabled': True, 'model': MODEL, 'modelRef': MODEL_REF, 'planMode': False,
    'preferredProviderId': '', 'preset': MODEL, 'projectPath': '/project',
    'projectPaths': ['/project'], 'provider_id': 'example-corp', 'readOnlyPaths': [],
    'schedulerEnabled': False, 'searchMode': 'multi', 'thinkingDepth': None, 'uiLang': 'zh',
}
# The composer snapshot every browser send submits (_buildToolbarOverrides).
TOOLBAR = {
    'maxTokens': 128000, 'model': MODEL, 'systemPrompt': '', 'systemPromptMode': 'append',
    'systemPromptBlocks': {}, 'thinkingDepth': None, 'preferredProviderId': '',
    'modelRef': MODEL_REF, 'routing': {}, 'searchMode': 'multi', 'fetchEnabled': True,
    'codeExecEnabled': True, 'memoryEnabled': True, 'schedulerEnabled': False,
    'browserEnabled': False, 'desktopEnabled': False, 'imageGenEnabled': False,
    'humanGuidanceEnabled': False, 'autopilot': False, 'activeFlow': '', 'chatMode': 'chat',
    'planMode': False, 'autoTranslate': True, 'uiLang': 'zh', 'autoApply': True,
    'browserClientId': 'browser-a', 'cache': {},
    'tools': {'toolSearch': 'auto', 'executionScope': 'available', 'programmaticCalling': 'auto'},
    'responses': {'transport': 'sse', 'reasoningMode': 'standard', 'verbosity': 'medium',
                  'imageDetail': 'auto', 'promptProfile': 'auto'},
    'compaction': {}, 'serverModel': MODEL,
}


def accepted_human_config(r, **toolbar):
    """The accepted attempt config of one browser-submitted human request."""
    from lib.conv_config import resolve_conv_config
    from lib.conversation_sync.execution_identity import capture_execution_identity
    from lib.tasks_pkg.plan_mode import normalize_interaction_mode_runtime_config
    submitted = resolve_conv_config(conv_settings=SETTINGS, overrides={**TOOLBAR, **toolbar},
                                    server_defaults={'serverModel': MODEL}, is_active=True)
    return capture_execution_identity(
        normalize_interaction_mode_runtime_config(submitted), 7, r.c.principal)


def ingress(monkeypatch, accepted, *, turn_id, attempt_id):
    """Run the real executor entry up to the point that pins the request policy."""
    import lib.conversation_sync.task_start as task_start
    pinned = []

    def build_api_messages(conversation_id, turn, config, *, user_id, **kwargs):
        pinned.append(deepcopy(config))

    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr('lib.turn_lifecycle.build_api_messages', build_api_messages)
    config = {**deepcopy(accepted), '_turnOwnerUserId': 7, '_turnId': turn_id,
              '_attemptId': attempt_id, 'excludeLast': True}
    assert task_start.start_conversation_attempt_executor('coordinator', config) == (
        None, 'conversation_not_found')
    return pinned[0]


class Replay:
    """Restore then publish each request through the real model-replay owner."""

    def __init__(self, tmp_path, monkeypatch):
        from lib.storage.model_replay_cache import ModelReplayCache
        from lib.tasks_pkg import model_replay
        self.owner = model_replay
        self.store = ModelReplayCache(root=tmp_path / 'replay', max_bytes=2_000_000)
        monkeypatch.setattr(model_replay, 'ModelContinuationStore', lambda: self.store)
        self.rows = []

    def query(self, operation, payload, **kwargs):
        assert operation == 'turn.get'
        return deepcopy(next(row for row in self.rows if row['turnId'] == payload['turn_id']))

    def request(self, config):
        index = len(self.rows)
        target = _turn(f'a{index}', 'assistant', '', index + 2)
        target['projection'] = {}
        self.rows += [_turn(f'u{index}', 'human', f'request {index}', index + 1), target]
        config = deepcopy(config)
        self.owner.restore_prefix(self.rows, config, conversation_id='coordinator', user_id=7)
        outcome = (config['_modelReplayStatus'], config.get('_modelReplayPolicyDrift', []))
        target.update(status='completed', projection={'content': f'done {index}'})
        # Like the incident's adaptive-thinking history: signed reasoning is
        # provider-bound, so only an unchanged policy may reuse the prefix.
        messages = [{'role': 'user', 'content': row['projection']['content']} if row['actor'] == 'human'
                    else {'role': 'assistant', 'content': row['projection']['content'],
                          'reasoning_content': 'signed thinking'} for row in self.rows]
        task = {'config': config, '_userId': 7, 'convId': 'coordinator', '_turnId': target['turnId'],
                'model': config.get('model'), 'content': f'done {index}', 'messages': messages}
        from lib import storage
        with pytest.MonkeyPatch.context() as patch:
            # Settlement reads only the target Turn; dispatch keeps its own client.
            patch.setattr(storage, 'get_storage_client', lambda: self)
            self.owner.publish_completed_task(task)
        assert task['_modelReplayPublishStatus'] == 'stored'
        return outcome


def human_turn(r, monkeypatch, **toolbar):
    """Accept, start and tool-assemble the human request that may delegate."""
    from lib.project_dispatch.delegation import capture_worker_policy
    from lib.project_dispatch.repository import CONTRACT
    accepted = accepted_human_config(r, **toolbar)
    r.attempts['coordinator-attempt']['config'] = deepcopy(accepted)
    pinned = ingress(monkeypatch, accepted, turn_id='coordinator-turn',
                     attempt_id='coordinator-attempt')
    # Tool assembly issues the worker policy from this same runtime config.
    r.task['config'] = deepcopy(pinned)
    names = CONTRACT['continuationTools'] + ['run_command', 'read_files', 'write_file']
    capture_worker_policy(r.task, r.task['config'], [{'function': {'name': name}} for name in names],
                          pinned['model'], worker_model=pinned['model'])
    return pinned


def continuation(r, monkeypatch):
    """Admit the result-driven continuation exactly as the dispatch service does."""
    from lib.project_dispatch import admission
    ready(r)
    row = controls(r)[-1]
    trusted = admission.TrustedDispatch(r.c.repo.admission(row['id']))
    accepted = trusted.capture({}, 7, trusted.principal, 'coordinator')
    return ingress(monkeypatch, accepted, turn_id=row['execution']['turnId'],
                   attempt_id=row['execution']['attemptId'])


def test_unchanged_policy_restores_across_human_and_continuation_requests(run, monkeypatch, tmp_path):
    r = run
    replay = Replay(tmp_path, monkeypatch)
    human = human_turn(r, monkeypatch)
    resumed = continuation(r, monkeypatch)
    assert replay.request(human)[0] == 'checkpoint_missing'
    status, drift = replay.request(resumed)
    assert (status, drift) == ('restored', []), (
        f'continuation cold-rebuilt the request prefix: status={status} drift={drift}')
    status, drift = replay.request(human_turn(r, monkeypatch))
    assert (status, drift) == ('restored', []), (
        f'next human request cold-rebuilt the continuation prefix: status={status} drift={drift}')


def test_continuation_runs_under_the_request_selection_and_its_grant(run, monkeypatch):
    from lib.context_experiment_flags import normalize_context_experiment_flags
    from lib.project_dispatch.admission import restore_dispatch_identity
    r = run
    human = human_turn(r, monkeypatch)
    resumed = continuation(r, monkeypatch)
    for key in ('chatMode', 'memoryEnabled', 'modelRef', 'preferredProviderId', 'routing',
                'preset', 'thinkingEnabled'):
        assert resumed.get(key) == human.get(key), key
    assert normalize_context_experiment_flags(resumed) == normalize_context_experiment_flags(human)
    # Capabilities stay with the issued grant and its fixed overrides.
    issued = controls(r)[-1]['request']['delegation']['policy']['config']
    # The issued tools are the Workers' (the human's own settings); the role
    # keeps its narrowed settings, so they are compared separately below.
    assert all(resumed[key] == value for key, value in issued.items()
               if key not in ('humanGuidanceEnabled', 'tools'))
    assert resumed['tools'] == human['tools'] and resumed['tools']['programmaticCalling'] == 'off'
    assert resumed['model'] == controls(r)[-1]['request']['model'] == MODEL
    assert (resumed['autopilot'], resumed['planMode'], resumed['autoTranslate']) == (False, False, False)
    # Asking the human is the role's own channel: the issued policy froze
    # ask_human, so the continuation may ask like the human request (Workers never).
    assert 'ask_human' in controls(r)[-1]['request']['profile']['tools']
    assert resumed['humanGuidanceEnabled'] is True and human['humanGuidanceEnabled'] is True
    assert issued['humanGuidanceEnabled'] is False
    # Request-scoped authority never crosses: no browser pin, identity or Flow.
    for key in ('browserClientId', '_conversationExecutionIdentity', 'flowBuiltin', 'flowId',
                'delegateMode', 'grantedToolPermissions'):
        assert key not in resumed, key
    principal, attended = restore_dispatch_identity(resumed)
    assert principal.kind == 'system' and attended is False


def test_route_naming_another_model_is_not_inherited(run, monkeypatch):
    r = run
    human_turn(r, monkeypatch)
    accepted = r.attempts['coordinator-attempt']['config']
    accepted['modelRef'] = {'creator_id': 'openai', 'model_id': 'gpt-6'}
    resumed = continuation(r, monkeypatch)
    assert resumed['model'] == MODEL
    for key in ('modelRef', 'preferredProviderId', 'routing', 'preset'):
        assert key not in resumed, key
    assert resumed['memoryEnabled'] is True and resumed['chatMode'] == 'chat'


ROUTE_CHANGES = [
    {'model': 'gpt-6', 'modelRef': {'creator_id': 'openai', 'model_id': 'gpt-6'}},
    {'preferredProviderId': 'provider-b'},
]
POLICY_CHANGES = [
    ({'responses': {**TOOLBAR['responses'], 'verbosity': 'high'}}, {'contextExperimentFlags'}),
    ({'tools': {**TOOLBAR['tools'], 'toolSearch': 'off'}}, {'tools', 'contextExperimentFlags'}),
    ({'systemPrompt': 'Answer tersely.'}, {'systemPrompt'}),
]


@pytest.mark.parametrize('change, expected', POLICY_CHANGES + [(change, None) for change in ROUTE_CHANGES])
def test_real_policy_change_still_forces_a_full_rebuild(run, monkeypatch, tmp_path, change, expected):
    r = run
    replay = Replay(tmp_path, monkeypatch)
    first = human_turn(r, monkeypatch)
    resumed = continuation(r, monkeypatch)
    assert replay.request(first)[0] == 'checkpoint_missing'
    assert replay.request(resumed)[0] == 'restored'
    changed = human_turn(r, monkeypatch, **change)
    # A continuation of the earlier request keeps that request's policy, so it
    # cannot reuse the changed request's prefix either.
    for config in (changed, resumed):
        status, drift = replay.request(config)
        if expected is None:
            # Route changes migrate only portable text; signed reasoning is stripped.
            from lib.tasks_pkg.model_replay import PORTABLE_DRIFT_FIELDS
            assert status == 'restored_portable', status
            assert drift and set(drift) <= PORTABLE_DRIFT_FIELDS, drift
        else:
            assert status == 'policy_changed' and expected <= set(drift), (status, drift)


def test_coordinator_inert_program_setting_keeps_the_prefix(run, monkeypatch, tmp_path):
    """The Coordinator never runs programs; this toggle cannot change its tools."""
    r = run
    replay = Replay(tmp_path, monkeypatch)
    replay.request(human_turn(r, monkeypatch))
    changed = human_turn(r, monkeypatch, tools={**TOOLBAR['tools'], 'programmaticCalling': 'on'})
    assert changed['tools']['programmaticCalling'] == 'off'
    assert replay.request(changed) == ('restored', [])


def test_ordinary_project_conversation_keeps_its_submitted_tool_settings(run, monkeypatch):
    r = run
    r.c.binding.value['conversationId'] = 'another-coordinator'
    pinned = ingress(monkeypatch, accepted_human_config(r), turn_id='coordinator-turn',
                     attempt_id='coordinator-attempt')
    assert pinned['tools'] == TOOLBAR['tools']


def test_failed_role_lookup_keeps_submitted_settings_for_tool_assembly(run, monkeypatch):
    from lib.conversations import project_coordination

    class Unavailable:
        def read(self, path):
            raise RuntimeError('binding store unavailable')
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository', lambda owner: Unavailable())
    pinned = ingress(monkeypatch, accepted_human_config(run), turn_id='coordinator-turn',
                     attempt_id='coordinator-attempt')
    assert pinned['tools'] == TOOLBAR['tools']
