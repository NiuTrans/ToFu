"""Executable contract for the shared production substrate.

The package facade and owner modules expose one implementation, the substrate
remains capability-neutral, and stage checkpoints preserve recovery semantics.
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


def test_package_exports_the_owner_module_objects():
    import lib.production as facade
    from lib.production import stages as home
    for name in _PUBLIC:
        assert getattr(home, name) is getattr(facade, name)


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


def test_expired_or_versioned_checkpoint_reruns_the_dependency_suffix(
        tmp_path, monkeypatch):
    """Fresh research must never be combined with an old downstream deck."""
    from lib.production import stages as st

    now = {'value': 1000.0}
    monkeypatch.setattr(st.time, 'time', lambda: now['value'])
    state_path = str(tmp_path / 'state.json')
    calls = []

    def graph(label, revision='facts-v1'):
        return [
            st.Stage('research',
                     lambda c: calls.append(f'research-{label}') or {'v': label},
                     resume_ttl_s=10, checkpoint_version=revision),
            st.Stage('outline',
                     lambda c: calls.append(f'outline-{label}') or {
                         'from': c['artifacts']['research']['v']}),
        ]

    st.run_stages(graph('first'), {}, state_path=state_path)
    assert calls == ['research-first', 'outline-first']

    calls.clear()
    now['value'] = 1005.0
    out = st.run_stages(graph('fresh'), {}, state_path=state_path)
    assert calls == []
    assert out['outline']['from'] == 'first'

    calls.clear()
    now['value'] = 1020.0
    out = st.run_stages(graph('expired'), {}, state_path=state_path)
    assert calls == ['research-expired', 'outline-expired']
    assert out['outline']['from'] == 'expired'

    calls.clear()
    now['value'] = 1021.0
    out = st.run_stages(graph('revision', 'facts-v2'), {},
                        state_path=state_path)
    assert calls == ['research-revision', 'outline-revision']
    assert out['outline']['from'] == 'revision'


def test_recipe_imports_the_shared_stage_owner():
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
    """The short agent entry point must lead to the authoritative domain map."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    claude = open(os.path.join(root, 'CLAUDE.md'), encoding='utf-8').read()
    docs_map = open(os.path.join(root, 'docs', 'README.md'),
                    encoding='utf-8').read()
    production = open(os.path.join(root, 'docs', 'modules', 'production.md'),
                      encoding='utf-8').read()
    assert '`docs/README.md`' in claude
    assert '`lib/production/`' in docs_map
    assert 'modules/production.md' in docs_map
    assert '`lib/production/stages.py`' in production
    assert '`lib/motion_video/`' in production
