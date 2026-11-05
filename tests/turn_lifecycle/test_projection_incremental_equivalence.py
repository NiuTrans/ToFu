"""Differential spec: incremental settled-row projection equals the oracle.

A live task is driven through the production settle points (round close,
priced API rounds, closed status intervals, settled segment mirrors, resume
hydration). Each event is captured and projected by the current pipeline and,
from a thawed plain copy of the same task, by the vendored pre-change oracle
(``_projection_oracle``). Projection documents and patches must be
byte-identical. Retained-byte charges follow the documented formula: settled
rows are shared and each is charged its standalone size once per graph, so a
graph without settled history is charged exactly as before.
"""
import collections
import copy
import json
import random
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest

import lib.conversation_sync.settled_rows as settled_rows
import lib.pricing._peak as peak_module
import lib.storage.storage_projection as storage_projection
import lib.turn_lifecycle as turn_lifecycle
from lib.conversation_sync.settled_rows import (
    SettledDict,
    SettledList,
    SettledRowMutation,
    detach_sharing_settled,
    is_settled,
    settle_items,
    settle_or_none,
)
from lib.pricing import set_provider_pricing
from lib.swarm.snapshot import stamp_round
from lib.tasks_pkg.manager._persist import _merge_tool_rounds
from lib.tasks_pkg.orchestrator._round_checkpoint import settle_finished_tool_rounds
from lib.tasks_pkg.round_costs import stamp_round_costs
from lib.tasks_pkg.segments import assemble_segments
from lib.tasks_pkg.turn_trace import observe_task_trace_event
from lib.tools.result_value import CanonicalReceipt, TrustedResultText
from lib.turn_lifecycle import _task_projection, capture_task_projection_snapshot
from lib.turn_projection_patch import build_projection_patch
from lib.turn_projection_segments import projection_with_stable_segments
from lib.utils import fast_deepcopy, retained_graph_bytes
from tests.turn_lifecycle import _projection_oracle as oracle

pytestmark = pytest.mark.unit

_ATTEMPT = 'attempt-equivalence'
_TASK = 'task-equivalence'
_PROVIDER = 'equivalence-provider'
_LIVE_ONLY_KEYS = frozenset({'content_lock', '_toolRuntimeLock', '_activeToolContexts'})
_PROGRESS = frozenset({'delta', 'program_output', 'tool_progress'})


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def _thaw(task):
    """The pre-change producers' plain task: same facts, nothing settled."""
    thawed = {key: copy.deepcopy(value) for key, value in task.items()
              if key not in _LIVE_ONLY_KEYS}
    thawed['content_lock'] = threading.Lock()
    return thawed


def _structure(source):
    return {key: value for key, value in source.items()
            if key not in {'content', 'thinking', 'phase'}}


def _standalone_bytes(row):
    pending, seen, total = [row], set(), 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return total


def _documented_charge(*values):
    """Owner allowance + unsettled objects once + each settled row standalone once."""
    pending, seen, total = list(values), set(), 1024
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if type(item) in (SettledDict, SettledList):
            total += _standalone_bytes(item)
            continue
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return total


_ROW_LISTS = ('toolRounds', 'segments', 'apiRounds', '_timingStatusHistory',
              '_checkpointToolRounds')


def _reference_capture(task, event):
    """Structural capture before row-list memos: every row derived and copied."""
    from lib.tasks_pkg.manager._events import snapshot_task_text
    keys = {
        'id', 'taskId', '_attemptId', 'attemptId', '_turnActor', '_turnStatus',
        'status', '_turnVisibleRunTurnIds', '_checkpointToolRounds', '_mcpImages',
        'toolRounds', 'apiRounds', 'error', '_activeModelRequestSpan',
        '_timingStatusHistory', '_timingStatusDroppedCount',
    }
    keys.update(key for key, _ in turn_lifecycle._PROJECTION_TASK_FIELDS)
    keys.update(key for key, _, _ in turn_lifecycle._PROJECTION_INJECTION_LANES)
    keys.update(key for key, _ in turn_lifecycle._PROJECTION_PROVENANCE_FIELDS)
    values = {key: task[key] for key in keys if key in task}
    if 'apiRounds' in values:
        values['apiRounds'] = storage_projection.sanitize_api_rounds_for_persist(
            values['apiRounds'])
    if 'toolRounds' in values:
        values['toolRounds'] = storage_projection.compact_tool_rounds_for_frame_budget([
            storage_projection.trim_tool_round_copy(row) for row in values['toolRounds']])
    if isinstance(values.get('segments'), list):
        values['segments'] = [turn_lifecycle._segment_without_round_mirror(segment)
                              for segment in values['segments']]
    if task.get('_todoState'):
        from lib.tools.todo import public_todo_state
        values['todoState'] = public_todo_state(task['_todoState'])
    values['config'] = {'checkpointToolRounds': [] if task.get('_checkpointToolRounds')
        else (task.get('config') or {}).get('checkpointToolRounds', [])}
    source, structure_bytes = detach_sharing_settled(values)
    source['content'], source['thinking'], _ = snapshot_task_text(task)
    source['phase'] = fast_deepcopy(task.get('phase'))
    return source, structure_bytes


