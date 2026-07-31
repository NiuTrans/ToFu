"""Guards for the deep craft channel (epic pt_db5602172ac44b11 item ③).

The defect these lock down: ``WORKFLOW.md`` told the scene author to
"Activate hyperframes-motion", but ``activate_skill`` is a CHAT-agent tool and
the headless author's toolset is fixed — measured skill hits: **0**. The craft
corpus (29 rules + 13 blueprints + 13 frame presets) sat in a catalog entry
nobody had installed, so every film was authored from the ~20 KB distilled
guide alone.

What must stay true, and why each is a separate test:
  * the author can actually CALL the channel (tool registered + dispatched);
  * the author can SEE what to ask for (index travels in the prompt);
  * everything advertised RESOLVES — an index entry backed by no file is the
    same dead-instruction defect wearing a different hat;
  * usage is COUNTED — the original bug survived precisely because nothing
    measured whether the channel was reached.

**Why a synthetic corpus.** ``tests/conftest.py`` redirects ``TOFU_DATA_DIR``
to a temp dir, so the real fetched corpus is (correctly) invisible here. These
tests therefore build a miniature corpus with the same SHAPE — including one
deliberately dead index entry, mirroring the real upstream drift where
``rules-index.md`` advertises ``rules/kinetic-beat-slam.md`` and the file is
absent. That keeps every guard hermetic, offline and deterministic instead of
skipping on any host that has not fetched the packs.
"""

from __future__ import annotations

import re

import pytest

from lib.motion_video import _craft
from lib.motion_video._quality import film_quality_summary, scene_telemetry
from lib.motion_video._scene_author import SCENE_AUTHOR_TOOLS, _build_prompt

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """A miniature craft corpus with the real one's structure.

    Contains one resolvable rule, one blueprint, one design preset, and one
    index entry whose file does NOT exist — the dead entry the index assembler
    must filter out.
    """
    root = tmp_path / 'craft'
    motion = root / 'hyperframes-motion'
    (motion / 'rules').mkdir(parents=True)
    (motion / 'blueprints').mkdir(parents=True)
    (motion / 'rules' / 'alpha-rule.md').write_text(
        '# alpha-rule\nGSAP body for alpha.', encoding='utf-8')
    (motion / 'blueprints' / 'beta-blueprint.md').write_text(
        '# beta-blueprint\nMulti-phase body.', encoding='utf-8')
    (motion / 'rules-index.md').write_text(
        '# Rules Index\n<rules>\n'
        '<alpha-rule path="rules/alpha-rule.md">Real. Tags: text</alpha-rule>\n'
        '<ghost-rule path="rules/ghost-rule.md">Advertised but absent.'
        '</ghost-rule>\n</rules>\n', encoding='utf-8')
    (motion / 'blueprints-index.md').write_text(
        '# Scene Blueprints\n<blueprints>\n'
        '<beta-blueprint path="blueprints/beta-blueprint.md">A blueprint.'
        '</beta-blueprint>\n</blueprints>\n', encoding='utf-8')

    preset = root / 'hyperframes-design' / 'frame-presets' / 'sample-preset'
    preset.mkdir(parents=True)
    (preset / 'FRAME.md').write_text(
        '---\nversion: alpha\nname: Sample Preset\ndescription: >\n'
        '  A warm parchment ground with a single indigo ink.\n---\nBody.',
        encoding='utf-8')

    monkeypatch.setattr(_craft, 'craft_root', lambda: str(root))
    return root


# ── the channel exists and is reachable ───────────────────

def test_craft_reference_is_in_the_author_toolset():
    """The deep channel must be a real tool, not a documented aspiration."""
    names = [t['function']['name'] for t in SCENE_AUTHOR_TOOLS]
    assert 'craft_reference' in names, (
        'craft_reference missing from SCENE_AUTHOR_TOOLS — the author cannot '
        'reach the craft corpus, which is the exact defect this epic fixed')
    spec = next(t['function'] for t in SCENE_AUTHOR_TOOLS
                if t['function']['name'] == 'craft_reference')
    assert 'name' in spec['parameters']['properties']
    assert spec['parameters']['required'] == ['name']


def test_craft_reference_is_dispatched_by_the_author_loop():
    """A registered tool with no dispatch branch answers 'Unknown tool'."""
    import inspect

    from lib.motion_video import _scene_author
    from tests._source_scan import strip_comments
    src = strip_comments(inspect.getsource(_scene_author), lang='python')
    assert "name == 'craft_reference'" in src, (
        'craft_reference has no dispatch branch — the model would call it and '
        'be told the tool is unknown')


def test_workflow_guide_does_not_leave_a_dead_activate_instruction():
    """The guide may mention activate_skill only while naming the engine path.

    The original sentence pointed the author at a chat-only tool with no
    caveat. Rewriting it to cover both paths is the fix; silently deleting the
    mention would lose the (valid) chat-agent instruction.
    """
    # Markdown has no comment syntax to strip (charter #24 targets code scans).
    with open('lib/motion_video/guide/WORKFLOW.md', encoding='utf-8') as f:
        body = f.read()
    if 'activate_skill' not in body:
        return  # mention removed entirely — also acceptable
    assert 'craft_reference' in body, (
        'WORKFLOW.md still tells an author to activate a skill without '
        'documenting that the engine path uses craft_reference instead')


# ── the author can see what to ask for ────────────────────

