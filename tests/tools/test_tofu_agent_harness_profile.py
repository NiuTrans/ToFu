"""Harness opt-in and conservative terminal protocol classification contracts."""

import pytest


pytestmark = pytest.mark.unit
from tofu_agent.cli import _parser
from tofu_agent.harness_profile import (
    apply_harness_profile, classify_longcat_tool_text,
)
from tofu_agent.models import AgentConfigurationError


def test_profile_off_is_identity_and_does_not_infer_model():
    cfg = {'model': 'longcat_flash', 'tools.nativeExposure': 'routed'}
    assert apply_harness_profile(cfg, 'off') == cfg
    assert apply_harness_profile({'harnessProfile': 'off'}, 'longcat_flash') == {
        'harnessProfile': 'off'}


def test_profile_uses_existing_exposure_flags_without_enabling_permissions():
    cfg = {'tools': {'nativeExposure': 'routed'}, 'codeExecEnabled': False}
    result = apply_harness_profile(cfg, 'longcat_flash')
    assert cfg['tools'] == {'nativeExposure': 'routed'}
    assert result['tools']['nativeExposure'] == 'full'
    assert result['tools']['toolSearch'] == 'off'
    assert result['tools.programmaticCalling'] == 'off'
    assert result['codeExecEnabled'] is False
    from lib.tools.gateway import resolve_tool_search_backend
    assert resolve_tool_search_backend(
        result['tools.toolSearch'], protocol='openai') == 'full'


def test_direct_common_schemas_come_from_registry(tmp_path):
    from lib.tools.registry import ToolContext, assemble_tool_list
    from lib.tools.gateway import full_wire_tools
    ctx = ToolContext(
        cfg=apply_harness_profile({}, 'longcat_flash'), task_id='profile-test',
        project_path=str(tmp_path), project_enabled=True,
        search_mode='off', search_enabled=False, fetch_enabled=False,
        code_exec_enabled=False, browser_enabled=False, desktop_enabled=False,
        lean=True, enabled_plugins=set())
    catalog, _ = assemble_tool_list(ctx)
    wire = full_wire_tools(catalog)
    names = {tool['function']['name'] for tool in wire}
    assert {'find_files', 'grep_search', 'read_files', 'edit_file', 'write_file',
            'run_command', 'poll',
            'kill'} <= names
    assert all(any(tool is original for original in catalog) for tool in wire)


@pytest.mark.parametrize('text', [
    'Example: <longcat_tool_call>run_command</longcat_tool_call>',
    '```xml\n<longcat_tool_call>run_command</longcat_tool_call>\n```',
    '"<longcat_tool_call>run_command</longcat_tool_call>"',
    '> <longcat_tool_call>run_command</longcat_tool_call>',
    '<ordinary_xml>hello</ordinary_xml>', '', None,
])
def test_ordinary_quoted_xml_is_not_protocol(text):
    assert classify_longcat_tool_text(text, profile='longcat_flash') == ''


@pytest.mark.parametrize('text', [
    '<longcat_tool_call>run_command</longcat_tool_call>',
    '<longcat_tool_call', '<longcat_arg_key>command',
    '<longcat_tool_call>broken <longcat_arg_key>',
    '<longcat_tool_call>one</longcat_tool_call><longcat_tool_call>two',

    'Let me run this:<longcat_arg_key>command</longcat_arg_key>\n'
    '<longcat_arg_value>echo `pwd`</longcat_arg_value>\n</longcat_tool_call>',
    '```text\nexample\n```\nLet me run this:<longcat_arg_key>command</longcat_arg_key>\n'
    '<longcat_arg_value>echo pwd</longcat_arg_value>\n</longcat_tool_call>',
    'Let me fix this:<longcat_arg_key>replace</longcat_arg_key>\n'
    '<longcat_arg_value>{"old": "x", "new": "y"}</longcat_arg_value>\n'
    '</longcat_tool_call>\n',
])
def test_packets_are_detection_only_and_never_replay_native_side_effects(text):
    assert classify_longcat_tool_text(text) == ''
    assert classify_longcat_tool_text(text, profile='longcat_flash') == 'invalid_protocol'
    assert classify_longcat_tool_text(
        text, profile='longcat_flash', has_native_tool_calls=True) == ''


def test_cli_ab_and_invalid_config():
    assert _parser().parse_args(['run', '--task', 'x']).harness_profile == 'auto'
    assert _parser().parse_args([
        'run', '--harness-profile', 'longcat_flash']).harness_profile == 'longcat_flash'
    with pytest.raises(AgentConfigurationError):
        apply_harness_profile({'harnessProfile': 'automatic'})


@pytest.mark.parametrize('model', ['gpt-5', 'claude-sonnet', 'deepseek-chat', 'longcat-flash'])
def test_auto_preserves_capabilities_and_explicit_policy(model):
    cfg = {'model': model, 'tools': {'nativeExposure': 'routed'},
           'tools.toolSearch': 'auto', 'tools.programmaticCalling': 'on',
           'codeExecEnabled': False}
    result = apply_harness_profile(cfg)
    assert result == dict(cfg, harnessProfile='auto')
    assert 'harnessProfile' not in cfg
    assert apply_harness_profile({}) == {
        'harnessProfile': 'auto', 'tools.nativeExposure': 'full'}


@pytest.mark.parametrize('model, expected', [
    ('longcat-flash', 'invalid_protocol'),
    ('provider/LongCat-Flash', 'invalid_protocol'),
    ('gpt-5', ''), ('claude-sonnet', ''), ('deepseek-chat', ''),
])
def test_auto_protocol_detection_is_model_scoped(model, expected):
    text = '<longcat_tool_call>run_command</longcat_tool_call>'
    assert classify_longcat_tool_text(text, model=model) == expected
    assert classify_longcat_tool_text(text, model=model, profile='off') == ''
    assert classify_longcat_tool_text(text, model=model, has_native_tool_calls=True) == ''
