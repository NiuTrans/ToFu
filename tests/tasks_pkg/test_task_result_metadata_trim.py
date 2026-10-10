"""Task-result wire diagnostics are rejected at the storage boundary."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


def _fat_meta():
    return {
        'finishReason': 'stop',
        'programmaticAdoptionNudges': [{
            'afterRound': 3,
            'targetRound': 4,
            'reason': 'serial_direct_reads',
            'chainLength': 3,
            'tools': ['find_files', 'grep_search', 'read_files'],
            'max': 1,
        }],
        'toolRoundTripNudges': [{
            'afterRound': 6,
            'targetRound': 7,
            'reason': 'serial_single_tool_rounds',
            'chainLength': 6,
            'tools': ['run_command'] * 6,
            'max': 1,
        }],
        'usage': {'input_tokens': 5},
        'apiRounds': [
            {'round': 1, 'usage': {
                'input_tokens': 3,
                'trace_id': 'trace-visible',
                '_dispatch': {'provider': 'visible'},
                '_wire_bytes': list(range(1000)),
                '_wire_field_bytes': {'messages': 'x' * 10000},
            }},
            {'round': 2, 'usage': {'output_tokens': 7}},
        ],
    }


def test_trim_metadata_reuses_live_sanitizer_and_preserves_visible_fields():
    from lib.storage_projection import (
        project_task_result_metadata_for_storage,
    )

    meta = _fat_meta()
    clean = project_task_result_metadata_for_storage(meta)
    assert len(json.dumps(clean)) < len(json.dumps(meta)) / 5
    usage = clean['apiRounds'][0]['usage']
    assert not any(key.startswith('_wire_') for key in usage)
    assert usage['trace_id'] == 'trace-visible'
    assert usage['_dispatch'] == {'provider': 'visible'}
    assert clean['finishReason'] == 'stop' and clean['usage'] == {'input_tokens': 5}
    assert clean['programmaticAdoptionNudges'] == (
        meta['programmaticAdoptionNudges'])
    assert clean['toolRoundTripNudges'] == meta['toolRoundTripNudges']
    assert '_wire_bytes' in meta['apiRounds'][0]['usage'], 'input must not mutate'


def test_sidecar_projection_sanitizes_text_metadata_and_indexes_experiment():
    from lib.storage_sidecar.operations_pkg._records import (
        _project_task_result_experiment,
    )

    meta = _fat_meta()
    meta['costExperiment'] = {'experimentId': 'exp-42'}
    value = {'metadata': json.dumps(meta), 'status': 'done'}
    clean_value = _project_task_result_experiment(value)
    clean_meta = json.loads(clean_value['metadata'])

    assert clean_value['cost_experiment_id'] == 'exp-42'
    assert not any(
        key.startswith('_wire_')
        for key in clean_meta['apiRounds'][0]['usage']
    )
    assert '_wire_bytes' in meta['apiRounds'][0]['usage']
    assert clean_value is not value


def test_sidecar_projection_also_guards_running_checkpoints():
    from lib.storage_sidecar.operations_pkg._records import (
        _project_task_result_experiment,
    )

    value = {'metadata': _fat_meta(), 'status': 'running'}
    clean_value = _project_task_result_experiment(value)
    usage = clean_value['metadata']['apiRounds'][0]['usage']
    assert not any(key.startswith('_wire_') for key in usage)
    assert clean_value['status'] == 'running'
