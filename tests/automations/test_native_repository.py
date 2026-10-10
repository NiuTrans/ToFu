"""Automation persistence through a disposable native authority and real client."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil

import pytest

from lib.automations.definitions import prepare_definition
from lib.automations.errors import AutomationConflict
from lib.automations.execution_repository import AutomationExecutionRepository
from lib.identity import PrincipalContext
from scripts.tofudb_perf_compare import TofuDbEngine, _OWNER_ID
from tests.support.automation_fixtures import skill_definition, trigger

pytestmark = pytest.mark.api


def test_native_occurrence_cas_and_reopen_preserve_exact_evidence(tmp_path):
    configured = os.environ.get('TOFUDB_BIN', '')
    if not configured:
        pytest.skip('Set TOFUDB_BIN to a matched native artifact')
    binary = Path(configured).resolve()
    assert binary.is_file()
    engine = TofuDbEngine(binary, authority_dir_parent=tmp_path)
    engine.start()
    directories = list(engine._dirs)
    principal = PrincipalContext.user(subject_id='native-automation-test', owner_user_id=_OWNER_ID,
        tenant_id='test-tenant', scopes={'admin'})
    repository = AutomationExecutionRepository(principal, client_factory=lambda **kwargs: engine._client)
    try:
        prepared = prepare_definition(skill_definition())
        definition = repository.publish(prepared, expected_version=0, now=100)
        event = trigger('manual')
        with ThreadPoolExecutor(max_workers=4) as pool:
            runs = list(pool.map(lambda _: repository.admit(definition.id, event, now=101), range(4)))
        assert len({row.id for row in runs}) == 1
        original = runs[0]
        assert repository.get('run', original.id).version == 1
        cursor = repository.put('cursor', 'native-cursor', {'observed':'one'}, expected_version=0)
        saved = repository.put('cursor', cursor.id, {'observed':'two'}, expected_version=cursor.version)
        with pytest.raises(AutomationConflict):
            repository.put('cursor', cursor.id, cursor.value, expected_version=cursor.version)
        assert saved.version == 2
        # Graceful close + reopen the same authority; no copied/fallback store.
        engine.restart_cycle()
        reread = repository.get('cursor', cursor.id)
        assert reread.version == 2
        assert reread.value == saved.value
        assert repository.admit(definition.id, event, now=105).id == original.id
        # Strict durability also preserves the acknowledged aggregate after
        # an ungraceful daemon death and WAL replay.
        engine.crash_cycle()
        assert repository.get('cursor', cursor.id).value == saved.value
        assert repository.get('run', original.id).value == original.value
        # Emit a bounded measurement for the owning operations contract.
        print(json.dumps({'definitionBytes': len(definition.document_json.encode('utf-8')),
                          'runBytes': len(original.document_json.encode('utf-8')),
                          'occurrencesAfterRedelivery': len(repository.page('run')[0])}))
    finally:
        if engine._process is not None and engine._process.poll() is None:
            engine.stop()
        for directory in directories:
            shutil.rmtree(directory, ignore_errors=True)