def _canonical(value):
    # Captured key order follows a compiled set constant, not a contract.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _assert_capture_matches_reference(source, charge, reference, reference_charge):
    """Same document, same charge and the same rows shared by reference."""
    assert charge == reference_charge
    assert _canonical(source) == _canonical(reference)
    for field in _ROW_LISTS:
        rows, expected = source.get(field), reference.get(field)
        if not isinstance(expected, list):
            continue
        assert sys.getsizeof(rows) == sys.getsizeof(expected)
        for row, expected_row in zip(rows, expected):
            if is_settled(expected_row):
                assert row is expected_row
            elif type(expected_row) in (dict, list):
                assert row is not expected_row


def _contains_settled(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) in (SettledDict, SettledList):
            return True
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return False


class _World:
    """One live task, projected by the current pipeline and by the oracle."""

    def __init__(self, rng, *, model='glm-5.3', provider=_PROVIDER):
        self.rng = rng
        self.model = model
        self.task = {
            'id': _TASK, '_attemptId': _ATTEMPT, 'status': 'running',
            '_turnActor': 'assistant', 'content': '', 'thinking': '',
            'content_lock': threading.Lock(), 'phase': None, 'model': model,
            'provider_id': provider, 'toolRounds': [], 'apiRounds': [], 'config': {},
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0},
            '_route_snapshot': {'model': model, 'providerId': provider, 'routeId': 'r1'},
        }
        self.state_api_rounds = []
        self.events = 0
        self.clock = 1_790_000_000_000
        self.revision = 0
        self.new_previous, self.old_previous = {}, {}
        self.new_source = self.old_source = None
        self.new_bytes = self.old_bytes = 0
        self.old_llm_round = 0
        self.settled_charges_checked = 0

    def emit(self, event):
        self.events += 1
        self.clock += self.rng.randint(5, 900)
        event = {'seq': self.events, 'emittedAt': self.clock, **event}
        observe_task_trace_event(self.task, event, observed_at_ms=self.clock)
        progress = event['type'] in _PROGRESS
        new_source, new_bytes = capture_task_projection_snapshot(
            self.task, event,
            previous_source=self.new_source if progress else None,
            previous_bytes=self.new_bytes)
        old_source, old_bytes = oracle.capture_task_projection_snapshot(
            _thaw(self.task), event,
            previous_source=self.old_source if progress else None,
            previous_bytes=self.old_bytes)
        if not progress:
            structure = _structure(new_source)
            assert new_bytes == _documented_charge(structure)
            if _contains_settled(structure):
                self.settled_charges_checked += 1
            _assert_capture_matches_reference(
                new_source, new_bytes, *_reference_capture(self.task, event))
        self.new_source, self.new_bytes = new_source, new_bytes
        self.old_source, self.old_bytes = old_source, old_bytes

        new_baseline = _encoded(self.new_previous)
        new_source['_activityLastLlmRound'] = self.task.get('_activityLastLlmRound', 0)
        new_projection = _task_projection(new_source, self.new_previous, event)
        self.task['_activityLastLlmRound'] = new_source['_activityLastLlmRound']
        old_source['_activityLastLlmRound'] = self.old_llm_round
        old_projection = oracle._task_projection(old_source, self.old_previous, event)
        self.old_llm_round = old_source['_activityLastLlmRound']
        assert _encoded(new_projection) == _encoded(old_projection), event
        assert new_projection == old_projection

        new_patch = build_projection_patch(
            self.new_previous, new_projection,
            base_revision=self.revision, target_revision=self.revision + 1)
        old_patch = oracle.build_projection_patch(
            self.old_previous, old_projection,
            base_revision=self.revision, target_revision=self.revision + 1)
        assert _encoded(new_patch) == _encoded(old_patch), event
        # The committed baseline is never edited by the next projection.
        assert _encoded(self.new_previous) == new_baseline
        self.revision += 1
        self.new_previous, self.old_previous = new_projection, old_projection
        return new_projection

    def settle_terminal(self, status):
        new = projection_with_stable_segments(
            self.new_previous, actor='assistant', status=status)
        old = oracle.projection_with_stable_segments(
            self.old_previous, actor='assistant', status=status)
        assert _encoded(new) == _encoded(old)
        new_patch = build_projection_patch(
            self.new_previous, new, base_revision=self.revision,
            target_revision=self.revision + 1)
        old_patch = oracle.build_projection_patch(
            self.old_previous, old, base_revision=self.revision,
            target_revision=self.revision + 1)
        assert _encoded(new_patch) == _encoded(old_patch)

    # ── producer helpers (the real settle points) ─────────────────────
    def text(self, length):
        words = ('alpha', 'beta', 'gamma', 'delta', 'lambda', 'def', 'class',
                 'return', 'é', '中文', '\n', 'value')
        return ' '.join(self.rng.choice(words) for _ in range(length))

    def start_tool(self, round_num, llm_round, *, name=None, status='running'):
        name = name or self.rng.choice(
            ['read_files', 'run_command', 'search_files', 'edit_file', 'write_file'])
        row = {
            'roundNum': round_num, 'llmRound': llm_round,
            'toolCallId': f'call_{round_num:04d}', 'toolName': name,
            'toolArgs': json.dumps({'path': f'lib/m{round_num}.py'}),
            'query': f'{name} lib/m{round_num}.py', 'status': status,
            'source': 'native_direct', 'tStart': self.clock, 'results': None,
            'attemptId': _ATTEMPT, 'taskId': _TASK,
        }
        if self.rng.random() < 0.4:
            row['assistantContent'] = self.text(12)
            row['thinking'] = self.text(20)
        self.task['toolRounds'].append(row)
        self.emit({'type': 'tool_start', 'roundNum': round_num,
                   'toolCallId': row['toolCallId'], 'toolName': name})
        return row

    def progress(self, row):
        row['_partialOutput'] = self.text(30)
        row['_partialOutputTotalChars'] = len(row['_partialOutput'])
        self.emit({'type': 'tool_progress', 'roundNum': row['roundNum'],
                   'toolCallId': row['toolCallId'], 'toolName': row['toolName']})

    def finish_tool(self, row, *, status='done', content=None):
        output = content if content is not None else self.text(self.rng.randint(20, 400))
        row['results'] = [{
            'toolName': row['toolName'], 'title': f"{row['toolName']} ok",
            'snippet': str(output)[:120], 'source': 'Project', 'fetched': True,
            'fetchedChars': len(output), 'output': output,
            'meta': {'lines': self.rng.randint(1, 900), 'tags': ['x', 'y']},
        }]
        row['toolContent'] = output
        row['status'] = status
        row['tEnd'] = self.clock + 400
        self.emit({'type': 'tool_result', 'roundNum': row['roundNum'],
                   'toolCallId': row['toolCallId'], 'toolName': row['toolName'],
                   'status': status, 'tStart': row['tStart'], 'tEnd': row['tEnd']})

    def bill_round(self, index, *, model=None, provider=_PROVIDER):
        self.state_api_rounds.append({
            'round': index, 'model': model or self.model, 'provider_id': provider,
            'tag': 'main', 'usage': {
                'prompt_tokens': 20_000 + index * 311, 'completion_tokens': 200 + index,
                'prompt_tokens_details': {'cached_tokens': 15_000 + index * 290},
            },
        })
        stamp_round_costs(self.task, self.state_api_rounds, model=self.model)
        self.task['apiRounds'] = (list(self.task.get('_checkpointApiRounds') or [])
                                  + list(self.state_api_rounds))
        usage = self.task['usage']
        usage['prompt_tokens'] += 20_000
        usage['completion_tokens'] += 200
        self.task['_lastRoundUsage'] = dict(self.state_api_rounds[-1]['usage'])

    def close_round(self, llm_round, *, checkpoint):
        settle_finished_tool_rounds(self.task)
        if checkpoint:
            self.task['segments'] = settle_items(
                assemble_segments(self.task, merged=_merge_tool_rounds(self.task)))
        self.emit({'type': 'round_end', 'roundNum': llm_round, 'reason': 'tools'})

    def phase(self, phase, **fields):
        self.emit({'type': 'phase', 'phase': phase, 'detail': f'{phase} beat', **fields})


