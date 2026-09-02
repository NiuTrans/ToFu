"""Contract ownership and browser/backend format parity coverage."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import (
    native_module_graph,
    orchestration_legacy_test_root,
)

import lib.orchestration.definition_conflict_schema as definition_conflict_schema
import lib.orchestration.definition_contract_registry as definition_contract_registry
import lib.orchestration.definition_contract_schema as definition_contract_schema
import lib.orchestration.definition_wire_projection as definition_wire_projection
import lib.orchestration.definition_write_field_registry as definition_write_field_registry
import lib.orchestration.runtime_wire_contracts as runtime_wire_contracts
from lib.orchestration.wire_formats import (
    DEFINITION_ENTRY_FORMAT,
    DEFINITION_LIST_FORMAT,
    INSPECTION_FORMAT,
    RUNTIME_START_FORMAT,
)


pytestmark = pytest.mark.unit
ROOT = Path(orchestration_legacy_test_root())
SOURCE_ROOT = Path(__file__).resolve().parents[1]
ESBUILD = str(ROOT / 'scripts' / 'vite_test_bundle.mjs')


def test_wire_projectors_return_detached_versioned_documents():
    inspection = {
        'format': INSPECTION_FORMAT,
        'ok': True,
        'warnings': ['review'],
        'contract': {'nodes': 1},
    }
    entry = {
        'id': 'flow-1',
        'definition': {'name': 'Flow', 'nodes': [], 'edges': []},
        'updatedAt': 7,
    }
    projected = definition_wire_projection.project_definition_entry(
        entry, inspection=inspection)
    listing = definition_wire_projection.project_definition_list([entry])

    assert projected['format'] == DEFINITION_ENTRY_FORMAT
    assert listing['format'] == DEFINITION_LIST_FORMAT
    assert projected['inspection'] == inspection
    assert projected is not entry
    assert listing['items'][0] is not entry

    projected['definition']['name'] = 'client mutation'
    projected['inspection']['contract']['nodes'] = 99
    listing['items'][0]['updatedAt'] = 8
    assert entry['definition']['name'] == 'Flow'
    assert inspection['contract']['nodes'] == 1
    assert entry['updatedAt'] == 7

    start = runtime_wire_contracts.project_runtime_start('run-1', 'durable')
    assert start == {
        'format': RUNTIME_START_FORMAT,
        'kind': 'durable',
        'id': 'run-1',
    }
    assert 'legacyIdFields' not in \
        runtime_wire_contracts.runtime_start_contract()
    assert runtime_wire_contracts.runtime_start_contract()['successStatuses'] == {
        'ephemeral': 200,
        'durable': 201,
    }


def test_http_adapters_import_wire_projection_instead_of_service_facade():
    paths = [
        'routes/api_v1/orchestration_definition_routes.py',
        'routes/api_v1/orchestration_authoring_routes.py',
        'routes/api_v1/orchestration_definition_http.py',
        'routes/api_v1/orchestration_runtime_routes.py',
        'routes/api_v1/orchestration_task_routes.py',
        'routes/api_v1/orchestration_mutation_routes.py',
        'routes/api_v1/orchestration_mutation_service_http.py',
        'routes/api_v1/orchestration_run_http.py',
        'routes/api_v1/orchestration_runtime_start_http.py',
    ]
    sources = {path: open(path, encoding='utf-8').read() for path in paths}
    for source in sources.values():
        assert 'lib.orchestration.service import' not in source
        assert 'lib.orchestration.wire_contracts import' not in source
        if 'inspection_response_fields' in source:
            assert 'lib.orchestration.inspection_wire_contract import' \
                in source
        if 'project_definition_' in source \
                or 'definition_request_schema' in source:
            assert (
                'lib.orchestration.definition_wire_projection import' in source
                or 'lib.orchestration.definition_contract_schema import'
                in source
            )
        if 'project_runtime_start' in source:
            assert 'lib.orchestration.runtime_wire_contracts import' in source

def test_definition_wire_responsibilities_have_focused_physical_owners():
    owners = {
        definition_contract_registry.definition_write_contract:
            'lib.orchestration.definition_contract_registry',
        definition_write_field_registry.definition_write_conflict_fields:
            'lib.orchestration.definition_write_field_registry',
        definition_contract_schema.definition_request_schema:
            'lib.orchestration.definition_contract_schema',
        definition_conflict_schema.definition_conflict_response_schema:
            'lib.orchestration.definition_conflict_schema',
        definition_wire_projection.project_definition_entry:
            'lib.orchestration.definition_wire_projection',
        definition_wire_projection.parse_definition_write_precondition:
            'lib.orchestration.definition_wire_projection',
    }
    assert all(owner.__module__ == module for owner, module in owners.items())
    assert not hasattr(
        definition_contract_schema, 'definition_conflict_response_schema')

    contract = definition_contract_registry.definition_write_contract()
    fields = definition_write_field_registry.definition_write_conflict_fields()
    assert contract['conflictFields'] == fields
    conflict = definition_wire_projection.definition_write_conflict(7, 9)
    conflict_schema = definition_conflict_schema.definition_conflict_response_schema()
    assert set(conflict['write']) == set(
        conflict_schema['properties']['write']['required'])
    assert conflict['write']['expectedUpdatedAt'] == 7
    assert conflict['write']['currentUpdatedAt'] == 9

    request_schema = definition_contract_schema.definition_request_schema()
    request_schema['required'].clear()
    assert definition_contract_schema.definition_request_schema()['required'] == [
        'name', 'nodes', 'edges',
    ]


def test_browser_registry_matches_backend_protocol_identifiers():
    from lib.orchestration.wire_formats import orchestration_wire_formats
    from scripts.gen_orchestration_wire_formats import (
        TYPESCRIPT_OUTPUT,
        render_typescript,
    )

    generated_path = ROOT / 'static/js/orchestration-wire-formats.generated.js'
    # The legacy-root view materializes the classic registry path from the
    # native TS owner THROUGH the bundler, so a byte-equal text contract with
    # the generator's classic rendering can never hold (IIFE wrapper vs plain
    # `var` assignment). The contract that must hold is behavioral: the frozen
    # registry object the bundle installs equals the backend's canonical
    # protocol identifiers.
    probe = subprocess.run(
        ['node', '-e',
         'const fs=require("fs");'
         'global.window=globalThis;'
         'eval(fs.readFileSync(process.argv[1],"utf8"));'
         'process.stdout.write(JSON.stringify('
         'globalThis.ORCHESTRATION_WIRE_FORMATS||null));',
         str(generated_path)],
        capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout) == orchestration_wire_formats()
    with open(TYPESCRIPT_OUTPUT, encoding='utf-8') as handle:
        assert handle.read() == render_typescript()


@pytest.mark.skipif(
    not shutil.which('node') or not shutil.which(ESBUILD),
    reason='node + vite test bundler unavailable',
)
def test_classic_and_native_wire_policies_have_behavioral_parity(tmp_path):
    built = tmp_path / 'wire-contract.js'
    compiled = subprocess.run(
        [ESBUILD, str(ROOT / 'frontend/src/features/orchestration/wire-contract.ts'),
         '--bundle', '--format=iife', '--platform=browser',
         f'--outfile={built}'],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr

    script = r"""