def test_prompt_carries_the_craft_index(corpus):
    prompt = _build_prompt({'id': 'scene-001', 'text': '开场', 'visual': 'x'},
                           width=1080, height=1440, duration=5.0,
                           scene_index=1, total_scenes=6)
    assert 'Craft corpus' in prompt, (
        'the index does not travel with the prompt — a tool the model cannot '
        'enumerate is a tool it will not call')
    assert 'alpha-rule' in prompt
    assert 'beta-blueprint' in prompt, (
        'blueprints are missing — a stale `break` in the index loop starves '
        'the author of every multi-phase scene template')
    assert 'sample-preset' in prompt


def test_index_covers_all_three_bodies_of_knowledge(corpus):
    index = _craft.craft_index()
    assert 'rules-index' in index
    assert 'blueprints-index' in index
    assert 'frame presets' in index
    assert 'index truncated' not in index, (
        'a truncated index is a silently unreachable tail, not a smaller index')


# ── everything advertised must resolve ────────────────────

def test_every_advertised_entry_resolves_to_real_text(corpus):
    index = _craft.craft_index()
    names = [m.group(1) for line in index.splitlines()
             if (m := re.match(r'\s*<([a-z0-9][a-z0-9-]*) path="', line))]
    assert names, 'index advertises nothing'
    dead = [n for n in names
            if _craft.craft_reference(n).startswith(('No craft reference',
                                                     'Could not read'))]
    assert not dead, (
        f'advertised entries resolve to nothing: {dead} — pointing an author '
        f'at an unreachable rule is the same defect as the dead '
        f'activate_skill instruction')


def test_dead_index_entry_is_filtered_out(corpus):
    """Mirrors real upstream drift: an advertised rule with no file."""
    index = _craft.craft_index()
    assert 'alpha-rule' in index
    assert 'ghost-rule' not in index, (
        'an index entry backed by no file was advertised to the author')


def test_design_preset_resolves_by_its_directory_name(corpus):
    """Presets are advertised by folder but their text lives in FRAME.md."""
    body = _craft.craft_reference('sample-preset')
    assert body.startswith('# craft reference: hyperframes-design')
    assert 'Body.' in body


def test_design_index_carries_the_preset_description(corpus):
    """A bare name is not choosable — the folded frontmatter must survive."""
    assert 'warm parchment ground' in _craft.craft_index()


def test_traversal_and_misses_are_refused_with_an_honest_message(corpus):
    """A miss must NAME the problem: silence reads as 'this rule is empty'."""
    for probe in ('../../../etc/passwd', '/etc/passwd', 'no-such-rule', ''):
        out = _craft.craft_reference(probe)
        assert out.startswith(('No craft reference', 'Error')), \
            f'{probe!r} was not refused: {out[:80]}'


# ── usage is measured ─────────────────────────────────────

def test_scene_telemetry_records_craft_reads():
    rec = scene_telemetry({'id': 'scene-001'}, '<html></html>', '',
                          mode='authored', fill=None,
                          craft_reads=['alpha-rule'])
    assert rec['craft_reads'] == ['alpha-rule'], (
        'craft usage is not persisted per scene — the original bug survived '
        'because nothing counted whether the channel was reached')


def test_film_summary_rolls_up_craft_usage():
    summary = film_quality_summary([
        {'scene_id': 'scene-001', 'craft_reads': ['a', 'b']},
        {'scene_id': 'scene-002', 'craft_reads': []},
        {'scene_id': 'scene-003', 'craft_reads': ['b']},
    ])
    assert summary['scenes_using_craft'] == 2
    assert summary['craft_entries_read'] == ['a', 'b']


def test_engine_passes_craft_reads_into_telemetry():
    """The author returns craft_reads; the engine must actually forward it."""
    import inspect

    from lib.motion_video import engine
    from tests._source_scan import strip_comments
    src = strip_comments(inspect.getsource(engine), lang='python')
    assert 'craft_reads=author_craft_reads' in src, (
        'engine drops craft_reads before telemetry — job.json would report '
        'the channel unused on every film')


def test_craft_reads_survives_every_author_return_path():
    """Including the failure paths: a timed-out scene still read what it read."""
    import inspect

    from lib.motion_video import _scene_author
    from tests._source_scan import strip_comments
    src = strip_comments(inspect.getsource(_scene_author.author_scene),
                         lang='python')
    returns = src.count("'tokens': state['tokens']")
    carries = src.count("'craft_reads':")
    assert returns == carries, (
        f'{returns} return sites carry tokens but only {carries} carry '
        f'craft_reads — some paths would report the channel unused')


# ── degradation is never fatal ────────────────────────────

def test_absent_corpus_degrades_instead_of_raising(monkeypatch):
    """Offline hosts must still author, from the in-tree guide."""
    monkeypatch.setattr(_craft, 'craft_available', lambda: False)
    assert _craft.craft_index() == ''
    assert 'not available' in _craft.craft_reference('anything')
    prompt = _build_prompt({'id': 'scene-001', 'text': 't'},
                           width=1080, height=1440, duration=3.0,
                           scene_index=1, total_scenes=2)
    assert 'Composition contract' in prompt
    assert 'Craft corpus' not in prompt


def test_unreachable_network_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(_craft, 'craft_root', lambda: str(tmp_path))

    def _boom(*a, **k):
        raise OSError('network unreachable')

    monkeypatch.setattr('lib.http_client.http_get', _boom)
    assert _craft.ensure_craft_corpus() is False
