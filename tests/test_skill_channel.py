"""tests/test_skill_channel.py — The skills channel (board epic pt_229606ca P2).

Covers the model-facing skills channel end to end:

  * ``build_skills_index``   — the always-visible <available_skills> block
    (format, byte-stability, disabled/ineligible exclusion)
  * ``load_skill``           — exact-id progressive-disclosure loader
    (body + bounded file sample, honest unknown/disabled/ineligible)
  * the ``_inject_system_contexts`` splice seam — gated on has_real_tools
    ONLY (independent of the memory toggle), idempotent, own cache block
  * tool registration (``_build_skills``) + display wiring (label /
    renderer / dispatch table) + the executor handler
"""

import os

import pytest

import lib.memory.storage as storage
import lib.memory.storage._dirs as dirs


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Redirect the server data dir to tmp + clear migration latches."""
    data_dir = tmp_path / 'data'
    monkeypatch.setattr(dirs, '_server_data_dir', lambda: str(data_dir))
    dirs._migrated_roots.clear()
    dirs._server_store_migrated = False
    yield tmp_path
    dirs._migrated_roots.clear()
    dirs._server_store_migrated = False


def _write_pkg(dirpath, pkg_id, *, description=None, enabled=None,
               requires_bins=None, with_script=False, body='guide body'):
    import json as _json
    pkg_dir = os.path.join(dirpath, pkg_id)
    os.makedirs(os.path.join(pkg_dir, 'references'), exist_ok=True)
    lines = ['---', f'name: {pkg_id}',
             f'description: {description or f"guide for {pkg_id} tasks"}']
    if enabled is not None:
        lines.append(f'enabled: {str(enabled).lower()}')
    if requires_bins:
        # House OpenClaw format: metadata as a JSON object (see
        # _parse_frontmatter Case B) — NOT YAML block style.
        md = {'openclaw': {'requires': {'bins': list(requires_bins)}}}
        lines.append(f'metadata: {_json.dumps(md)}')
    lines.append('---')
    lines.append('')
    lines.append(body)
    with open(os.path.join(pkg_dir, 'SKILL.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    with open(os.path.join(pkg_dir, 'references', 'ref.md'), 'w',
              encoding='utf-8') as f:
        f.write('reference doc\n')
    if with_script:
        os.makedirs(os.path.join(pkg_dir, 'scripts'), exist_ok=True)
        with open(os.path.join(pkg_dir, 'scripts', 'run.py'), 'w',
                  encoding='utf-8') as f:
            f.write('print("hi")\n')
    return pkg_dir


def _proj(tmp_path, name='proj'):
    p = tmp_path / name
    (p / '.tofu' / 'skills').mkdir(parents=True, exist_ok=True)
    return str(p)


def _system_full_text(messages):
    content = messages[0]['content']
    if isinstance(content, list):
        return '\n\n'.join(b.get('text', '') for b in content
                           if isinstance(b, dict))
    return content or ''


# ── build_skills_index ───────────────────────────────────────────────

@pytest.mark.unit
def test_skills_index_lists_sorted_and_byte_stable(isolated):
    from lib.skills import build_skills_index
    proj = _proj(isolated)
    root = os.path.join(proj, '.tofu', 'skills')
    _write_pkg(root, 'zeta')
    _write_pkg(root, 'alpha')
    _write_pkg(str(isolated / 'data' / 'skills' / 'global'), 'gpkg')

    block = build_skills_index(project_path=proj)
    assert block.startswith('<available_skills>')
    assert block.endswith('</available_skills>')
    assert '- alpha (project): guide for alpha tasks' in block
    assert '- zeta (project): guide for zeta tasks' in block
    assert '- gpkg (global): guide for gpkg tasks' in block
    # Sorted by id: alpha before gpkg before zeta.
    assert block.index('alpha') < block.index('gpkg') < block.index('zeta')
    # Byte-stable: identical bytes on a rebuild (prompt-cache contract).
    assert build_skills_index(project_path=proj) == block


@pytest.mark.unit
def test_skills_index_empty_string_when_none_installed(isolated):
    from lib.skills import build_skills_index
    proj = _proj(isolated)
    assert build_skills_index(project_path=proj) == ''


@pytest.mark.unit
def test_skills_index_excludes_disabled_and_ineligible(isolated):
    from lib.skills import build_skills_index
    proj = _proj(isolated)
    root = os.path.join(proj, '.tofu', 'skills')
    _write_pkg(root, 'good')
    _write_pkg(root, 'off', enabled=False)
    _write_pkg(root, 'needsbin',
               requires_bins=['definitely-not-a-real-bin-xyz123'])

    block = build_skills_index(project_path=proj)
    assert '- good (project):' in block
    assert 'off' not in block
    assert 'needsbin' not in block
    # Hidden count is surfaced so the user can debug "why isn't it offered".
    assert '2 installed skills are hidden' in block


# ── load_skill ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_load_returns_instructions_and_manifest(isolated):
    from lib.skills import load_skill
    proj = _proj(isolated)
    pkg_dir = _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg',
                         with_script=True, body='THE GUIDE')

    out = load_skill('mypkg', project_path=proj)
    assert out.startswith('Skill loaded: **mypkg**')
    assert f'base_directory: {os.path.abspath(pkg_dir)}' in out
    assert 'content_hash:' in out
    assert '<skill_instructions>\nTHE GUIDE\n</skill_instructions>' in out
    # Bounded sample uses absolute, directly-readable paths.
    assert os.path.join(pkg_dir, 'references', 'ref.md') in out
    assert os.path.join(pkg_dir, 'scripts', 'run.py') in out


@pytest.mark.unit
def test_load_requires_exact_id(isolated):
    from lib.skills import load_skill
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')
    assert load_skill('MYPKG', project_path=proj).startswith(
        "Skill not found: 'MYPKG'")


@pytest.mark.unit
def test_load_unknown_lists_available(isolated):
    from lib.skills import load_skill
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')

    out = load_skill('nope', project_path=proj)
    assert out.startswith("Skill not found: 'nope'")
    assert 'Available skill IDs: mypkg' in out


@pytest.mark.unit
def test_load_disabled_reports_without_leaking(isolated):
    from lib.skills import load_skill
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'off', enabled=False,
               body='SECRET GUIDE')

    out = load_skill('off', project_path=proj)
    assert 'disabled' in out
    assert 'SECRET GUIDE' not in out


@pytest.mark.unit
def test_load_ineligible_reports_reason(isolated):
    from lib.skills import load_skill
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'needsbin',
               requires_bins=['definitely-not-a-real-bin-xyz123'],
               body='SECRET GUIDE')

    out = load_skill('needsbin', project_path=proj)
    assert 'unavailable' in out
    assert 'definitely-not-a-real-bin-xyz123' in out
    assert 'SECRET GUIDE' not in out


# ── _inject_system_contexts splice seam ──────────────────────────────

def _run_inject(messages, *, has_real_tools=True, memory_enabled=False,
                project_path=None, project_enabled=False):
    from lib.tasks_pkg.system_context import _inject_system_contexts
    _inject_system_contexts(
        messages,
        project_path=project_path or '/tmp/x',
        project_enabled=project_enabled,
        memory_enabled=memory_enabled,
        search_enabled=False, swarm_enabled=False,
        has_real_tools=has_real_tools,
        conv_id='', task={'config': {}}, model='claude-opus-4',
    )


@pytest.mark.unit
def test_injection_splices_index_independent_of_memory_toggle(isolated):
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')

    messages = [{'role': 'system', 'content': 'Base.'}]
    # memory_enabled=False on purpose: the skills channel does NOT follow
    # the memory toggle.
    _run_inject(messages, project_path=proj, project_enabled=True,
                memory_enabled=False)
    full = _system_full_text(messages)
    assert '<available_skills>' in full
    assert '- mypkg (project):' in full


@pytest.mark.unit
def test_injection_skills_gating_and_idempotency(isolated):
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')

    # has_real_tools=False → no index (no load_skill tool either).
    no_tools = [{'role': 'system', 'content': 'Base.'}]
    _run_inject(no_tools, has_real_tools=False, project_path=proj,
                project_enabled=True)
    assert '<available_skills>' not in _system_full_text(no_tools)

    # No skills installed → no block at all (byte-stability: absent, not empty).
    empty_proj = _proj(isolated, name='empty_proj')
    empty_msgs = [{'role': 'system', 'content': 'Base.'}]
    _run_inject(empty_msgs, project_path=empty_proj, project_enabled=True)
    assert 'available_skills' not in _system_full_text(empty_msgs)

    # Idempotent: a second injection does not duplicate the block.
    twice = [{'role': 'system', 'content': 'Base.'}]
    _run_inject(twice, project_path=proj, project_enabled=True)
    _run_inject(twice, project_path=proj, project_enabled=True)
    assert _system_full_text(twice).count('<available_skills>') == 1

    # Own cache block: the index rides its OWN content block (a skill
    # install must not invalidate the static prefix's breakpoint).
    content = twice[0]['content']
    assert isinstance(content, list)
    block_texts = [b.get('text', '') for b in content if isinstance(b, dict)]
    idx_blocks = [t for t in block_texts if '<available_skills>' in t]
    assert len(idx_blocks) == 1
    assert 'Base.' not in idx_blocks[0]


# ── tool registration + display wiring ───────────────────────────────

@pytest.mark.unit
def test_tool_registration_surface(isolated):
    from types import SimpleNamespace
    from lib.tools.registry._build import _build_skills

    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')
    ctx = SimpleNamespace(
        lean=False, has_base_tools=True, cfg={'projectPaths': [proj]},
        project_path=proj, project_enabled=True, tid='test')
    on = _build_skills(ctx)
    assert [t['function']['name'] for t in on] == ['load_skill']

    # No base tools or no eligible installed skills → no loader schema.
    assert _build_skills(
        SimpleNamespace(lean=False, has_base_tools=False)) == []
    assert _build_skills(
        SimpleNamespace(lean=True, has_base_tools=True)) == []

    # Declared idempotent (read-only) via the ToolSpec registry.
    from lib.tools import all_specs
    spec = next(s for s in all_specs() if s.key == 'skills')
    assert 'load_skill' in spec.idempotent_tools
    assert 'load_skill' in spec.provides
    assert 'activate_skill' not in spec.provides
    assert not spec.write_tools


@pytest.mark.unit
def test_display_wiring(isolated):
    from lib.tasks_pkg.tool_display._dispatch import (
        _TOOL_DISPLAY_DISPATCH, tool_round_label)
    from lib.tasks_pkg.tool_dispatch._labels import tool_label

    assert 'load_skill' in _TOOL_DISPLAY_DISPATCH
    assert tool_round_label('load_skill', {'skill_id': 'mypkg'}) == \
        'Loaded skill: mypkg'
    assert tool_label('load_skill') == 'Loading skill'
    # Display-only history shim; this name has no schema or handler.
    assert tool_round_label('activate_skill', {'skill': 'mypkg'}) == \
        'Previously activated skill: mypkg'


@pytest.mark.unit
def test_handler_registered_via_handlers_package(isolated):
    """Pins the import chain: importing lib.tasks_pkg.executor triggers the
    handlers package import, which must register load_skill — a missing
    'skills' line in handlers/__init__.py flips this red."""
    import lib.tasks_pkg.executor as executor
    handler = executor.tool_registry.lookup('load_skill')
    assert handler is not None
    assert handler.__module__ == 'lib.tasks_pkg.handlers.skills'
    assert executor.tool_registry.lookup('activate_skill') is None


@pytest.mark.unit
def test_handler_end_to_end(isolated):
    from lib.tasks_pkg.handlers.skills import _handle_skill_tool
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')

    import threading
    # _suppressEvents: the same isolation swarm sub-agents use — keeps
    # append_event (SSE push + durable event-log row) out of this unit test.
    task = {'id': 'skill-test-task', 'toolRounds': [], 'events': [],
            'events_lock': threading.Lock(), '_suppressEvents': True}
    round_entry = {'query': 'Loaded skill: mypkg'}
    tc_id, content, is_search = _handle_skill_tool(
        task, None, 'load_skill', 'tc1', {'skill_id': 'mypkg'},
        0, round_entry,
        {'projectPaths': [proj]}, proj, True)
    assert tc_id == 'tc1'
    assert content.startswith('Skill loaded: **mypkg**')
    assert is_search is False
    # The round was finalized with display metadata (badge + status).
    assert round_entry['status'] == 'done'
    metas = round_entry.get('results') or []
    assert metas and metas[0].get('badge') == '📦 loaded'