def _drive(world, *, llm_rounds, tools_per_round, features=()):
    rng = world.rng
    round_num = len(world.task['toolRounds'])
    for llm_round in range(llm_rounds):
        world.phase('waiting_model', roundNum=llm_round)
        if 'retry' in features and rng.random() < 0.2:
            world.phase('retrying', roundNum=llm_round, attempt=1, backoff_s=1.5,
                        statusCode=503)
        if rng.random() < 0.5:
            world.task['content'] = world.text(25)
            world.emit({'type': 'delta', 'content': 'x'})
            world.task['content'] = ''
            world.emit({'type': 'delta_reset'})
        rows = []
        for _ in range(rng.randint(1, tools_per_round)):
            round_num += 1
            name = None
            if 'swarm' in features and rng.random() < 0.08:
                name = 'spawn_agents'
            if 'tool-phases' in features:
                world.phase('tool_exec', roundNum=round_num)
            rows.append(world.start_tool(round_num, llm_round, name=name))
        if not (world.task['content'] or world.task['thinking']):
            # The post-parse mirror refresh of the live executor.
            world.task['segments'] = settle_items(assemble_segments(world.task))
        for row in rows:
            if rng.random() < 0.3:
                world.progress(row)
            if 'human' in features and row is rows[-1] and rng.random() < 0.15:
                row['status'] = 'awaiting_human'
                world.emit({'type': 'phase', 'phase': 'awaiting_human'})
            status = rng.choice(['done'] * 8 + ['error', 'rejected'])
            content = None
            if 'large' in features and rng.random() < 0.3:
                content = world.text(1500)
            if 'custom' in features and rng.random() < 0.2:
                content = TrustedResultText(world.text(30), canonical=None)
            world.finish_tool(row, status=status, content=content)
            if 'custom' in features and rng.random() < 0.1:
                row['results'].append(CanonicalReceipt({'resultId': 'r'}, canonical=None))
            if row['toolName'] == 'spawn_agents':
                row['toolContent'] = json.dumps({'swarm_run_id': 'run-1', 'agents': [
                    {'id': 'agent-1', 'role': 'reviewer', 'objective': 'check'}]})
        if 'peak' in features:
            moment = _utc(2026, 9, 11, 7 if llm_round % 2 else 12, 0)
            peak_module._utc_now = (lambda moment=moment: moment)
            world.bill_round(llm_round + 1, model='deepseek-flash', provider=None)
        world.bill_round(llm_round + 1)
        if 'pricing' in features and llm_round == llm_rounds // 2:
            set_provider_pricing(_PROVIDER, world.model, {'input': 9.0, 'output': 30.0})
        if 'swarm' in features:
            for row in world.task['toolRounds']:
                if not is_settled(row) and row.get('toolName') == 'spawn_agents':
                    stamp_round(row, {'swarm_run_id': 'run-1', 'version': llm_round,
                                      'agents': [{'id': 'agent-1', 'status': 'running'}]})
        if 'files' in features and rng.random() < 0.4:
            files = list(world.task.get('modifiedFileList') or [])
            files.append({'path': f'lib/m{round_num}.py', 'kind': 'edit',
                          'commandId': f'cmd-{round_num}'})
            world.task['modifiedFileList'] = files
            world.task['modifiedFiles'] = len(files)
        if 'compaction' in features and rng.random() < 0.15:
            world.phase('compacting', roundNum=llm_round)
            archive = f'archive-{llm_round}'
            world.emit({'type': 'compaction', 'archiveId': archive, 'roundNum': llm_round,
                        'tokensBefore': 90_000})
            world.emit({'type': 'compaction_done', 'archiveId': archive, 'roundNum': llm_round,
                        'tokensBefore': 90_000, 'tokensAfter': 30_000,
                        'receipt': {'status': 'completed', 'summary': {'durationMs': 1200}}})
        if 'fallback' in features and rng.random() < 0.1:
            world.emit({'type': 'model_fallback', 'fallbackFrom': world.model,
                        'fallbackModel': 'backup-model', 'fallbackReason': 'quota'})
        world.close_round(llm_round, checkpoint=rng.random() < 0.5)


