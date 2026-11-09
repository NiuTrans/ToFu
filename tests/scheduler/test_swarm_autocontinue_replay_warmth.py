"""R1 cold-rebuild: swarm auto-continuation must keep the warm replay prefix.

The swarm auto-continuation turn runs the SAME conversation, model and
capability policy as the UI turns that published the model-replay
checkpoint. The old checkpoint policy witness must therefore match the
witness computed from the continuation's task config; a mismatch forces a
policy_changed full-history rebuild that re-bills the whole conversation
prefix (10-25 22:56, conv mvl5wxpj: restore=policy_changed
fallback=full_history messages=808).
"""
import pytest

pytestmark = pytest.mark.unit

# The conversation's durable settings at the 10-25 incident (metadata.settings
# of conv mvl5wxpjxiylpe, verbatim from the authority, MCP wire blob dropped).
INCIDENT_SETTINGS = {
    'activeFlow': '', 'autoApply': True, 'autoTranslate': True,
    'autopilotEnabled': False, 'browserEnabled': False, 'chatMode': 'chat',
    'codeExecEnabled': True, 'desktopEnabled': False, 'fetchEnabled': True,
    'humanGuidanceEnabled': False, 'imageGenEnabled': False,
    'mcpToolExposure': 'auto', 'memoryEnabled': True,
    'model': 'claude-opus-5.5',
    'modelRef': {'creator_id': 'anthropic', 'model_id': 'claude-opus-5.5'},
    'planMode': False, 'preferredProviderId': '',
    'preset': 'claude-opus-5.5',
    'projectPath': '/mnt/proj', 'projectPaths': ['/mnt/proj'],
    'provider_id': 'sankuai', 'readOnlyPaths': [], 'schedulerEnabled': False,
    'searchMode': 'multi', 'thinkingDepth': 'max',
}


def _ui_turn_config(settings):
    """The UI turn config exactly as the interactive request resolves it."""
    from lib.conv_config import resolve_conv_config
    return resolve_conv_config(conv_settings=dict(settings), is_active=False)


def _swarm_continuation_config(settings):
    from lib.scheduler.conversation_dispatch import _build_swarm_continuation_config
    return _build_swarm_continuation_config(
        dict(settings), {}, {'_swarmAutoContinue': True, 'schedulerEnabled': False})


def _publish_then_restore(tmp_path, monkeypatch, publish_config, resume_config):
    """Publish a checkpoint under publish_config, restore under resume_config."""
    from lib import storage
    from lib.storage.model_replay_cache import ModelReplayCache
    from lib.tasks_pkg import model_replay as replay
    from tests.model_routing.test_model_replay_stability import _turn

    store = ModelReplayCache(root=tmp_path / 'replay', max_bytes=2_000_000)
    monkeypatch.setattr(replay, 'ModelContinuationStore', lambda: store)
    rows = [_turn('u1', 'human', 'objective', 1), _turn('a1', 'assistant', '', 2)]

    # A first (resume) pass pins the ingress policy on the publisher's config,
    # then the turn completes and Sidecar publishes the settled checkpoint.
    replay_config = dict(publish_config)
    replay.restore_prefix(rows, replay_config, conversation_id='c', user_id=1)
    rows[-1].update(status='completed', projection={'content': 'done'})

    class Client:
        def query(self, operation, payload, **kwargs):
            assert operation == 'turn.get'
            import copy
            return copy.deepcopy(rows[-1])

    monkeypatch.setattr(storage, 'get_storage_client', lambda: Client())
    messages = [{'role': 'user', 'content': 'objective'},
                {'role': 'assistant', 'content': 'done'}]
    task = {'config': replay_config, '_userId': 1, 'convId': 'c',
            '_turnId': 'a1', 'model': replay_config.get('model'),
            'content': 'done', 'messages': [dict(m) for m in messages]}
    replay.publish_completed_task(task)
    assert task['_modelReplayPublishStatus'] == 'stored'

    next_config = dict(resume_config)
    target = _turn('u2', 'human', 'continue', 3)
    target['projection'] = {}
    restored, covered = replay.restore_prefix(
        rows + [target], next_config,
        conversation_id='c', user_id=1)
    return next_config, restored, covered


def test_swarm_autocontinue_restores_the_warm_checkpoint(tmp_path, monkeypatch):
    from lib.tasks_pkg import model_replay

    publish_config = _ui_turn_config(INCIDENT_SETTINGS)
    resume_config = _swarm_continuation_config(INCIDENT_SETTINGS)
    assert resume_config is not None

    next_config, restored, covered = _publish_then_restore(
        tmp_path, monkeypatch, publish_config, resume_config)
    status = next_config['_modelReplayStatus']
    assert status == 'restored', (
        f'swarm auto-continuation cold-rebuilt the prefix: '
        f'status={status}; publish policy='
        f'{model_replay.policy_witness(publish_config)[:12]} resume policy='
        f'{model_replay.policy_witness(resume_config)[:12]}')
    assert covered == 2
    assert restored[-1]['content'] == 'done'


def test_policy_witness_reports_the_drifted_fields(tmp_path, monkeypatch):
    """A refused restore must NAME the drifted fields, not just 'policy_changed'."""
    publish_config = _ui_turn_config(INCIDENT_SETTINGS)
    resume_config = _swarm_continuation_config(INCIDENT_SETTINGS)
    resume_config['memoryEnabled'] = False
    next_config, _, _ = _publish_then_restore(
        tmp_path, monkeypatch, publish_config, resume_config)
    assert next_config['_modelReplayStatus'] == 'policy_changed'
    drift = next_config.get('_modelReplayPolicyDrift')
    assert drift, 'policy_changed restore left no drift evidence on the config'
    assert 'memoryEnabled' in drift
    assert all(field != '_modelReplayPolicyDrift' for field in drift)


def test_real_policy_change_on_continuation_still_refuses(tmp_path, monkeypatch):
    """Mechanism, not instance: a genuine settings change must cold-rebuild."""
    publish_config = _ui_turn_config(INCIDENT_SETTINGS)
    resume_config = _swarm_continuation_config(
        {**INCIDENT_SETTINGS, 'searchMode': 'off'})
    next_config, restored, covered = _publish_then_restore(
        tmp_path, monkeypatch, publish_config, resume_config)
    assert restored == [] and covered == 0
    assert next_config['_modelReplayStatus'] == 'policy_changed'
    assert 'searchMode' in next_config.get('_modelReplayPolicyDrift', ())
