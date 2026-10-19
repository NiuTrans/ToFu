"""Complete evolution workspace records cross the physical storage boundary."""
from copy import deepcopy
import json
import time
import pytest

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('credential_storage', ['sqlite','tofudb'], indirect=True)
def test_workspace_roundtrip_above_old_record_budget_preserves_cas(credential_storage):
    from lib.evolution import library, records
    from lib.storage.errors import StorageError
    materials = [dict(material_id=f'mat-{index:024x}', run_id='large', kind='paper',
                      title='研'*240, url=f'https://example.org/{index}'+'研'*1900,
                      note='研'*500, origin='user', task_id='', created_at=1, updated_at=1)
                 for index in range(160)]
    value = {'run_id': 'large', 'materials': materials}
    library._validate(value, 'library')
    assert len(json.dumps(value, ensure_ascii=False).encode()) > 1024*1024
    started = time.perf_counter()
    version = records.put_record(records.NAMESPACE_LIBRARIES, '123:large', value,
                                 kind='library', expected_version=0)
    assert records.get_record(records.NAMESPACE_LIBRARIES, '123:large') == value
    print(f'workspace_materials={len(materials)} utf8_bytes={len(json.dumps(value,ensure_ascii=False).encode())} write_read_seconds={time.perf_counter()-started:.4f}')
    changed = deepcopy(value)
    changed['materials'][0]['title'] = 'updated'
    records.put_record(records.NAMESPACE_LIBRARIES, '123:large', changed,
                       kind='library', expected_version=version)
    with pytest.raises(StorageError) as conflict:
        records.put_record(records.NAMESPACE_LIBRARIES, '123:large', value,
                           kind='library', expected_version=version)
    assert conflict.value.code == 'database_conflict'
    assert records.get_record(records.NAMESPACE_LIBRARIES, '123:large') == changed


@pytest.mark.parametrize('credential_storage', ['sqlite', 'tofudb'], indirect=True)
def test_complete_strategy_lineage_crosses_physical_storage_and_preserves_cas(credential_storage):
    from lib.evolution.contracts import validate_strategy
    from lib.evolution import records
    from lib.storage.errors import StorageError
    lineage = [f'episode-{index:06d}-' + 'x' * 120 for index in range(9000)]
    value = validate_strategy({'schema_version': 1, 'template_id': 'large-lineage',
        'domain': 'code', 'stages': [{'kind': 'execute', 'detail': 'd'}],
        'applicability_terms': ['needle'], 'lineage': lineage, 'samples': len(lineage),
        'success_rate': 1, 'created_at': 1, 'updated_at': 1})
    size = len(json.dumps(value, ensure_ascii=False).encode())
    assert size > 1 << 20
    key = records.entity_key(value['template_id'], 123, kind='strategy')
    started = time.perf_counter()
    version = records.put_record(records.NAMESPACE_STRATEGIES, key, value,
                                 kind='strategy', expected_version=0)
    assert records.get_record(records.NAMESPACE_STRATEGIES, key) == value
    print(f'strategy_lineage={len(lineage)} utf8_bytes={size} write_read_seconds={time.perf_counter()-started:.4f}')
    changed = deepcopy(value)
    changed['updated_at'] = 2
    records.put_record(records.NAMESPACE_STRATEGIES, key, changed,
                       kind='strategy', expected_version=version)
    with pytest.raises(StorageError) as failure:
        records.put_record(records.NAMESPACE_STRATEGIES, key, value,
                           kind='strategy', expected_version=version)
    assert failure.value.code == 'database_conflict'
    assert records.get_record(records.NAMESPACE_STRATEGIES, key) == changed
