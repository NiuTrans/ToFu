"""Tests for role-scoped nudge text in the tool loop breaker.

Incident anchor (conv mvdapxexnid4hg, agent-general-751ce80f): a read-only
swarm agent (researcher/analyst/reviewer) stalled on repeated read_files calls.
The breaker's nudge told it to "issue the real next action (edit_file,
write_file, run_command)" — but those tools are NOT in the agent's scoped
tool list. The model could not obey the correction, so it kept re-reading
and the harness force-stopped it. The nudge must only name tools the agent
can actually call, and must offer "finish and report findings" as a
first-class exit when no action tools are in scope.
"""

import threading

import pytest

from lib.tasks_pkg.orchestrator._tool_loop_breaker import (
    _nudge_text,
    _visible_action_tools,
)

pytestmark = pytest.mark.unit


def _task_with_tools(names):
    """Build a minimal task whose tool catalog carries exactly *names*."""
    return {
        'id': 'abcdef1234567890', 'convId': 'convtest',
        'toolRounds': [], 'events': [], 'events_lock': threading.Lock(),
        '_invocationTools': [
            {'type': 'function', 'function': {'name': name}}
            for name in names
        ],
    }


class TestVisibleActionTools:
    def test_full_catalog_offers_all_actions(self):
        task = _task_with_tools(
            ['read_files', 'edit_file', 'write_file', 'run_command'])
        assert _visible_action_tools(task) == [
            'edit_file', 'write_file', 'run_command']

    def test_read_only_role_scopes_out_writes(self):
        """researcher/analyst/reviewer/critic have no write or shell tools."""
        task = _task_with_tools(
            ['read_files', 'grep_search', 'find_files', 'web_search'])
        assert _visible_action_tools(task) == []

    def test_browser_role_scopes_out_writes_and_shell(self):
        task = _task_with_tools(
            ['browser_read_page', 'browser_click', 'fetch_url'])
        assert _visible_action_tools(task) == []

    def test_empty_catalog_falls_back_to_tool_schema(self):
        """A root-loop task without _invocationTools still resolves."""
        task = {
            'id': 'abcdef1234567890', 'convId': 'convtest',
            'toolRounds': [], 'events': [], 'events_lock': threading.Lock(),
            '_tool_schema': [
                {'type': 'function', 'function': {'name': 'edit_file'}},
            ],
        }
        assert _visible_action_tools(task) == ['edit_file']


class TestNudgeTextForReadOnlyAgent:
    def test_redundant_read_nudge_offers_finish_not_writes(self):
        """The nudge must NOT name edit_file/write_file to a read-only agent."""
        task = _task_with_tools(['read_files', 'grep_search'])
        text = _nudge_text(task, 'redundant_reads')
        assert 'edit_file' not in text
        assert 'write_file' not in text
        assert 'run_command' not in text
        assert 'finish now and report your findings' in text

    def test_exact_repetition_nudge_offers_finish(self):
        task = _task_with_tools(['read_files', 'grep_search'])
        text = _nudge_text(task, 'exact_repetition')
        assert 'finish now and report your findings' in text
        assert 'edit_file' not in text

    def test_noop_nudge_offers_finish(self):
        task = _task_with_tools(['read_files', 'grep_search'])
        text = _nudge_text(task, 'explicit_noop')
        assert 'finish now and report your findings' in text
        assert 'edit_file' not in text


class TestNudgeTextForCoderAgent:
    def test_redundant_read_nudge_offers_writes(self):
        """A coder-role agent CAN write — the nudge names its real tools."""
        task = _task_with_tools(
            ['read_files', 'write_file', 'edit_file', 'run_command'])
        text = _nudge_text(task, 'redundant_reads')
        assert 'edit_file' in text
        assert 'write_file' in text
        assert 'run_command' in text
        assert 'finish now and report your findings' in text

    def test_general_role_offers_all_tools(self):
        task = _task_with_tools(
            ['read_files', 'write_file', 'edit_file', 'run_command',
             'web_search', 'grep_search'])
        text = _nudge_text(task, 'redundant_reads')
        assert 'edit_file' in text
        assert 'write_file' in text
        assert 'run_command' in text
