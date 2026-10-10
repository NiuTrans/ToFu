"""Research Foundry must continue through experiments and manuscript source."""

from __future__ import annotations

import json
from pathlib import Path


import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT / 'frontend/src/features/evolution/research/controllers/workspace.ts'
GATES = ROOT / 'frontend/src/features/evolution/research/components/submission-gates.ts'
VIEW = ROOT / 'frontend/src/features/evolution/research/components/view.ts'
API = ROOT / 'frontend/src/api/endpoint-registry/research.ts'
PANEL = ROOT / 'frontend/src/features/paper/panel-owners.ts'
# Research (rs-*/rsw-*) styles split out of 03-paper-reader.css (2026-09).
CSS = ROOT / 'frontend/src/styles/application/27-evolution-research.css'


def test_completed_research_mounts_the_production_workspace():
    view = VIEW.read_text(encoding='utf-8')
    panel = PANEL.read_text(encoding='utf-8')
    assert "researchWorkspaceHtml(stream)" in view
    assert "loadResearchWorkspace(stream.direction, stream.lang)" in view
    assert "import './research-workspace'" not in panel
    assert "../controllers/workspace" in view


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
    assert "get('/api/v4/research/workspace'" in api
    assert "put('/api/v4/research/workspace'" in api
    assert 'localStorage' not in src


def test_submission_gate_is_derived_from_evidence_and_compile_state():
    """Submission readiness is the SERVER's decision; the workspace renders it.

    The client no longer recomputes gates from run/claim/manuscript fields —
    the workspace API owns that verdict and ships it as a `readiness`
    projection. The contract here: the controller renders the server decision
    verbatim, keeps the local compile-state display, and invalidates the shown
    readiness the moment a local edit diverges from the saved revision.
    """
    src = WORKSPACE.read_text(encoding='utf-8')
    assert 'submissionGatesHtml(state.readiness, tr)' in src
    # A local source edit downgrades a live 'passing' compile back to not_run,
    # so the shown compile state can never outpace the saved server revision.
    assert "copy.compilation?.status === 'passing'" in src
    assert "copy.compilation.status = 'not_run'" in src
    assert 'state.readiness = null' in src
    # The old client-side gate expression must not come back.
    assert 'gates.every((gate) => gate.ok)' not in src

    gates = GATES.read_text(encoding='utf-8')
    # The renderer preserves every server gate verbatim (no client recompute).
    assert 'row.gates.every(gate =>' in gates


def test_latex_is_a_multifile_source_project_not_a_fake_local_compiler():
    src = WORKSPACE.read_text(encoding='utf-8')
    assert '\\\\documentclass[11pt]{article}' in src
    assert '\\\\section{Method}' in src
    assert 'source_files' in src
    assert 'scaffoldManuscript' in src
    assert 'sourceArchiveUrl' in src
    assert "['compile', tr('evolution.research.actionCompile'" in src
    assert "_startResearchAction('${action}',this)" in src
    assert 'Overleaf' not in src
    assert '/compile' not in src and 'compileResearch' not in src


def test_workspace_styles_are_scoped_and_responsive():
    css = CSS.read_text(encoding='utf-8')
    assert '.rsw-workspace' in css and '.rsw-output-grid' in css
    assert '@media(max-width:640px)' in css
    assert '.rsw-grid.cols-2,.rsw-grid.cols-3{grid-template-columns:1fr}' in css


def test_protocol_starts_from_a_grounded_draft_not_a_blank_form():
    """The last Auto Research step used to open with ~10 empty fields even
    though the pipeline had already paid for survey + gaps + ideas. The
    contract now: one click drafts the whole protocol from that evidence,
    and rarely-changed fields stay collapsed until asked for."""
    src = WORKSPACE.read_text(encoding='utf-8')
    api = API.read_text(encoding='utf-8')
    assert '_draftResearchProtocol(this)' in src
    assert "api.draftProtocol({ direction: state.direction, lang: state.lang, idea_id: ideaId })" in src
    assert "post('/api/v4/research/workspace/draft-protocol'" in api
    # Rarely-touched protocol fields live behind an opt-in disclosure.
    assert '<details class="rsw-advanced">' in src
    assert 'evolution.research.advancedProtocol' in src
    # Suggested capability bindings are adoptable in one click.
    assert '_autobindResearchCapabilities()' in src
    assert "notes: 'suggested'" in src


def test_every_workspace_translation_is_present_in_both_locales():
    src = WORKSPACE.read_text(encoding='utf-8')
    zh = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text(encoding='utf-8'))
    en = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text(encoding='utf-8'))
    import re
    keys = set(re.findall(r"tr\('([^']+)'", src))
    missing = sorted(key for key in keys if key not in zh or key not in en)
    assert not missing, f'missing en/zh keys: {missing}'
