"""tests/test_event_emit.py — Characterization tests for the typed emit chokepoint.

Item 2 of the frontend↔backend cleanup unifies the built-in orchestrator's
event emissions through ``lib.agent_core.events.build_event`` / ``emit`` so
there is ONE event model.  The hard requirement is **byte-identical wire
output**: ``build_event(EventType.X, **fields)`` must equal the old
``{'type': 'X', **fields}`` literal exactly — same keys, same order, same JSON.

These tests lock that invariant so the conversion can never silently change the
wire shape a frontend depends on.
"""

from __future__ import annotations

import json
import unittest

from lib.agent_core.events import EventType, build_event, emit


class TestBuildEventByteIdentity(unittest.TestCase):
    def _assert_identical(self, built: dict, literal: dict):
        # Same value, same key ORDER, same serialized bytes.
        self.assertEqual(built, literal)
        self.assertEqual(list(built.keys()), list(literal.keys()))
        self.assertEqual(json.dumps(built, ensure_ascii=False),
                         json.dumps(literal, ensure_ascii=False))

    def test_phase_event(self):
        self._assert_identical(
            build_event(EventType.PHASE, phase='llm_thinking',
                        detail='Generating response…', round=1),
            {'type': 'phase', 'phase': 'llm_thinking',
             'detail': 'Generating response…', 'round': 1})

    def test_phase_with_tool_context(self):
        self._assert_identical(
            build_event(EventType.PHASE, phase='llm_thinking',
                        detail='Analyzing…', toolContext='web_search', round=3),
            {'type': 'phase', 'phase': 'llm_thinking', 'detail': 'Analyzing…',
             'toolContext': 'web_search', 'round': 3})

    def test_messages_snapshot(self):
        self._assert_identical(
            build_event(EventType.MESSAGES_SNAPSHOT, round='fallback',
                        label='Fallback · 3条', messages=[{'role': 'user'}]),
            {'type': 'messages_snapshot', 'round': 'fallback',
             'label': 'Fallback · 3条', 'messages': [{'role': 'user'}]})

    def test_project_external_edit(self):
        self._assert_identical(
            build_event(EventType.PROJECT_EXTERNAL_EDIT,
                        files=['a.py'], sha='deadbeef'),
            {'type': 'project_external_edit', 'files': ['a.py'], 'sha': 'deadbeef'})

    def test_swarm_inbox_inject(self):
        self._assert_identical(
            build_event(EventType.SWARM_INBOX_INJECT, round=2, count=1,
                        agentIds=['a1']),
            {'type': 'swarm_inbox_inject', 'round': 2, 'count': 1,
             'agentIds': ['a1']})

    def test_done_built_incrementally(self):
        # The done event is assembled with conditional keys; build_event(TYPE)
        # then mutate must match the literal-then-mutate sequence exactly.
        built = build_event(EventType.DONE)
        built['finishReason'] = 'stop'
        built['usage'] = {'total_tokens': 10}
        built['model'] = 'claude'
        literal = {'type': 'done'}
        literal['finishReason'] = 'stop'
        literal['usage'] = {'total_tokens': 10}
        literal['model'] = 'claude'
        self._assert_identical(built, literal)

    def test_done_error(self):
        self._assert_identical(
            build_event(EventType.DONE, error={'msg': 'x'}, finishReason='error'),
            {'type': 'done', 'error': {'msg': 'x'}, 'finishReason': 'error'})


class TestEmitDelivery(unittest.TestCase):
    def test_emit_delivers_through_append_event(self):
        """emit() must build the same dict and hand it to append_event.

        (manager.append_event mints event['seq'] in place and returns None —
        emit mirrors that contract exactly; we assert on the delivered event.)
        """
        from lib.tasks_pkg.manager import _chat_runtime
        task = _chat_runtime.create()
        emit(task, EventType.PHASE, phase='working', detail='go', round=1)
        last = task['events'][-1]
        expected = {'type': 'phase', 'phase': 'working', 'detail': 'go', 'round': 1}
        for k, v in expected.items():
            self.assertEqual(last[k], v)
        self.assertEqual(last['type'], 'phase')

    def test_emit_assigns_monotonic_seq_on_events(self):
        from lib.tasks_pkg.manager import _chat_runtime
        task = _chat_runtime.create()
        emit(task, EventType.PHASE, phase='a')
        emit(task, EventType.PHASE, phase='b')
        seqs = [e['seq'] for e in task['events']]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))


class TestConvertedOrchestratorSites(unittest.TestCase):
    """End-to-end: the REAL converted orchestrator helpers emit byte-identical
    dicts to their pre-conversion literals (key order included)."""

    def test_emit_tool_round_phase_round0(self):
        from lib.tasks_pkg import orchestrator as orch
        from lib.tasks_pkg.manager import _chat_runtime
        task = _chat_runtime.create()
        orch._emit_tool_round_phase(task, {'tool_calls': []}, 0)
        got = {k: v for k, v in task['events'][-1].items() if k != 'seq'}
        expected = {'type': 'phase', 'phase': 'llm_thinking',
                    'detail': 'Generating response…', 'round': 1}
        self.assertEqual(json.dumps(got, ensure_ascii=False),
                         json.dumps(expected, ensure_ascii=False))

    def test_emit_tool_round_phase_with_tools(self):
        from lib.tasks_pkg import orchestrator as orch
        from lib.tasks_pkg.manager import _chat_runtime
        task = _chat_runtime.create()
        am = {'tool_calls': [{'function': {'name': 'web_search'}}]}
        orch._emit_tool_round_phase(task, am, 2)
        got = {k: v for k, v in task['events'][-1].items() if k != 'seq'}
        self.assertEqual(list(got.keys()),
                         ['type', 'phase', 'detail', 'toolContext', 'round'])
        self.assertEqual(got['type'], 'phase')
        self.assertEqual(got['round'], 3)


class TestUnregisteredTypeAllowed(unittest.TestCase):
    def test_unregistered_type_still_builds(self):
        # Forward-compat: unknown types are not rejected at runtime.
        e = build_event('some_future_event', x=1)
        self.assertEqual(e, {'type': 'some_future_event', 'x': 1})


if __name__ == '__main__':
    unittest.main(verbosity=2)