@pytest.fixture
def pricing(monkeypatch):
    from lib.pricing import PROVIDER_PRICING
    monkeypatch.setitem(PROVIDER_PRICING, _PROVIDER, {
        'glm-5.3': {'input': 0.6, 'output': 2.2, 'cacheWriteMul': 1.0, 'cacheReadMul': 0.2},
    })
    monkeypatch.setattr(peak_module, '_utc_now', peak_module._utc_now)
    yield


@pytest.mark.parametrize('seed', [3, 11, 29])
def test_randomized_turns_project_and_patch_identically(pricing, seed):
    world = _World(random.Random(seed))
    _drive(world, llm_rounds=14, tools_per_round=4, features=(
        'retry', 'custom', 'human', 'swarm', 'files', 'compaction', 'fallback',
        'pricing', 'peak'))
    world.emit({'type': 'done', 'finishReason': 'stop'})
    world.settle_terminal('completed')
    assert world.settled_charges_checked > 0
    settled_rows = [row for row in world.task['toolRounds'] if is_settled(row)]
    assert settled_rows, 'round close must settle finished history'
    assert all(row['toolName'] != 'spawn_agents' for row in settled_rows)


def test_long_turn_crosses_every_bounded_lane_identically(pricing, monkeypatch):
    # More status intervals, timeline rows and tool rounds than every lane
    # retains, plus a frame budget small enough to elide settled payloads.
    # Both pipelines read the same lowered bounds, so the paths are the
    # production ones at a size a unit test can afford.
    import lib.tasks_pkg.turn_trace as turn_trace
    import lib.turn_activity_timeline as timeline
    import lib.turn_trace_contract as trace_contract
    for module in (timeline, oracle):
        monkeypatch.setattr(module, 'ACTIVITY_TIMELINE_MAX_ENTRIES', 24)
    for module in (turn_trace, trace_contract, oracle):
        monkeypatch.setattr(module, 'TRACE_MAX_STATUS_ENTRIES', 24)
    monkeypatch.setattr(storage_projection, 'TOOL_ROUNDS_FRAME_BUDGET_BYTES', 60_000)
    world = _World(random.Random(5))
    _drive(world, llm_rounds=16, tools_per_round=6,
           features=('retry', 'compaction', 'files', 'tool-phases', 'large'))
    projection = world.new_previous
    assert projection['activityTimeline']['droppedCount'] > 0
    assert projection['timingTrace']['statusDroppedCount'] > 0
    assert any(row.get('_persistCompacted') for row in projection['toolRounds'])
    world.emit({'type': 'error', 'error': {'kind': 'internal', 'detail': 'boom'}})
    world.settle_terminal('failed')


