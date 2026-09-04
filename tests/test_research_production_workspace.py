"""Research Foundry must continue through experiments and manuscript source."""

from __future__ import annotations

import json
from pathlib import Path


import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / 'frontend/src/features/paper/research-workspace.ts'
VIEW = ROOT / 'frontend/src/features/paper/research-view.ts'
API = ROOT / 'frontend/src/runtime/sections/api.js'
PANEL = ROOT / 'frontend/src/features/paper/panel-owners.ts'
CSS = ROOT / 'frontend/src/styles/application/03-paper-reader.css'


def test_completed_research_mounts_the_production_workspace():
    view = VIEW.read_text(encoding='utf-8')
    panel = PANEL.read_text(encoding='utf-8')
    assert "researchWorkspaceHtml(stream)" in view
    assert "loadResearchWorkspace(stream.direction, stream.lang)" in view
    assert "import './research-workspace'" in panel


def test_workspace_has_real_experiment_evidence_and_manuscript_controls():
    src = WORKSPACE.read_text(encoding='utf-8')
    for token in (
        'protocol.primary_metric', 'protocol.baseline', 'protocol.dataset',
        'protocol.falsifier', '_addResearchRun()', 'runs.${index}.artifact_ref',
        '_addResearchClaim()', 'claims.${index}.evidence_refs_csv',
        'manuscript.abstract', 'manuscript.method', 'manuscript.results',
        'manuscript.limitations', '_copyResearchLatex(this)',
        'capability_bindings', 'schema_hash', "tasks.start('research-action'",
        'figures', 'tables', 'visualData', 'source_files',
        '_scaffoldResearchManuscript(this)',
    ):
        assert token in src, f'missing production boundary: {token}'


def test_workspace_uses_versioned_backend_instead_of_browser_only_storage():
    src = WORKSPACE.read_text(encoding='utf-8')
    api = API.read_text(encoding='utf-8')
    assert 'expected_revision: workspace.revision' in src
    assert "get('/api/v1/research/workspace'" in api
    assert "put('/api/v1/research/workspace'" in api
    assert 'localStorage' not in src


def test_submission_gate_is_derived_from_evidence_and_compile_state():
    src = WORKSPACE.read_text(encoding='utf-8')
    assert "run.status === 'passed' && (run.artifact_ref || run.artifact_refs?.length)" in src
    assert "claim.status === 'supported'" in src
    assert 'workspace.source_files.length' in src
    assert "workspace.compilation?.status === 'passing'" in src
    assert 'gates.every((gate) => gate.ok)' in src


def test_latex_is_a_multifile_source_project_not_a_fake_local_compiler():
    src = WORKSPACE.read_text(encoding='utf-8')
    assert '\\\\documentclass[11pt]{article}' in src
    assert '\\\\section{Method}' in src
    assert 'source_files' in src
    assert 'scaffoldManuscript' in src
    assert 'sourceArchiveUrl' in src
    assert "['compile', tr('paper.research.actionCompile'" in src
    assert "_startResearchAction('${action}',this)" in src
    assert 'Overleaf' not in src
    assert '/compile' not in src and 'compileResearch' not in src


def test_workspace_styles_are_scoped_and_responsive():
    css = CSS.read_text(encoding='utf-8')
    assert '.rsw-workspace' in css and '.rsw-output-grid' in css
    assert '@media(max-width:640px)' in css
    assert '.rsw-grid.cols-2,.rsw-grid.cols-3{grid-template-columns:1fr}' in css


def test_every_workspace_translation_is_present_in_both_locales():
    src = WORKSPACE.read_text(encoding='utf-8')
    zh = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text(encoding='utf-8'))
    en = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text(encoding='utf-8'))
    import re
    keys = set(re.findall(r"tr\('([^']+)'", src))
    missing = sorted(key for key in keys if key not in zh or key not in en)
    assert not missing, f'missing en/zh keys: {missing}'
