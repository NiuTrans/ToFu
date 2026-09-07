"""Frontend budget accounting must include Rollup shared chunks exactly once."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_vite_total_includes_shared_static_chunks_and_deduplicates_files():
    from scripts.frontend_budget import _vite_graph_and_url_assets

    manifest = {
        'frontend/src/main.ts': {
            'file': 'assets/main.js', 'isEntry': True,
            'imports': ['_shared.js'],
        },
        'frontend/src/feature.ts': {
            'file': 'assets/feature.js', 'isDynamicEntry': True,
            'imports': ['_shared.js'],
        },
        '_shared.js': {'file': 'assets/shared.js'},
        'style.css': {'file': 'assets/style.css'},
        'duplicate': {'file': 'assets/shared.js'},
    }
    graph, url_assets = _vite_graph_and_url_assets(manifest)
    assert graph == {
        'assets/main.js', 'assets/feature.js', 'assets/shared.js',
    }
    assert url_assets == set()


def test_vite_url_assets_split_out_of_the_module_graph():
    """The pdf.worker is an on-demand binary, NOT module-graph JS: Vite
    emits it as a standalone row with no inbound edges, so it lands in its
    own budgeted bucket while the tiny ?url shim (a dynamic entry) stays in
    the graph."""
    from scripts.frontend_budget import _vite_graph_and_url_assets

    manifest = {
        'frontend/src/main.ts': {'file': 'assets/main.js', 'isEntry': True},
        # The standalone worker binary: no entry flag, no inbound edges.
        'node_modules/pdfjs-dist/legacy/build/pdf.worker.min.mjs': {
            'file': 'assets/pdf.worker.min-rsCePomN.mjs',
            'src': 'node_modules/pdfjs-dist/legacy/build/pdf.worker.min.mjs',
        },
        # The URL shim that hands the worker's URL to pdfjs at runtime.
        'node_modules/pdfjs-dist/legacy/build/pdf.worker.min.mjs?url': {
            'file': 'assets/pdf.worker.min-7WEkucD6.js',
            'isDynamicEntry': True,
        },
    }
    graph, url_assets = _vite_graph_and_url_assets(manifest)
    assert graph == {'assets/main.js', 'assets/pdf.worker.min-7WEkucD6.js'}
    assert url_assets == {'assets/pdf.worker.min-rsCePomN.mjs'}


def test_vite_budget_rejects_paths_outside_the_asset_directory():
    from scripts.frontend_budget import _vite_graph_and_url_assets

    with pytest.raises(ValueError, match='unsafe Vite asset path'):
        _vite_graph_and_url_assets({'bad': {'file': '../main.js'}})


def test_vite_url_assets_require_an_explicit_whitelist_entry():
    """A chunk that falls OUT of the manifest graph must not silently join
    the URL-asset budget line (each file quietly passing its own per-file
    budget): the gate fails loudly until a reviewer wires it into the graph
    or extends the whitelist deliberately."""
    from scripts.frontend_budget import _url_asset_whitelist_violations

    assert _url_asset_whitelist_violations(set()) == []
    assert _url_asset_whitelist_violations(
        {'assets/pdf.worker.min-rsCePomN.mjs'}) == []
    assert _url_asset_whitelist_violations({
        'assets/pdf.worker.min-rsCePomN.mjs',
        'assets/orchestration-a1b2c3d4.js',
    }) == ['assets/orchestration-a1b2c3d4.js']


def test_tool_presentation_policy_maps_only_explicit_modules_to_one_chunk():
    script = r"""
import { manualChunkName } from './vite.config.mjs';
const moduleIds = [
  '/repo/frontend/src/conversation/presentation/image-source-policy.ts',
  '/repo/frontend/src/conversation/presentation/tool-approval-presentation.ts',
  'C:\\repo\\frontend\\src\\conversation\\presentation\\tool-command-execution-presentation.ts',
  '/repo/frontend/src/conversation/presentation/tool-injection-presentation.ts',
  '/repo/frontend/src/conversation/presentation/write-gate-refusal.ts',
  '/repo/frontend/src/conversation/ui/human-guidance-actions.ts',
  '/repo/frontend/src/conversation/presentation/conversation-view-model.ts',
  '/repo/frontend/src/features/paper.ts',
];
process.stdout.write(JSON.stringify(moduleIds.map(manualChunkName)));
"""
    completed = subprocess.run(
        ['node', '--input-type=module', '--eval', script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == [
        'tool-presentation',
        'tool-presentation',
        'tool-presentation',
        'tool-presentation',
        'tool-presentation',
        None,
        None,
        None,
    ]


def test_task_mode_owner_family_maps_to_one_separate_chunk():
    """The Task Mode owner family splits out of the lazy orchestration chunk
    (independent cache generation + per-chunk budget headroom), while the
    rest of the studio stays in the entry chunk."""
    script = r"""
import { manualChunkName } from './vite.config.mjs';
const moduleIds = [
  '/repo/frontend/src/features/orchestration/task-mode-controller-hub.ts',
  'C:\\repo\\frontend\\src\\features\\orchestration\\task-mode-run-store.ts',
  '/repo/frontend/src/features/orchestration/graph.ts',
  '/repo/frontend/src/features/orchestration/api-client.ts',
  '/repo/frontend/src/features/orchestration.ts',
];
process.stdout.write(JSON.stringify(moduleIds.map(manualChunkName)));
"""
    completed = subprocess.run(
        ['node', '--input-type=module', '--eval', script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == [
        'orchestration-task-mode',
        'orchestration-task-mode',
        None,
        None,
        None,
    ]


def test_conversation_architecture_contract_has_one_owner_per_boundary():
    from scripts.check_architecture import load_frontend_architecture_contract

    document = load_frontend_architecture_contract()
    assert document['contract'] == 'tofu.frontend-conversation-architecture/v1'
    assert len(document['owners']) == len(set(document['owners']))
    assert document['owners']['normalizedTurnState'].endswith(
        'conversation/domain/turn-store.ts')
    assert document['owners']['viewModelProjection'].endswith(
        'conversation-view-model.ts')
    assert document['owners']['conversationDom'].endswith(
        'conversation-surface.ts')
    assert document['owners']['humanGuidanceActions'].endswith(
        'conversation/ui/human-guidance-actions.ts')


def test_frontend_legacy_debt_is_a_monotonic_zero_target_ratchet():
    from scripts.check_architecture import frontend_legacy_debt_violations

    output, violations = frontend_legacy_debt_violations()
    assert not violations, '\n'.join(violations)
    assert output

    document = json.loads((
        ROOT / 'contracts/frontend_conversation_architecture_v1.json'
    ).read_text(encoding='utf-8'))
    debts = document['legacyDebt']
    assert all(item['target'] == 0 for item in debts)
    assert {item['metric'] for item in debts} == {
        'file_count', 'byte_count', 'regex_count',
    }
