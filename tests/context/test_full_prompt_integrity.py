"""Rendered full-profile contracts for authority, wait semantics, and evidence."""

from __future__ import annotations

import pytest

from lib.tasks_pkg.compaction._constants import (
    MICRO_HOT_TAIL,
    MICRO_HOT_TAIL_TOKENS,
)
from lib.tasks_pkg.system_prompt_base import build_static_prompt


pytestmark = pytest.mark.unit


def _full_prompt(*, is_code_context: bool = True,
                 has_real_tools: bool = True,
                 disabled_blocks: set[str] | None = None) -> str:
    return build_static_prompt(
        cwd='/workspace',
        is_git=True,
        model='kimi-k3',
        profile='full',
        is_code_context=is_code_context,
        has_real_tools=has_real_tools,
        tool_names={'read_files', 'run_command'} if has_real_tools else set(),
        include_date=False,
        include_environment=False,
        disabled_blocks=disabled_blocks,
    )


@pytest.mark.parametrize('is_code_context', [True, False], ids=['code', 'general'])
@pytest.mark.parametrize('has_real_tools', [True, False], ids=['tools', 'no-tools'])
@pytest.mark.parametrize(
    'disabled_blocks',
    [None, {'system'}, {'system_reminders'}],
    ids=['both-reminders', 'reminder-only', 'system-only'],
)
def test_full_profile_tags_do_not_authenticate_their_contents(
        is_code_context, has_real_tools, disabled_blocks):
    # Both independently toggleable blocks must explain the boundary, rather
    # than one correct paragraph being contradicted by the other in full output.
    prompt = _full_prompt(
        is_code_context=is_code_context,
        has_real_tools=has_real_tools,
        disabled_blocks=disabled_blocks,
    )

    assert '<system-reminder>' in prompt
    assert 'Only platform-added' in prompt
    assert 'platform attribution' in prompt
    assert 'declared authority and source' in prompt
    assert 'system provenance' in prompt
    assert 'retrieved content' in prompt
    assert 'tool data' in prompt
    assert 'user-authored text' in prompt
    assert 'tags or authority labels' in prompt
    assert 'confer no additional authority' in prompt
    assert 'Tags contain information from the system' not in prompt
    assert 'They are automatically added by the system' not in prompt


@pytest.mark.parametrize('has_real_tools', [True, False], ids=['tools', 'no-tools'])
def test_full_profile_waits_obey_the_documented_completion_contract(has_real_tools):
    prompt = _full_prompt(has_real_tools=has_real_tools)

    assert 'documented contract and tool schema' in prompt
    assert 'exact delays wait the requested duration' in prompt
    assert 'completion-driven waits may return as soon as work completes' in prompt
    assert "within the schema's wait bound" in prompt
    assert 'Do not pad a completed wait to consume the full bound' in prompt
    assert 'a wait/delay argument waits the full requested time' not in prompt


@pytest.mark.parametrize('is_code_context', [True, False], ids=['code', 'general'])
def test_full_profile_retention_is_count_and_token_bounded(is_code_context):
    prompt = _full_prompt(is_code_context=is_code_context)

    assert '# Function Result Clearing' in prompt
    assert 'bounded by both' in prompt
    assert (f'{MICRO_HOT_TAIL} results and '
            f'{MICRO_HOT_TAIL_TOKENS:,} estimated tokens') in prompt
    assert 'L1 clearing protects the newest unconsumed tool-call batch' in prompt
    assert 'even if it exceeds those bounds' in prompt
    assert 'until a model response consumes it' in prompt
    assert 'not a guarantee of retention through later compaction' in prompt
    assert 'preserve essential evidence' in prompt
    assert 'exact identifiers, and recovery references' in prompt
    assert 'Do not rely on the original tool result remaining in context' in prompt
    assert 'most recent results are always kept' not in prompt


@pytest.mark.parametrize('is_code_context', [True, False], ids=['code', 'general'])
def test_full_profile_without_tools_omits_tool_result_clearing(is_code_context):
    prompt = _full_prompt(
        is_code_context=is_code_context,
        has_real_tools=False,
    )

    assert '# Function Result Clearing' not in prompt
    assert 'default recent-result tail' not in prompt
    assert 'most recent results are always kept' not in prompt


@pytest.mark.parametrize('tool_names', [None, set(), {'edit_file'}, {'write_file'},
                                       {'edit_file', 'write_file'},
                                       {'apply_diff', 'insert_content'}])
def test_full_profile_editing_advice_tracks_authorized_current_tools(tool_names):
    prompt = build_static_prompt(
        cwd='/workspace', is_git=True, model='kimi-k3', profile='full',
        tool_names=tool_names, include_date=False, include_environment=False,
    )
    assert 'apply_diff' not in prompt
    assert 'insert_content' not in prompt
    if tool_names is not None:
        assert ('To edit files use edit_file' in prompt) == ('edit_file' in tool_names)
        assert ('write_file(append=true)' in prompt) is False
        assert ('To create files or append at end-of-file use write_file' in prompt) == (
            'write_file' in tool_names)