@pytest.mark.parametrize('terminal', [
    ({'type': 'done', 'finishReason': 'stop'}, 'completed'),
    ({'type': 'aborted', 'finishReason': 'aborted'}, 'interrupted'),
])
def test_resumed_turn_shares_retained_history_identically(pricing, terminal):
    rng = random.Random(17)
    retained = []
    for index in range(1, 30):
        retained.append({
            'roundNum': index, 'llmRound': index // 3, 'toolCallId': f'old_{index}',
            'toolName': 'read_files', 'toolArgs': '{}', 'status': 'done',
            'results': [{'title': 'old', 'output': 'o' * index}],
            'toolContent': 'o' * index, 'attemptId': 'attempt-old', 'taskId': 'task-old',
        })
    world = _World(rng)
    task = world.task
    # Hydration (orchestrator/_resume_state): retained rounds settle.
    task['config'] = {'checkpointToolRounds': copy.deepcopy(retained)}
    task['_checkpointToolRounds'] = [settle_or_none(row) or dict(row) for row in retained]
    retained_api = [settle_or_none({
        'round': index, 'model': 'glm-5.3', 'provider_id': _PROVIDER,
        'usage': {'prompt_tokens': 1000 * index, 'completion_tokens': 10},
        'cost': {'costUsd': 0.01 * index}}) for index in range(1, 6)]
    task['_checkpointApiRounds'] = retained_api
    task['apiRounds'] = list(retained_api)
    _drive(world, llm_rounds=6, tools_per_round=3)
    event, status = terminal
    world.emit(event)
    world.settle_terminal(status)
    source, _ = capture_task_projection_snapshot(task, {'type': 'round_end'})
    # The shadowed config copy of retained history is never detached again.
    assert source['config']['checkpointToolRounds'] == []
    assert all(row is task['_checkpointToolRounds'][index]
               for index, row in enumerate(source['_checkpointToolRounds']))


