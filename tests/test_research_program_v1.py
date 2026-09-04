"""Executable contracts for provider-neutral research production."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_empty_workspace_conforms_to_machine_readable_contract():
    from lib.research.workspace import empty_workspace

    schema = json.loads((
        ROOT / 'contracts/research_program_v1.schema.json'
    ).read_text(encoding='utf-8'))
    Draft202012Validator(schema).validate(empty_workspace('Sparse attention'))


def test_capability_catalog_is_derived_from_live_tool_contracts():
    from lib.research.capabilities import build_capability_catalog

    class Bridge:
        def get_tool_catalog_snapshot(self):
            return [{
                'server_name': 'arbitrary-provider',
                'tool_name': 'launch_trial',
                'namespaced_name': 'mcp__arbitrary-provider__launch_trial',
                'description': 'Launch an experiment job on a GPU cluster',
                'read_only_hint': False,
                'schema_hash': 'schema-1',
                'openai_def': {'function': {
                    'name': 'mcp__arbitrary-provider__launch_trial',
                    'description': 'Launch an experiment job on a GPU cluster',
                    'parameters': {'type': 'object', 'properties': {}},
                }},
            }]

    catalog = build_capability_catalog(Bridge())
    tool = catalog['tools'][0]
    assert tool['server'] == 'arbitrary-provider'
    assert any(row['id'] == 'experiment.execute'
               for row in tool['suggested_capabilities'])
    assert 'hope' not in json.dumps(catalog).lower()
    assert 'overleaf' not in json.dumps(catalog).lower()


def test_read_binding_rejects_a_write_annotated_tool():
    from lib.research.capabilities import validate_bindings

    got = validate_bindings([{
        'capability': 'literature.search',
        'tool': 'mcp__provider__search',
        'enabled': True,
    }], catalog={'tools': [{
        'name': 'mcp__provider__search', 'read_only': False,
    }]})
    assert got['resolved'] == []
    assert got['problems'][0]['code'] == 'read_capability_requires_read_only_tool'


def test_binding_schema_drift_revokes_execution_until_owner_rebinds():
    from lib.research.capabilities import validate_bindings

    got = validate_bindings([{
        'capability': 'experiment.execute',
        'tool': 'mcp__provider__run', 'schema_hash': 'old', 'enabled': True,
    }], catalog={'tools': [{
        'name': 'mcp__provider__run', 'read_only': False,
        'schema_hash': 'new',
    }]})
    assert got['resolved'] == []
    assert got['problems'][0]['code'] == 'tool_schema_changed'


def test_binding_without_live_schema_receipt_cannot_execute():
    from lib.research.capabilities import validate_bindings

    got = validate_bindings([{
        'capability': 'experiment.execute',
        'tool': 'mcp__provider__run', 'enabled': True,
    }], catalog={'tools': [{
        'name': 'mcp__provider__run', 'read_only': False,
        'schema_hash': 'current',
    }]})
    assert got['resolved'] == []
    assert got['problems'][0]['code'] == 'binding_schema_hash_missing'


def test_latex_scaffold_preserves_edits_and_exports_safe_deterministic_zip():
    from lib.research.manuscript import export_source_zip, scaffold_source_files

    program = {
        'manuscript': {'title': 'A&B_Study', 'abstract': 'Observed 5% gain.'},
        'source_files': [{
            'path': 'sections/method.tex', 'content': 'USER EDIT',
        }, {'path': '../escape.tex', 'content': 'bad'}],
    }
    files = scaffold_source_files(program)
    by_path = {row['path']: row['content'] for row in files}
    assert by_path['sections/method.tex'] == 'USER EDIT'
    assert r'A\&B\_Study' in by_path['main.tex']
    assert '../escape.tex' not in by_path
    first = export_source_zip({'source_files': files})
    second = export_source_zip({'source_files': files})
    assert first == second
    with zipfile.ZipFile(BytesIO(first)) as archive:
        assert 'main.tex' in archive.namelist()
        assert all(not name.startswith('/') and '..' not in name.split('/')
                   for name in archive.namelist())


def test_readiness_invalidates_compile_and_publication_after_source_edit():
    from lib.research.program import readiness, source_tree_digest
    from lib.research.workspace import empty_workspace

    workspace = empty_workspace('Evidence contracts')
    workspace['source_files'] = [{'path': 'main.tex', 'content': 'v1'}]
    digest = source_tree_digest(workspace['source_files'])
    workspace['compilation'].update(status='passing', source_digest=digest)
    workspace['publication'].update(status='published', source_digest=digest)
    assert readiness(workspace)['published_current'] is True
    workspace['source_files'][0]['content'] = 'v2'
    got = readiness(workspace)
    assert got['published_current'] is False
    assert next(row for row in got['gates'] if row['id'] == 'compile')['ok'] is False


def test_normalization_downgrades_a_stale_passing_compile_receipt():
    from lib.research.program import normalize_program_fields, source_tree_digest

    old_sources = [{'path': 'main.tex', 'content': 'v1'}]
    normalized = normalize_program_fields({
        'source_files': [{'path': 'main.tex', 'content': 'v2'}],
        'compilation': {
            'mode': 'bound_tool', 'status': 'passing',
            'source_digest': source_tree_digest(old_sources),
        },
    })
    assert normalized['compilation']['status'] == 'not_run'


def test_normalization_rejects_non_http_publication_links():
    from lib.research.program import normalize_program_fields

    normalized = normalize_program_fields({
        'publication': {
            'status': 'published',
            'project_url': 'javascript:alert(document.domain)',
        },
    })
    assert normalized['publication']['project_url'] == ''
