"""Frontend budget accounting must include Rollup shared chunks exactly once."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


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