def test_charge_without_settled_history_matches_the_oracle_exactly(pricing):
    world = _World(random.Random(23))
    for index in range(1, 6):
        row = world.start_tool(index, index, name='read_files')
        world.finish_tool(row)
    world.bill_round(1)
    # No round close, no priced settlement: only unsettled structure.
    world.state_api_rounds[:] = [copy.deepcopy(row) for row in world.state_api_rounds]
    world.task['apiRounds'] = list(world.state_api_rounds)
    world.task['_lastRoundUsage'] = copy.deepcopy(world.task['_lastRoundUsage'])
    world.task['_timingStatusHistory'] = []
    for event in ({'type': 'phase', 'phase': 'tool_exec'},
                  {'type': 'tool_result', 'roundNum': 5}):
        world.emit(event)
        assert world.new_bytes == world.old_bytes


def test_terminal_sweep_keeps_settled_verdicts_untouched(pricing):
    from lib.tasks_pkg.orchestrator._finalize import _finalize_dangling_tool_rounds
    world = _World(random.Random(41))
    row = world.start_tool(1, 0, name='ask_human')
    world.finish_tool(row, status='unanswerable')
    world.close_round(0, checkpoint=False)
    settled = world.task['toolRounds'][0]
    assert is_settled(settled)
    oracle_task = _thaw(world.task)
    assert _finalize_dangling_tool_rounds(oracle_task) == 0
    assert _finalize_dangling_tool_rounds(world.task) == 0
    assert world.task['toolRounds'][0] is settled
    assert _encoded(world.task['toolRounds']) == _encoded(oracle_task['toolRounds'])
    assert settled['status'] == 'unanswerable'


def test_settled_history_refuses_late_in_place_writes(pricing):
    world = _World(random.Random(31))
    _drive(world, llm_rounds=3, tools_per_round=2)
    row = next(row for row in world.task['toolRounds'] if is_settled(row))
    for write in (lambda: row.__setitem__('status', 'error'),
                  lambda: row['results'][0].__setitem__('output', 'late'),
                  lambda: row['results'].append({})):
        with pytest.raises(SettledRowMutation):
            write()
    api_row = next(row for row in world.task['apiRounds'] if is_settled(row))
    with pytest.raises(SettledRowMutation):
        api_row['usage']['prompt_tokens'] = 0
    entry = next(entry for entry in world.task['_timingStatusHistory'] if is_settled(entry))
    with pytest.raises(SettledRowMutation):
        entry['tEnd'] = 0


def test_rows_that_can_still_be_written_never_settle(pricing):
    from lib.tasks_pkg.tool_runtime import (
        close_tool_result_publication, context_for_task, unregister_context,
    )
    world = _World(random.Random(37))
    world.task['_userId'] = 7
    finished = world.start_tool(1, 0)
    world.finish_tool(finished)
    # The collector publishes a timeout verdict while the worker still runs.
    late = world.start_tool(2, 0)
    context = context_for_task(
        world.task, round_num=2, tool_call_id=late['toolCallId'],
        tool_name=late['toolName'], round_entry=late)
    close_tool_result_publication(
        world.task, round_num=2, tool_call_id=late['toolCallId'],
        round_entry=late, terminal_status='error')
    # A command handed off to a background owner keeps its callbacks.
    handed_off = world.start_tool(3, 0, name='run_command')
    handed_off['backgroundCommandId'] = 'bg_0123456789ab'
    world.finish_tool(handed_off)
    world.close_round(0, checkpoint=False)
    assert is_settled(world.task['toolRounds'][0])
    assert world.task['toolRounds'][1] is late
    # The worker exits, then stamps its declared timeout after unregistering.
    unregister_context(context)
    late['_declaredTimeoutMs'] = 1000
    world.close_round(1, checkpoint=False)
    assert world.task['toolRounds'][1] is late and not is_settled(late)
    assert world.task['toolRounds'][2] is handed_off and not is_settled(handed_off)
    handed_off['status'] = 'awaiting_stdin'
    world.emit({'type': 'phase', 'phase': 'tool_exec'})
    assert world.new_previous['toolRounds'][1]['_declaredTimeoutMs'] == 1000
    assert world.new_previous['toolRounds'][2]['status'] == 'awaiting_stdin'


