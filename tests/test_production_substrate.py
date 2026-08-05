"""tests/test_production_substrate.py — P6 strangler relocation guard.

Covers the P6 step of docs/PRODUCTION_PIPELINE_DESIGN.md (board epic
pt_a22189455f754206): the stage-graph contract moved from
``lib/motion_video/_stages.py`` to ``lib/production/stages.py``.

The owner's hard constraint on this step was **relocation, not rewrite**
(strangler-fig). These tests pin exactly that:

  * every public symbol resolves to the SAME object through all three paths
    (new home / package facade / legacy shim) — a re-implementation would
    produce distinct objects and fail;
  * the legacy import path still works, so no caller had to change;
  * the substrate stays capability-agnostic: ``lib.production.stages`` must
    not import motion-video / audio / LLM modules, or the "horizontal layer"
    claim is false and the next recipe inherits video baggage;
  * behaviour is unchanged end-to-end (checkpointed resume still works when
    driven through the NEW path).
"""

from __future__ import annotations

import ast
import os

import pytest

pytestmark = pytest.mark.unit

_PUBLIC = ('Stage', 'StageAborted', 'StageFailed', 'run_stages',
           'load_state', 'stage_is_done', 'stage_artifact', 'STATE_VERSION')


def test_new_home_exports_the_full_contract():
    from lib.production import stages
    for name in _PUBLIC:
        assert hasattr(stages, name), name


def test_all_three_paths_are_the_same_objects():
    """Relocation, not re-implementation: identity must hold across paths."""
    import lib.motion_video._stages as legacy
    import lib.production as facade
    from lib.production import stages as home
    for name in _PUBLIC:
        a, b, c = getattr(home, name), getattr(facade, name), getattr(legacy, name)
        assert a is b is c, f'{name} diverged across import paths'


def test_legacy_import_path_still_works():
    """No caller had to change — the shim keeps the old path alive."""
    from lib.motion_video._stages import Stage, run_stages  # noqa: F401
    assert Stage.__module__ == 'lib.production.stages'


def test_substrate_is_capability_agnostic():
    """lib/production/stages.py must not import video/audio/LLM modules.

    The whole point of the substrate is that the NEXT recipe (podcast, PPT,
    long report) can ride it without inheriting motion-video baggage.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'lib', 'production', 'stages.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    banned = ('motion_video', 'tts', 'llm', 'paper', 'ffmpeg', 'audio')
    for mod in imported:
        for token in banned:
            assert token not in mod, f'substrate imports {mod!r} (banned: {token})'


def test_behaviour_unchanged_through_new_path(tmp_path):
    """The relocated runner still checkpoints + resumes identically."""
    from lib.production import Stage, run_stages, load_state, stage_is_done

    state_path = str(tmp_path / 'state.json')
    calls = []

    def boom(ctx):
        calls.append('b1')
        raise RuntimeError('crash')

    from lib.production import StageFailed
    with pytest.raises(StageFailed):
        run_stages([Stage('a', lambda c: calls.append('a1') or {'v': 1}),
                    Stage('b', boom)], {}, state_path=state_path)
    assert calls == ['a1', 'b1']
    assert stage_is_done(load_state(state_path), 'a')

    calls.clear()
    run_stages([Stage('a', lambda c: calls.append('a2') or {'v': 9}),
                Stage('b', lambda c: calls.append('b2') or {'v': 2})],
               {}, state_path=state_path)
    assert calls == ['b2']  # 'a' resumed from the checkpoint, not re-run


def test_recipe_uses_the_new_home_not_the_shim():
    """The live caller was repointed at the real home; the shim exists only
    for back-compat. If this ever regresses, the shim silently becomes load-
    bearing again and the strangler step is incomplete."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'lib', 'motion_video', '_recipe.py')
    src = open(path, encoding='utf-8').read()
    assert 'from lib.production.stages import' in src
    assert 'from lib.motion_video._stages import' not in src


@pytest.mark.skipif(
    not os.path.isfile(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'CLAUDE.md')),
    reason='CLAUDE.md not shipped in opensource (agent-rules doc, export-excluded)')
def test_claude_md_documents_the_new_packages():
    """CLAUDE.md's directory map is the agent-facing source of truth and its
    own rule says to re-scan it when a sub-package is added. Both packages
    this epic created must appear there, or the next agent works from a map
    that omits them (exactly the drift this test exists to stop)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, 'CLAUDE.md'), encoding='utf-8').read()
    assert 'production/' in src, 'lib/production/ missing from the CLAUDE.md map'
    assert 'motion_video/' in src, 'lib/motion_video/ missing from the CLAUDE.md map'
    assert '_scene_author.py' in src
    assert 'stages.py' in src


def test_design_note_status_matches_reality():
    """The design note must reflect what is actually in HEAD — it must not
    read as an un-started proposal, and it must keep naming the pieces that
    were deliberately NOT built so nobody assumes they exist.

    (Originally this also required an open 待拍板 item; that premise expired
    when P4–P7 all landed. What still matters is the honesty half: the note
    says what shipped AND what was deliberately scoped out.)"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, 'docs', 'PRODUCTION_PIPELINE_DESIGN.md'),
               encoding='utf-8').read()
    assert '已落地' in src, 'design note still reads as an un-started proposal'
    assert 'deliverable' in src, 'the deliberately-unextracted piece must stay named'
    assert '刻意不抽' in src or '刻意没抽' in src, (
        'the note must mark deliberate scoping, so a reader can tell it from '
        'an oversight')


def test_production_package_docstring_records_partial_scope():
    """The substrate is deliberately PARTIAL. The package must SAY which
    pieces are absent, so a later reader doesn't assume they exist or think
    their absence was an oversight.

    P6 extracted the two clusters the P7 measurement justified
    (ProductionRuntime + job manifest/resume); `deliverable` stayed out
    because the third sample didn't need it."""
    import lib.production as prod
    doc = prod.__doc__ or ''
    assert 'deliverable' in doc
    assert 'NOT here' in doc
