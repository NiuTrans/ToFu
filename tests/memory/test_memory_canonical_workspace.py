"""Project memories belong to the canonical project, not an execution checkout."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from lib.tasks_pkg.handlers.memory import _handle_memory_tool
from lib.tasks_pkg.model_config import _memory_scope
from lib.tasks_pkg.orchestrator._memory_prefetch import memory_prefetch_corpus_roots

pytestmark = pytest.mark.unit


def test_isolated_worker_writes_project_memory_to_canonical_root(tmp_path):
    canonical = tmp_path / 'canonical'
    worker = tmp_path / 'worker'
    canonical.mkdir()
    worker.mkdir()
    cfg = {
        'projectPath': str(worker),
        'canonicalProjectPath': str(canonical),
        'projectPaths': [str(worker), str(tmp_path / 'extra-worker')],
        'canonicalProjectPaths': [str(canonical), str(tmp_path / 'extra-canonical')],
    }
    created = []
    from lib.memory.storage import create_memory as create

    def create_memory(**kwargs):
        record = create(**kwargs)
        created.append(record)
        return record

    with patch('lib.memory.storage.create_memory', create_memory), \
            patch('lib.tasks_pkg.handlers.memory._finalize_tool_round'), \
            patch('lib.tasks_pkg.handlers.memory._build_simple_meta', return_value={}):
        _handle_memory_tool(
            {}, {}, 'create_memory', 'call-1',
            {'name': 'Canonical lesson', 'description': 'Worker memory location',
             'body': 'Project memories use the canonical project.', 'scope': 'project'},
            0, {}, cfg, str(worker), True)

    assert len(created) == 1
    filename = created[0]['id'] + '.md'
    assert (canonical / '.tofu' / 'memories' / filename).is_file()
    assert not (worker / '.tofu').exists()


def test_memory_prefetch_uses_the_same_canonical_roots():
    cfg = {
        'projectPath': '/worker',
        'canonicalProjectPath': '/canonical',
        'projectPaths': ['/worker', '/extra-worker', '/worker'],
        'canonicalProjectPaths': ['/canonical', '/extra-canonical', '/canonical'],
    }
    enabled, project_path, project_enabled = _memory_scope(cfg)

    # Execution stays in the isolated checkout; only memory maps to the project.
    assert enabled and project_enabled and project_path == '/worker'
    assert memory_prefetch_corpus_roots(cfg, project_path, project_enabled) == (
        '/canonical', ['/extra-canonical'])