def test_structural_capture_derives_only_live_and_new_rows(pricing, monkeypatch):
    world = _World(random.Random(43))
    _drive(world, llm_rounds=6, tools_per_round=3, features=('files', 'retry'))
    task = world.task
    calls = []

    def counted(name, original):
        return lambda value: calls.append((name, value)) or original(value)

    for name in ('trim_tool_round_copy', '_segment_without_round_mirror'):
        monkeypatch.setattr(turn_lifecycle, name, counted(name, getattr(turn_lifecycle, name)))
    monkeypatch.setattr(settled_rows, 'settled_retained_bytes', counted(
        'settled_retained_bytes', settled_rows.settled_retained_bytes))
    history = set()

    def capture(event):
        calls.clear()
        source, charge = capture_task_projection_snapshot(task, event)
        made = list(calls)
        _assert_capture_matches_reference(source, charge, *_reference_capture(task, event))
        # Rows the previous capture already shared are neither derived nor
        # charged again.
        assert not any(id(value) in history for _, value in made)
        history.clear()
        history.update(id(row) for field in _ROW_LISTS for row in source.get(field) or ()
                       if is_settled(row))
        history.update(id(row) for field in _ROW_LISTS for row in task.get(field) or ()
                       if is_settled(row))
        return collections.Counter(name for name, _ in made)

    def live(rows):
        return sum(not is_settled(row) for row in rows)

    assert any(is_settled(row) for row in task['toolRounds'])
    capture({'type': 'round_end', 'roundNum': 6})
    counts = capture({'type': 'phase', 'phase': 'waiting_model'})
    assert counts['trim_tool_round_copy'] == live(task['toolRounds'])
    assert counts['_segment_without_round_mirror'] == live(task['segments'])
    # A new running row is derived; settled history is not visited again.
    row = world.start_tool(len(task['toolRounds']) + 1, 7)
    world.finish_tool(row)
    counts = capture({'type': 'tool_result', 'roundNum': row['roundNum']})
    assert counts['trim_tool_round_copy'] == live(task['toolRounds']) >= 1
    # Round close replaces live rows by settled twins in place: only those
    # positions are derived once more.
    live_before = live(task['toolRounds'])
    assert settle_finished_tool_rounds(task) >= 1
    counts = capture({'type': 'round_end', 'roundNum': 7})
    assert counts['trim_tool_round_copy'] == live_before
    counts = capture({'type': 'phase', 'phase': 'tool_exec'})
    assert counts['trim_tool_round_copy'] == live(task['toolRounds'])


_CONTRACT_FIELDS = {
    'delta_reset': {'roundNum': 0, 'contentEpoch': 0},
    'compaction': {'convId': 'lane-equivalence-conv', 'trigger': 'auto', 'tokensAfter': 30_000,
                   'tokenCountKind': 'exact', 'msgsBefore': 40, 'msgsAfter': 12,
                   'model': 'glm-5.3', 'reason': 'context', 'snapshotKind': 'summary',
                   'ts': 1_790_000_000},
    'compaction_done': {'convId': 'lane-equivalence-conv', 'trigger': 'auto',
                        'tokenCountKind': 'exact', 'msgsBefore': 40, 'msgsAfter': 12,
                        'reductionPct': 66.7},
    'model_fallback': {'fallbackKind': 'quota'},
}


class _LaneWorld(_World):
    """The same producers, emitting through the production append path."""

    def __init__(self, rng, task, measured):
        super().__init__(rng)
        fields = dict(self.task)
        fields.pop('id')
        task.update(fields, convId='lane-equivalence-conv', _turnId='lane-equivalence-turn',
                    _workerStartedAt=time.time())
        self.task = task
        self.measured = measured
        self.accepted = []

    def emit(self, event):
        from lib.tasks_pkg.manager import _events
        self.events += 1
        self.clock += self.rng.randint(5, 900)
        event = {**_CONTRACT_FIELDS.get(event['type'], {}), **event}
        if event['type'] in ('tool_start', 'tool_result'):
            # Delivered tool frames carry the row's query and live results.
            row = next(row for row in self.task['toolRounds']
                       if row.get('toolCallId') == event['toolCallId'])
            event['query'] = row['query']
            if event['type'] == 'tool_result':
                event['results'] = row['results']
        before = len(self.accepted)
        self.measured.clear()
        _events.append_event(self.task, event)
        measured = dict(self.measured)
        for wire, retained_bytes, options, reference in self.accepted[before:]:
            _assert_one_walk_and_exact_charges(
                wire, retained_bytes, options, reference, measured)
        return None


