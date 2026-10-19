"""Complete pending plans cross the physical selected storage boundary."""
from copy import deepcopy
import json
import time
import pytest

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('credential_storage', ['sqlite', 'tofudb'], indirect=True)
def test_large_program_frontier_roundtrip_preserves_final_audit_and_cas(credential_storage):
    from lib.evolution import programs
    from lib.storage.errors import StorageError
    steps = [{'kind': 'experiment', 'title': f'Owned experiment {index}',
              'instruction': '完整原始计划' * 300, 'checks': ['Keep original evidence.']}
             for index in range(241)]
    final = {'kind': 'final', 'title': 'Original final audit',
             'instruction': 'Audit every original requirement.', 'checks': ['Retain all failed checks.']}
    started = time.perf_counter()
    program = programs.create_program({'objective': 'Retain the complete large pending plan.',
                                       'steps': steps + [final]},
                                      user_id=123, idempotency_key='large-physical-frontier')
    size = len(json.dumps(program, ensure_ascii=False).encode())
    assert size > 1 << 20
    assert programs.get_program(program['program_id'], user_id=123) == program
    print(f'program_frontier_steps={len(program["queue"])} utf8_bytes={size} write_read_seconds={time.perf_counter()-started:.4f}')
    old, version = programs._read(program['program_id'], 123)
    addition = {'kind': 'experiment', 'title': 'Another authorized check',
                'instruction': 'Retain the original final audit.', 'checks': ['Preserve order.']}
    expanded = programs.append_steps(program['program_id'], {'steps': [addition]},
                                     user_id=123, idempotency_key='physical-expansion')
    assert expanded['queue'] == steps + [addition, final]
    with pytest.raises(StorageError) as failure:
        programs._save(deepcopy(old), version, 123)
    assert failure.value.code == 'database_conflict'
    assert programs.get_program(program['program_id'], user_id=123) == expanded
