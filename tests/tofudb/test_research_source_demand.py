"""Complete large Research workspaces through SQL/native semantic boundaries."""
from uuid import uuid4
import pytest
from tests.tofudb.test_tofudb_differential import legacy_driver as legacy_driver, tofudb_binary, tofudb_daemon as tofudb_daemon, _OWNER_ID  # noqa: F401
pytestmark = [pytest.mark.unit, pytest.mark.serial]
pytest_plugins = ('tests._chat_sidecar',)


def test_research_source_document_exceeds_old_native_byte_bound(legacy_driver, tofudb_daemon):
    paper = 'research-source-' + uuid4().hex
    source = 'original evidence ' + 'x' * (9 * 1024 * 1024)
    workspace = {'source_files': [{'path': 'main.tex', 'content': source}]}
    for driver in (legacy_driver, tofudb_daemon):
        payload = {'user_id': _OWNER_ID, 'paper_hash': paper, 'lang': 'en', 'updated_at': 2,
                   'expected_revision': 0, 'workspace': workspace}
        result = driver.execute('research.workspace.put', payload, uuid4().hex)
        assert result.ok, (result.error_code, result.error_message)
        assert result.value['workspace'] == workspace
        assert result.value['revision'] == 1
        stale = driver.execute('research.workspace.put', payload, uuid4().hex)
        assert not stale.ok
        loaded = driver.execute('research.workspace.get', {'user_id': _OWNER_ID, 'paper_hash': paper, 'lang': 'en'}, None)
        assert loaded.ok, (loaded.error_code, loaded.error_message)
        assert loaded.value['workspace'] == workspace
        assert loaded.value['revision'] == 1