const fs=require('fs');global.window=global;
const expected='tofu.orchestration.outcome/v1';
function capture(spec,inspect){
  let unknown=null;
  try{spec('future-wire');}
  catch(error){unknown={name:error.name,message:error.message};}
  return {
    missing:inspect('outcome',{}),
    exact:inspect('outcome',{format:expected}),
    schema:inspect('outcome',{schema:expected}),
    future:inspect('outcome',{format:'future/v2'}),
    malformed:inspect('outcome',{format:7}),
    throwing:spec('outcome',()=>{throw Error('x')}),
    array:spec('outcome',[]),
    unknown,
  };
}
eval(fs.readFileSync(
  'static/js/orchestration-compatibility-defaults.generated.js','utf8'));
eval(fs.readFileSync(
  'static/js/orchestration-compatibility-contracts.js','utf8'));
eval(fs.readFileSync('static/js/orchestration-wire-formats.generated.js','utf8'));
eval(fs.readFileSync('static/js/orchestration-wire-contract.js','utf8'));
const classic=capture(
  orchestrationWireContractSpec,inspectOrchestrationWireFormat);
require(process.argv[1]);
const native=capture(
  global.orchestrationWireContractSpec,
  global.inspectOrchestrationWireFormat);
process.stdout.write(JSON.stringify({classic,native}));
"""
    run = subprocess.run(
        ['node', '-e', script, str(built)],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result['native'] == result['classic']
    assert result['native']['unknown'] == {
        'name': 'OrchestrationWireContractError',
        'message': 'Unknown orchestration wire contract: future-wire',
    }
    assert result['native']['array']['contract'] is None


_NATIVE_CONTRACT_OWNER = native_module_graph([
    (
        'orchestration-contract-owners.js',
        SOURCE_ROOT / 'frontend/src/features/orchestration/contracts.ts',
    ),
    (
        'orchestration-outcome-owner.js',
        SOURCE_ROOT / 'frontend/src/features/orchestration/outcome-result.ts',
    ),
])

_NATIVE_OWNER_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  targets: [process.argv[2]],
});
const published = {
  outcomeContract: {
    format: 'tofu.orchestration.outcome/v1',
    categories: ['success', 'incomplete', 'failure', 'aborted'],
    incompleteStopReasons: ['budget_exhausted'],
    displayLimits: { final: 8, error: 4 },
  },
};
check('facade publishes focused contract functions',
  typeof resolveDirectContractSource === 'function'
    && typeof publishedContract === 'function'
    && typeof compatibilityContract === 'function'
    && typeof wireContractSpec === 'function'
    && typeof inspectWireFormat === 'function');
check('published contracts override detached compatibility defaults',
  publishedContract('outcomeContract', published) === published.outcomeContract
    && compatibilityContract('outcomeContract').categories.includes('success'));
const spec = wireContractSpec('outcome', published.outcomeContract);
const inspected = inspectWireFormat(
  'outcome', { format: spec.expected }, published.outcomeContract);
check('wire owner resolves and validates the canonical format',
  spec.supported && inspected.present && inspected.supported
    && inspected.identityField === 'format');
const normalized = normalizeOrchestrationOutcome({
  outcome: { format: spec.expected, category: 'success', engine_status: 'completed' },
}, published);
const projected = projectOrchestrationOutcomeText(
  '123456789', 'final', published.outcomeContract);
check('outcome owner consumes the facade contract at runtime',
  normalized.canonical && normalized.ok
    && projected.text === '12345678' && projected.truncated);
let errorName = '';
try { wireContractSpec('future-contract'); } catch (error) { errorName = error.name; }
check('unknown contracts fail closed with the typed wire error',
  errorName === 'OrchestrationWireContractError');
report();
"""


def test_native_contract_facade_has_focused_physical_owners():
    run_harness(
        _NATIVE_CONTRACT_OWNER,
        _NATIVE_OWNER_HARNESS,
        expect_pass=5,
        label='native orchestration contract owners',
    )
