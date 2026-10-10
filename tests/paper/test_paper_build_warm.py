"""Warm incremental compile state: reuse, cold-retry resilience, spool staging."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from lib.paper.builds import compiler
from lib.paper.builds.budget import BuildBudget
from lib.paper.builds.cache import LocalBuildCache, default_spool_root
from lib.paper.builds.contract import limit
from lib.paper.builds.sources import snapshot_sources

# serial: warm/cold retry state timing under load — cannot safely overlap the parallel unit lane
# (TESTING_STRATEGY serial policy).
pytestmark = [pytest.mark.unit, pytest.mark.serial]
_DOC = ('\\documentclass{article}\n\\begin{document}\n'
        'A citation \\cite{example} and section \\ref{sec:a}.\n'
        '\\section{A}\\label{sec:a}Body text.\n'
        '\\bibliographystyle{plain}\n\\bibliography{refs}\n\\end{document}\n')
_BIB = '@article{example,author={A. Author},title={Evidence},journal={Journal},year={2026}}'


def _require_tex():
    if not shutil.which('latexmk') and not (Path(__file__).resolve().parents[2] / '.texlive/bin').is_dir():
        pytest.skip('TeX toolchain unavailable')


def _document_tree(tmp_path, text=_DOC):
    root = tmp_path / 'source'
    root.mkdir(parents=True, exist_ok=True)
    (root / 'main.tex').write_text(text)
    (root / 'refs.bib').write_text(_BIB)
    return root


def _compile(snapshot, directory, **kwargs):
    return compiler.compile_snapshot(snapshot, directory, owner_user_id=7,
                                     document_id='a' * 64, build_id=uuid.uuid4().hex, **kwargs)


def _pdflatex_runs(build_directory: Path) -> int:
    log = (build_directory / 'build.log').read_text(errors='replace')
    return log.count("of rule 'pdflatex'")


def test_warm_state_eliminates_redundant_passes(tmp_path):
    _require_tex()
    root = _document_tree(tmp_path)
    snapshot = snapshot_sources(root, root / 'main.tex', tmp_path / 'snapshot')
    warm = tmp_path / 'warm'
    first = _compile(snapshot, tmp_path / 'build1', warm_dir=warm)
    assert first.receipt['status'] == 'success', first.receipt
    out = warm / 'output'
    assert _pdflatex_runs(tmp_path / 'build1') >= 2  # bibliography resolves over several passes
    assert (out / 'main.aux').is_file()
    assert (out / 'main.bbl').is_file()
    assert (out / 'main.fdb_latexmk').is_file()
    assert (out / 'main.pdf').is_file()  # the target stays so latexmk can prove up-to-date

    # Identical sources: the seeded latexmk database makes every pass redundant.
    second = _compile(snapshot, tmp_path / 'build2', warm_dir=warm)
    assert second.receipt['status'] == 'success', second.receipt
    assert _pdflatex_runs(tmp_path / 'build2') == 0

    # Changed sources: warm intermediates still save passes against a cold build.
    updated = _document_tree(tmp_path / 'v2', _DOC.replace('Body text.', 'Revised body text.'))
    moved = snapshot_sources(updated, updated / 'main.tex', tmp_path / 'v2' / 'snapshot')
    third = _compile(moved, tmp_path / 'v2' / 'build', warm_dir=warm)
    assert third.receipt['status'] == 'success', third.receipt
    assert 0 < _pdflatex_runs(tmp_path / 'v2' / 'build') < _pdflatex_runs(tmp_path / 'build1')


def test_corrupt_warm_state_falls_back_to_cold_retry(tmp_path):
    _require_tex()
    root = _document_tree(tmp_path)
    snapshot = snapshot_sources(root, root / 'main.tex', tmp_path / 'snapshot')
    warm = tmp_path / 'warm'
    out = warm / 'output'
    out.mkdir(parents=True)
    (out / 'main.aux').write_bytes(b'\x00\x01 not an aux file \\broken{')
    result = _compile(snapshot, tmp_path / 'build', warm_dir=warm)
    assert result.receipt['status'] == 'success', result.receipt
    assert (tmp_path / 'build' / 'document.pdf').is_file()
    # The successful cold retry replaces the poisoned state with valid files.
    assert not (out / 'main.aux').read_bytes().startswith(b'\x00\x01')


def test_oversized_warm_output_runs_cold(tmp_path):
    _require_tex()
    root = _document_tree(tmp_path)
    snapshot = snapshot_sources(root, root / 'main.tex', tmp_path / 'snapshot')
    warm = tmp_path / 'warm'
    out = warm / 'output'
    out.mkdir(parents=True)
    oversized = out / 'bloated.aux'
    oversized.write_bytes(b'x' * (limit('warm_state_bytes') + 1))
    result = _compile(snapshot, tmp_path / 'build', warm_dir=warm)
    assert result.receipt['status'] == 'success', result.receipt
    # State over the byte cap is wiped before the run, so this build ran cold.
    assert not oversized.exists()
    assert _pdflatex_runs(tmp_path / 'build') >= 2


def test_warm_lease_contention_degrades_to_cold_build(tmp_path):
    budget = BuildBudget(1, 4, 16384, 4)
    first = LocalBuildCache(tmp_path / 'cache', budget, spool_root=tmp_path / 'spool')
    second = LocalBuildCache(tmp_path / 'cache', budget, spool_root=tmp_path / 'spool')
    with first.warm_lease(7, 'b' * 64) as warm:
        assert warm is not None
        with second.warm_lease(7, 'b' * 64) as blocked:
            assert blocked is None
    with second.warm_lease(7, 'b' * 64) as warm_after:
        assert warm_after is not None


def test_stage_uses_spool_and_reclaims_spool_orphans(tmp_path):
    cache = LocalBuildCache(tmp_path / 'cache', BuildBudget(1, 4, 16384, 4),
                            spool_root=tmp_path / 'spool')
    with cache.stage(7, 'c' * 64, 'a' * 32) as stage:
        assert stage.is_relative_to(tmp_path / 'spool')
        orphan = stage.parent / ('d' * 32 + '.work')
        orphan.mkdir()
        (orphan / '.lease').touch()
    assert not stage.exists()
    with cache.stage(7, 'c' * 64, 'e' * 32):
        pass  # admission prunes the abandoned sibling work directory
    assert not orphan.exists()


def test_default_spool_root_prefers_override_and_falls_back(tmp_path, monkeypatch):
    override = tmp_path / 'fast'
    monkeypatch.setenv('TOFU_PAPER_BUILD_SPOOL_DIR', str(override))
    assert default_spool_root(tmp_path / 'cache') == override
    blocker = tmp_path / 'blocker'
    blocker.write_bytes(b'not a directory')
    monkeypatch.setenv('TOFU_PAPER_BUILD_SPOOL_DIR', str(blocker / 'child'))
    resolved = default_spool_root(tmp_path / 'cache')
    assert resolved != blocker / 'child'
    assert resolved.is_dir()
