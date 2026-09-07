"""Machine-readable ownership contract for the demand-loaded Project workspace."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'frontend/src/runtime/sections/manifest.json'


def _project_bundle() -> tuple[dict, dict]:
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    bundle = next(
        row for row in manifest['lazyBundles']
        if row['name'] == 'project-presenters'
    )
    return manifest, bundle


def test_workspace_is_lazy_while_state_and_coding_actions_stay_retained():
    manifest, bundle = _project_bundle()
    retained = [row['source'] for row in manifest['sections']]

    assert [row['source'] for row in bundle['sections']] == ['project.js']
    assert 'project.js' not in retained
    assert retained.count('project_state.js') == 1
    assert retained.count('execution-interactions.js') == 1
    assert bundle['output'] == (
        'frontend/src/runtime/project-presenters.generated.js'
    )


def test_workspace_declares_one_closed_dependency_surface():
    _manifest, bundle = _project_bundle()
    module_imports = {
        row['source']: tuple(row['bindings'])
        for row in bundle['moduleImports']
    }
    services = {
        row['name']: row['kind']
        for row in bundle['runtimeServices']
    }

    assert module_imports == {
        'frontend/src/i18n/index.ts': ('t',),
        'frontend/src/html-safety.ts': ('escapeHtml',),
        'frontend/src/core/project-browse-coordinator.ts': (
            'createProjectBrowseCoordinator',
        ),
        'frontend/src/features/project/directory-browser.ts': (
            'createProjectDirectoryBrowser',
        ),
        'frontend/src/features/project/presentation-assets.ts': (
            'PROJECT_PRESENTATION_ASSETS',
        ),
    }
    assert services == {
        'Api': 'object',
        'ProjectPresentationShellState': 'object',
        '_applyProjectData': 'function',
        '_saveConvProjectPath': 'function',
        '_updateProjectUI': 'function',
        'captureActiveConversationSettings': 'function',
        'debugLog': 'function',
        'generateId': 'function',
        'getActiveFolderId': 'function',
        'loadProjectStatus': 'function',
        'onProjectAttached': 'function',
        'onProjectCleared': 'function',
        'reconcileConversationCatalogMetadata': 'function',
        'renderConversationList': 'function',
        'showAlert': 'function',
        'showConfirm': 'function',
        'showPrompt': 'function',
        'showToast': 'function',
    }
    # The typed renderer emits these two declarative action names, so they are
    # explicit ports instead of relying on the classic-source action scanner.
    assert bundle['runtimeExports'] == [
        'ProjectModalPresentation',
        'mpAddBrowsedPath',
        'mpDeleteFolder',
    ]
