"""Frontend budget accounting must include Rollup shared chunks exactly once."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_vite_total_includes_shared_static_chunks_and_deduplicates_files():
    from scripts.frontend_budget import _vite_javascript_paths

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
    assert _vite_javascript_paths(manifest) == {
        'assets/main.js', 'assets/feature.js', 'assets/shared.js',
    }


def test_vite_budget_rejects_paths_outside_the_asset_directory():
    from scripts.frontend_budget import _vite_javascript_paths

    with pytest.raises(ValueError, match='unsafe Vite asset path'):
        _vite_javascript_paths({'bad': {'file': '../main.js'}})


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
