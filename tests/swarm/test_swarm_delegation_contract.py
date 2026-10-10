"""Delegation contract: per-spawn tool allowlists and role mapping.

The orchestrator keeps its own context clean by handing sub-agents exactly
what they need at spawn time: an explicit narrow-only tool allowlist (MCP
tools the orchestrator never saw included), and by receiving each completed
agent's FULL natural-text result in the swarm-update payload — no preview
truncation, no second round trip. These tests pin the contract semantics;
they never exercise the LLM path.
"""
import json

import pytest

from lib.agent_inbox._format import format_swarm_update
from lib.swarm.registry import resolve_spawnable_role, scope_tools_for_spec
from lib.swarm.types import SubTaskSpec

pytestmark = pytest.mark.unit


def _tool(name):
    return {'type': 'function', 'function': {'name': name, 'parameters': {}}}


def _names(tools):
    return {t['function']['name'] for t in tools}


# ─────────────────────────────────────────────────────────
#  scope_tools_for_spec — explicit allowlist, narrow-only
# ─────────────────────────────────────────────────────────

def test_spec_tools_intersect_role_scope():
    full = [_tool('read_files'), _tool('edit_file'), _tool('run_command')]
    spec = SubTaskSpec(role='general', objective='x', tools=['read_files'])
    assert _names(scope_tools_for_spec(spec, full)) == {'read_files'}


def test_spec_tools_cannot_widen_role_scope():
    # A specialist whose hint covers only read_files must NOT gain run_command
    # just because the explicit allowlist names it.
    full = [_tool('read_files'), _tool('run_command')]
    spec = SubTaskSpec(role='scoped_reader', objective='x',
                       tools=['read_files', 'run_command'])
    # Unknown roles behave like general (no hint), so register a hint via
    # monkeypatch-free path: use a real specialist role instead.
    spec = SubTaskSpec(role='reviewer', objective='x',
                       tools=['read_files', 'run_command'])
    from lib.swarm.registry import get_tools_for_role
    expected = set(get_tools_for_role('reviewer')) & {'read_files', 'run_command'}
    assert _names(scope_tools_for_spec(spec, full)) == expected


def test_spec_tools_denylist_always_wins():
    full = [_tool('read_files'), _tool('spawn_agents'), _tool('ask_human')]
    spec = SubTaskSpec(role='general', objective='x',
                       tools=['read_files', 'spawn_agents', 'ask_human'])
    assert _names(scope_tools_for_spec(spec, full)) == {'read_files'}


def test_spec_empty_tools_keeps_role_default():
    full = [_tool('read_files'), _tool('edit_file'), _tool('spawn_agents')]
    spec = SubTaskSpec(role='general', objective='x')
    assert _names(scope_tools_for_spec(spec, full)) == {'read_files', 'edit_file'}


# ─────────────────────────────────────────────────────────
#  resolve_spawnable_role — harness subagent_type mapping
# ─────────────────────────────────────────────────────────

def test_resolve_spawnable_role_maps_known_case_insensitively():
    assert resolve_spawnable_role('Researcher') == 'researcher'
    assert resolve_spawnable_role('CODER') == 'coder'


def test_resolve_spawnable_role_falls_back_for_unknown_and_flow_roles():
    assert resolve_spawnable_role('general-purpose') == 'general'
    assert resolve_spawnable_role('Explore') == 'general'
    assert resolve_spawnable_role('planner') == 'general'  # flow-internal
    assert resolve_spawnable_role('') == 'general'
    assert resolve_spawnable_role(None) == 'general'


def test_task_envelope_maps_known_subagent_type():
    from lib.tool_input_repair import validate_then_repair
    out, _log = validate_then_repair('spawn_agents', {
        'description': 'Research the topic',
        'prompt': 'Find authoritative sources.',
        'subagent_type': 'Researcher'})
    assert out['agents'] == [
        {'objective': 'Find authoritative sources.', 'role': 'researcher'}]


def test_task_envelope_unknown_subagent_type_stays_inert():
    from lib.tool_input_repair import validate_then_repair
    out, _log = validate_then_repair('spawn_agents', {
        'description': 'Explore the auth flow',
        'prompt': 'Trace how login works.',
        'subagent_type': 'Explore'})
    assert out['agents'] == [{'objective': 'Trace how login works.'}]


# ─────────────────────────────────────────────────────────
#  Full-result swarm-update payloads (no truncation)
# ─────────────────────────────────────────────────────────

_LONG_ANSWER = ('analysis paragraph\n\n' * 400) + '```py\ncode\n```' + (
    '\n\ntail paragraph' * 200)


def test_swarm_update_carries_full_result_untruncated():
    payload = format_swarm_update(
        agent_id='a', role='researcher', status='completed',
        elapsed_seconds=1.0, tokens=10, result=_LONG_ANSWER)
    assert '<result>' in payload and '<preview>' not in payload
    # Natural text is returned whole — the tail must be present.
    assert 'tail paragraph' in payload
    assert payload.count('analysis paragraph') == 400


def test_swarm_update_escapes_full_result():
    payload = format_swarm_update(
        agent_id='a', role='r', status='completed',
        elapsed_seconds=1.0, tokens=10, result='hello & <world>')
    assert 'hello &amp; &lt;world&gt;' in payload


def test_swarm_update_omits_empty_result():
    payload = format_swarm_update(
        agent_id='a', role='r', status='failed',
        elapsed_seconds=1.0, tokens=10, result='', error='boom')
    assert '<result>' not in payload
    assert '<error>boom</error>' in payload


# ─────────────────────────────────────────────────────────
#  spawn_agents parameter parsing → SubTaskSpec
# ─────────────────────────────────────────────────────────

def test_spawn_agents_parses_delegation_params(monkeypatch, tmp_path):
    import uuid
    from lib import agent_inbox
    from lib.swarm import integration as integ

    task_id = 'tdelegate-' + uuid.uuid4().hex[:8]

    class _FakeSession:
        def __init__(self):
            self.specs = []
            self.user_id = 1
            self.is_terminated = False
            self.task_id = task_id
            self.conv_id = ''
            self._parent_task_proxy = {'config': {}}

        def run_in_background(self):
            return None

    captured = {}

    def _fake_master(**kw):
        captured['specs'] = list(kw['specs'])
        return _FakeSession()

    monkeypatch.setattr('lib.swarm.integration._tools.MasterOrchestrator',
                        lambda **kw: _fake_master(**kw))
    monkeypatch.setattr('lib.swarm.integration._tools._get_session',
                        lambda key: None)
    monkeypatch.setattr('lib.swarm.integration._tools._set_session',
                        lambda *a, **kw: None)
    monkeypatch.setattr('lib.swarm.persistence.save_session',
                        lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(agent_inbox, 'untombstone', lambda *a, **kw: None)
    try:
        raw = integ.execute_swarm_tool('spawn_agents', {'agents': [{
            'objective': 'Research with the pack',
            'tools': ['mcp__github__get_issue', 'read_files'],
        }, {
            'objective': 'Plain worker',
            'tools': 'not-a-list',
        }]}, task={'id': task_id, '_userId': 1, 'status': 'running'})
    finally:
        agent_inbox.reset_for_test(task_id)
    handle = json.loads(raw)
    assert handle['status'] == 'async_launched'
    first, second = captured['specs']
    assert first.tools == ['mcp__github__get_issue', 'read_files']
    # Invalid shapes degrade safely instead of failing the spawn.
    assert second.tools == []


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
