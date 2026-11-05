"""Request-list projections preserve fold evidence without transporting full tails."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from lib.task_replay.generated_request_inspector_projection import PROJECTION
from lib.task_replay.request_inspector_projection import project_request_inspector_event

ROOT = Path(__file__).resolve().parents[2]
CASES = json.loads((ROOT / 'tests/fixtures/request_inspector_projection.json').read_text())
pytestmark = pytest.mark.unit


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['name'])
def test_storage_projection_matches_shared_contract_without_mutating_source(case):
    original = copy.deepcopy(case['event'])
    assert project_request_inspector_event(case['event']) == case['expected']
    assert case['event'] == original


def test_projection_constants_have_no_generator_drift():
    subprocess.run([sys.executable, str(ROOT / 'scripts/gen_request_inspector_projection.py'),
                    '--check'], check=True, cwd=ROOT)


def test_projected_fold_preserves_round_counts_tool_attribution_and_usage():
    from lib.tasks_pkg.request_inspector import _fold_event_rows

    request = copy.deepcopy(CASES[0]['event'])
    request.update(roundNum=1, newMessages=[{'role': 'user', 'content': 'private' * 10000}])
    state = copy.deepcopy(CASES[0]['event'])
    state.update(kind='state', roundNum=2)
    payloads = [request, state, CASES[0]['event'],
                {'type': 'round_usage', 'roundNum': 1, 'turn': 'root',
                 'tokensIn': 30, 'tokensOut': 5, 'usage': {'trace_id': 'trace'}}]
    rows = [{'event_id': i, 'ts_ms': i, 'type': p['type'], 'payload': p}
            for i, p in enumerate(payloads)]
    projected = [dict(row, payload=project_request_inspector_event(row['payload'])) for row in rows]
    before = _fold_event_rows(rows)
    assert _fold_event_rows(projected) == before
    assert before[0][0]['toolNames'] == ['read_file', 'search', 'inspect']
    assert len(json.dumps(projected)) < len(json.dumps(rows)) / 10


def test_metadata_cache_never_supplies_incomplete_detail_and_full_cache_can_supply_list(monkeypatch):
    from lib import storage
    from lib.tasks_pkg import request_inspector as inspector

    event = copy.deepcopy(CASES[0]['event'])
    reads = []

    class Storage:
        def query(self, operation, payload):
            assert operation == 'event.list'
            view = payload.get('payload_projection', 'full')
            reads.append(view)
            result = project_request_inspector_event(event) if view == PROJECTION else event
            return [{'sequence': 1, 'created_at_ms': 100, 'event': copy.deepcopy(result)}]

    monkeypatch.setattr(storage, 'get_storage_client', lambda: Storage())
    monkeypatch.setattr(inspector, '_EVENTS_CACHE', {})
    monkeypatch.setattr(inspector, '_task_may_append', lambda _: False)
    projected, ok = inspector._read_events('owned', False, payload_projection=PROJECTION)
    assert ok and 'contextManifest' not in projected[0]['payload']
    complete, ok = inspector._read_events('owned', False)
    assert ok and complete[0]['payload'] == event
    assert reads == [PROJECTION, 'full']
    inspector.invalidate_task_cache('owned')
    inspector._read_events('owned', False)
    inspector._read_events('owned', False, payload_projection=PROJECTION)
    assert reads == [PROJECTION, 'full', 'full']
