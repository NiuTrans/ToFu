"""Verified context-growth optimizations: aggregate-budget exempt spill,
round-evidence post-tool dedup, and child-invocation provider narrowing."""
import pytest

pytestmark = pytest.mark.unit


# ── FINDING A — aggregate guard spills exempt tools past 2x source allowance ──

def _prose(n_chars):
    line = 'def handler(request):  # a line of ordinary source code\n'
    return (line * (n_chars // len(line) + 1))[:n_chars]


def test_aggregate_spills_oversized_exempt_read_result():
    from lib.tasks_pkg.compaction._budget import (
        SOURCE_TOOL_RESULT_V2_MAX_TOKENS, enforce_round_aggregate_budget)
    from lib.tasks_pkg.compaction._constants import MAX_ROUND_TOOL_RESULTS_CHARS
    # One read_files result far beyond 2x SOURCE_TOOL_RESULT_V2_MAX_TOKENS
    # chars (2*24000*4 = 192_000 chars), plus a small sibling so the round
    # total exceeds the aggregate budget.
    big = _prose(400_000)
    results = {
        'tc_read': (big, 'read_files', 'tc_read'),
        'tc_grep': ('y' * (MAX_ROUND_TOOL_RESULTS_CHARS + 1),
                    'grep_search', 'tc_grep'),
    }
    updated = enforce_round_aggregate_budget(results, conv_id='tier2a')
    assert updated['tc_read'][0].startswith('[Persisted to:')
    assert len(updated['tc_read'][0]) < len(big)


def test_aggregate_preserves_exempt_read_below_threshold():
    """Below 2x SOURCE allowance the read_files exemption is preserved exactly."""
    from lib.tasks_pkg.compaction._budget import enforce_round_aggregate_budget
    body = _prose(100_000)  # well under 192_000-char exempt threshold
    results = {'tc_read': (body, 'read_files', 'tc_read')}
    updated = enforce_round_aggregate_budget(results, conv_id='tier2a_under')
    assert updated['tc_read'][0] == body


# ── FINDING C — post-tool duplicate snapshot skipped when unchanged ─────────

def test_post_tool_snapshot_skipped_when_unchanged():
    from lib.tasks_pkg.round_evidence import (
        build_round_evidence, begin_round_evidence, snapshot_unchanged_since_request)
    task = {'id': 'snap', '_inspectorTaskId': 'snap', 'convId': 'c',
            '_contextManifest': []}
    messages = [{'role': 'user', 'content': 'q'},
                {'role': 'assistant', 'content': 'a'}]
    ref = begin_round_evidence(task, 'snap', 1, '')
    build_round_evidence(task, messages, ref=ref, kind='request', model='m')
    assert snapshot_unchanged_since_request(task, messages) is True
    # An append or edit must invalidate the watermark.
    messages.append({'role': 'tool', 'tool_call_id': 'x', 'content': 'r'})
    assert snapshot_unchanged_since_request(task, messages) is False


def test_snapshot_unchanged_requires_matching_round():
    from lib.tasks_pkg.round_evidence import (
        build_round_evidence, begin_round_evidence, snapshot_unchanged_since_request)
    task = {'id': 'snap2', '_inspectorTaskId': 'snap2', 'convId': 'c',
            '_contextManifest': []}
    messages = [{'role': 'user', 'content': 'q'}]
    ref = begin_round_evidence(task, 'snap2', 1, '')
    build_round_evidence(task, messages, ref=ref, kind='request', model='m')
    # A different round's ref must not validate.
    task['_inspectorRef'] = {'taskId': 'snap2', 'roundNum': 2, 'turn': 't'}
    assert snapshot_unchanged_since_request(task, messages) is False


# ── FINDING D — child invocations narrow ambient providers ─────────────────

def _child_task():
    from lib.tasks_pkg.invocation import InvocationPolicy
    return {'id': 'child', 'config': {},
            '_invocationPolicy': InvocationPolicy.child('parent-1')}


def test_child_invocation_disables_ambient_providers():
    from lib.tasks_pkg.context_composer import (
        ComposeRequest, _with_context_policy)
    req = ComposeRequest(task=_child_task())
    narrowed = _with_context_policy(req)
    assert 'skills_index' in narrowed.disabled_blocks
    assert 'credential_vault' in narrowed.disabled_blocks
    assert 'user_context' in narrowed.disabled_blocks
    assert 'parallel_execution' in narrowed.disabled_blocks
    assert 'ui_nav_directory' in narrowed.disabled_blocks
    # Tool-relevant and project-rule blocks are preserved.
    assert 'project_rules' not in narrowed.disabled_blocks
    assert 'mcp_tools_delta' not in narrowed.disabled_blocks
    assert 'disclosed_tools' not in narrowed.disabled_blocks
    assert 'environment' not in narrowed.disabled_blocks


def test_root_invocation_keeps_full_provider_set():
    from lib.tasks_pkg.context_composer import (
        ComposeRequest, _with_context_policy)
    req = ComposeRequest(task={'id': 'root', 'config': {}})
    narrowed = _with_context_policy(req)
    assert 'skills_index' not in narrowed.disabled_blocks
    assert 'credential_vault' not in narrowed.disabled_blocks
    assert 'user_context' not in narrowed.disabled_blocks