def _assert_one_walk_and_exact_charges(wire, retained_bytes, options, reference, measured):
    source = options.get('projection_source')
    side = () if source is None else (source['content'], source['thinking'], source.get('phase'))
    wire_bytes = _documented_charge(wire)
    assert retained_bytes == _documented_charge(wire, *side) + wire_bytes
    if not _contains_settled(wire):
        # The charge before the fused copy: the frame walked again beside its values.
        assert retained_bytes == retained_graph_bytes(wire, *side) + retained_graph_bytes(wire)
    if options.get('progress_frame') is not None:
        assert options['progress_wire_bytes'] == 2 * wire_bytes
    # One walk: every container the frame and its owned phase hold was measured once.
    containers = _containers(wire, *side)
    assert containers and all(measured.get(id(item)) == 1 for item in containers)
    if reference is not None:
        reference_source, reference_bytes = reference
        assert options['projection_bytes'] == reference_bytes
        assert _canonical(_structure(source)) == _canonical(_structure(reference_source))


def _containers(*values):
    pending, seen, found = list(values), set(), []
    while pending:
        item = pending.pop()
        if id(item) in seen or is_settled(item):
            continue
        seen.add(id(item))
        if isinstance(item, dict):
            found.append(item)
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            found.append(item)
            pending.extend(item)
    return found


@pytest.mark.parametrize('seed', [7, 19])
def test_append_path_copies_and_charges_each_frame_in_one_walk(pricing, monkeypatch, seed):
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.event_delivery import EventDeliveryCoordinator
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    owner = EventDeliveryCoordinator(workers=1, retry_delay=0, idle_timeout=.02)
    monkeypatch.setattr(event_delivery, '_COORDINATOR', owner)
    monkeypatch.setattr(turn_lifecycle, 'record_task_event', lambda task, wire, **kwargs: 'carried')
    monkeypatch.setattr(turn_lifecycle, 'record_task_events',
                        lambda task, entries, **kwargs: ['carried'] * len(entries))
    monkeypatch.setattr('lib.translate.terminal.schedule_terminal_turn_translations',
                        lambda task: None)
    monkeypatch.setattr(event_delivery, 'retained_snapshot_bytes', None)
    measured = collections.Counter()
    getsizeof = sys.getsizeof

    def counting_getsizeof(value, *default):
        measured[id(value)] += 1
        return getsizeof(value, *default)

    task = chat_task_runtime.create(user_id=1, task_id='lane-equivalence-' + uuid.uuid4().hex)
    world = _LaneWorld(random.Random(seed), task, measured)
    original_submit = owner.submit
    original_capture = turn_lifecycle.capture_task_projection_snapshot
    references = {}

    def capture(task_, event, **kwargs):
        source, charge = original_capture(task_, event, **kwargs)
        if not (turn_lifecycle.task_event_projection_is_progress(event)
                and kwargs.get('previous_source') is not None):
            # Text sizes grow once the frame is serialized: compare at capture time.
            with monkeypatch.context() as unmeasured:
                unmeasured.setattr(sys, 'getsizeof', getsizeof)
                references[id(source)] = _reference_capture(task_, event)
        return source, charge

    def submit(lane, sequence, retained_bytes, persist, publish, **options):
        wire = task['events'][-1]
        assert wire['seq'] == sequence
        reference = references.pop(id(options.get('projection_source')), None)
        world.accepted.append((wire, retained_bytes, options, reference))
        return original_submit(lane, sequence, retained_bytes, persist, publish, **options)

    monkeypatch.setattr(turn_lifecycle, 'capture_task_projection_snapshot', capture)
    monkeypatch.setattr(owner, 'submit', submit)
    monkeypatch.setattr(sys, 'getsizeof', counting_getsizeof)
    try:
        _drive(world, llm_rounds=5, tools_per_round=3,
               features=('retry', 'custom', 'files', 'compaction', 'fallback', 'tool-phases'))
        world.emit({'type': 'done', 'finishReason': 'stop'})
    finally:
        monkeypatch.setattr(sys, 'getsizeof', getsizeof)
        assert owner.close(2)
        task['_executionSession'].settle(ExecutionPhase.CANCELLED)
        chat_task_runtime.discard(task['id'])
    kinds = collections.Counter(wire['type'] for wire, *_ in world.accepted)
    assert kinds['tool_progress'] and kinds['tool_start'] and kinds['round_end']
    assert any(options.get('progress_frame') is not None for _, _, options, _ in world.accepted)
    assert sum(reference is not None for *_, reference in world.accepted) >= 10
