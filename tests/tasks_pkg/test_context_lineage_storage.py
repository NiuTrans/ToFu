"""Request source maps share the existing incremental evidence storage owner."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.snapshot_delta import SnapshotProjector, rebuild_snapshots

pytestmark = pytest.mark.unit


def frames(kind, count=8):
    return [{'type': kind, 'kind': 'request', 'turn': 'turn', 'roundNum': n,
             'messages': [{'role': 'user', 'content': 'request'}],
             'contextLineage': {'version': 1, 'stage': 'canonical',
                                'sources': [{'id': str(i), 'spanHash': 'a' * 64}
                                            for i in range(n)]}}
            for n in range(1, count + 1)]


@pytest.mark.parametrize('kind', ['messages_snapshot', 'tool_wire_projection'])
def test_lineage_roundtrip_stores_each_unchanged_source_once(kind):
    original = frames(kind)
    untouched = deepcopy(original)
    projector = SnapshotProjector()
    projected = [projector.project('task', frame) for frame in original]
    assert sum(len(frame['contextLineage'].get('newSources', [])) for frame in projected) == len(original)
    rebuilt = rebuild_snapshots([{'type': kind, 'payload': frame} for frame in projected])
    for source, restored in zip(original, rebuilt):
        assert source['contextLineage'] == restored['contextLineage']
    assert original == untouched


def test_wire_lineage_without_its_baseline_is_explicitly_degraded():
    projector = SnapshotProjector()
    projected = [projector.project('task', frame) for frame in frames('tool_wire_projection')]
    rebuilt = rebuild_snapshots([{'type': 'tool_wire_projection', 'payload': projected[-1]}])
    assert rebuilt[0]['contextLineage']['degraded']
    assert rebuilt[0]['contextLineage']['degradedReason']


def test_lineage_turns_and_projector_forget_do_not_borrow_another_baseline():
    projector = SnapshotProjector()
    frame = frames('tool_wire_projection')[-1]
    projector.project('task', frame)
    for changed in ({**frame, 'turn': 'another'}, frame):
        projector.forget('task')
        projected = projector.project('task', changed)
        assert projected['contextLineage']['prefixLen'] == 0


@pytest.mark.parametrize('invalid', [
    {'prefixHash': 'incorrect'}, {'newSources': 7}, {'sourceCount': -1},
    {'sourceDeltaVersion': 2}, {'prefixLen': True},
])
def test_corrupt_source_delta_is_explicitly_degraded(invalid):
    projector = SnapshotProjector()
    projected = [projector.project('task', frame) for frame in frames('tool_wire_projection', 2)]
    projected[-1]['contextLineage'].update(invalid)
    result = rebuild_snapshots([{'payload': frame} for frame in projected])
    assert result[-1]['contextLineage']['degraded']
    assert result[-1]['contextLineage']['degradedReason']
