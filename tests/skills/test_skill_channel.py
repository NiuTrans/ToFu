"""tests/skills/test_skill_channel.py — The skills channel (board epic pt_229606ca P2).

Covers the model-facing skills channel end to end:

  * ``build_skills_index``   — the always-visible <available_skills> block
    (format, byte-stability, disabled/ineligible exclusion)
  * ``load_skill``           — exact-id progressive-disclosure loader
    (body + bounded file sample, honest unknown/disabled/ineligible)
  * the ``compose_task_context`` splice seam — gated on has_real_tools
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
               requires_bins=None, with_script=False, with_references=True,
               body='guide body', allow_implicit=None, requires_tools=None,
               requires_tools_meta=None, openai_yaml_implicit=None):
    import json as _json
    pkg_dir = os.path.join(dirpath, pkg_id)
    os.makedirs(pkg_dir, exist_ok=True)
    if with_references:
        os.makedirs(os.path.join(pkg_dir, 'references'), exist_ok=True)
    lines = ['---', f'name: {pkg_id}',
             f'description: {description or f"guide for {pkg_id} tasks"}']
    if enabled is not None:
        lines.append(f'enabled: {str(enabled).lower()}')
    if allow_implicit is not None:
        lines.append(
            f'allow_implicit_invocation: {str(allow_implicit).lower()}')
    if requires_tools:
        lines.append(
            'requires_tools: [' + ', '.join(requires_tools) + ']')
    if requires_bins or requires_tools_meta:
        # House OpenClaw format: metadata as a JSON object (see
        # _parse_frontmatter Case B) — NOT YAML block style.
        requires = {}
        if requires_bins:
            requires['bins'] = list(requires_bins)
        if requires_tools_meta:
            requires['tools'] = list(requires_tools_meta)
        md = {'openclaw': {'requires': requires}}
        lines.append(f'metadata: {_json.dumps(md)}')
    lines.append('---')
    lines.append('')
    lines.append(body)
    with open(os.path.join(pkg_dir, 'SKILL.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    if openai_yaml_implicit is not None:
        agents_dir = os.path.join(pkg_dir, 'agents')
        os.makedirs(agents_dir, exist_ok=True)
        with open(os.path.join(agents_dir, 'openai.yaml'), 'w',
                  encoding='utf-8') as f:
            f.write('policy:\n  allow_implicit_invocation: '
                    f'{str(openai_yaml_implicit).lower()}\n')
    if with_references:
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
    parts = []
    for message in messages:
        content = message.get('content')
        if isinstance(content, list):
            parts.extend(b.get('text', '') for b in content
                         if isinstance(b, dict))
        elif content:
            parts.append(str(content))
    return '\n\n'.join(parts)


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
    assert 'do not invent skill:// paths' in block
    assert 'workspace files' in block
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
    assert 'base_directory:' not in out
    assert os.path.abspath(pkg_dir) not in out
    assert 'content_hash:' in out
    assert '<skill_instructions>\nTHE GUIDE\n</skill_instructions>' in out
    # Bounded sample uses opaque, owner-resolved resource paths.
    assert 'skill://mypkg/references/ref.md' in out
    assert 'skill://mypkg/scripts/run.py' in out
    assert 'instruction-only' not in out


@pytest.mark.unit
def test_load_returns_the_whole_guide_without_paging(isolated):
    """A guide longer than the old 6k page is disclosed in full; overflow
    paging is the artifact layer's job, not the loader's."""
    from lib.skills import load_skill
    proj = _proj(isolated)
    body = '\n'.join(f'line {i:04d} of the guide' for i in range(600))
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'longguide', body=body)

    out = load_skill('longguide', project_path=proj)
    assert 'line 0599 of the guide' in out, 'guide tail was cut'
    assert 'read_skill_resource' not in out.split(
        '<skill_instructions>')[1].split('</skill_instructions>')[0]
    assert 'instructions continue' not in out
    assert 'content_hash:' in out


@pytest.mark.unit
def test_skills_index_marks_explicit_only_and_shadowed(isolated):
    from lib.skills import build_skills_index
    proj = _proj(isolated)
    root = os.path.join(proj, '.tofu', 'skills')
    _write_pkg(root, 'quiet', allow_implicit=False)
    _write_pkg(root, 'shared')
    _write_pkg(str(isolated / 'data' / 'skills' / 'global'), 'shared')

    block = build_skills_index(project_path=proj)
    assert '- quiet (project) explicit-only: guide for quiet tasks' in block
    assert '- shared (project): guide for shared tasks' in block
    # Codex-style trigger rules in the header.
    assert 'must use that guide' in block
    assert 'Do not carry a guide' in block
    assert 'explicit-only activate' in block
    # The surviving project package names its masked same-id duplicate.
    assert 'shared shadows same-id package(s)' in block
    # Byte-stable with the new annotations (prompt-cache contract).
    assert build_skills_index(project_path=proj) == block


@pytest.mark.unit
def test_openai_yaml_policy_marks_explicit_only(isolated):
    from lib.skills import build_skills_index, get_skill
    proj = _proj(isolated)
    root = os.path.join(proj, '.tofu', 'skills')
    _write_pkg(root, 'codex-style', openai_yaml_implicit=False)
    # Top-level frontmatter wins over the sidecar policy.
    _write_pkg(root, 'override', allow_implicit=True,
               openai_yaml_implicit=False)

    assert get_skill(
        'codex-style', project_path=proj)['allow_implicit_invocation'] is False
    assert get_skill(
        'override', project_path=proj)['allow_implicit_invocation'] is True
    block = build_skills_index(project_path=proj)
    assert '- codex-style (project) explicit-only:' in block
    assert '- override (project):' in block


@pytest.mark.unit
def test_load_skill_reports_declared_tool_requirements(isolated):
    from lib.skills import get_skill, load_skill
    proj = _proj(isolated)
    root = os.path.join(proj, '.tofu', 'skills')
    _write_pkg(root, 'tools-guide',
               requires_tools=['read_files', 'no-such-tool-xyz123'])
    _write_pkg(root, 'meta-tools', requires_tools_meta=['run_command'])

    assert get_skill(
        'meta-tools', project_path=proj)['requires_tools'] == ['run_command']
    out = load_skill('tools-guide', project_path=proj)
    assert 'Declared tool requirements' in out
    assert '- read_files: available' in out
    assert '- no-such-tool-xyz123: unavailable' in out
    plain = load_skill('meta-tools', project_path=proj)
    assert '- run_command: available' in plain


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


@pytest.mark.unit
def test_load_instruction_only_and_missing_resource_are_recoverable(isolated):
    from lib.skills import load_skill, read_skill_resource
    proj = _proj(isolated)
    pkg = _write_pkg(
        os.path.join(proj, '.tofu', 'skills'), 'guide',
        with_references=False, body='Use docs/README.md')

    loaded = load_skill('guide', project_path=proj)
    assert 'Use docs/README.md' in loaded
    assert 'Supporting files: none.' in loaded
    assert 'instruction-only' in loaded
    assert 'ordinary file tools' in loaded
    assert os.path.abspath(pkg) not in loaded

    missing = read_skill_resource(
        'guide', 'skill://guide/docs.md', project_path=proj)
    assert missing.startswith(
        'Skill resource not found: skill://guide/docs.md')
    assert 'instruction-only' in missing
    assert 'ordinary file tools' in missing
    assert os.path.abspath(pkg) not in missing


# ── compose_task_context splice seam ──────────────────────────────

def _run_inject(messages, *, has_real_tools=True, memory_enabled=False,
                project_path=None, project_enabled=False):
    from lib.tasks_pkg.context_composer import compose_task_context
    compose_task_context(
        messages,
        user_id=0,
        project_path=project_path or '/tmp/x',
        project_enabled=project_enabled,
        memory_enabled=memory_enabled,
        search_enabled=False,
        has_real_tools=has_real_tools,
        tool_names={'load_skill'} if has_real_tools else set(),
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

    assert twice[0]['content'] == 'Base.'
    carriers = [message for message in twice
                if message.get('_contextComposer')]
    assert carriers
    assert sum('<available_skills>' in str(row['content'])
               for row in carriers) == 1
    assert all('Base.' not in str(row['content']) for row in carriers)


# ── tool registration + display wiring ───────────────────────────────

@pytest.mark.unit
def test_tool_registration_surface(isolated):
    from types import SimpleNamespace
    from lib.tools.registry._build import _build_skill_install, _build_skills

    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')
    ctx = SimpleNamespace(
        lean=False, has_base_tools=True, cfg={'projectPaths': [proj]},
        project_path=proj, project_enabled=True, tid='test')
    on = _build_skills(ctx)
    assert [t['function']['name'] for t in on] == [
        'search_skills', 'load_skill', 'read_skill_resource',
    ]
    assert [t['function']['name'] for t in _build_skill_install(ctx)] == [
        'request_skill_install']

    # No base tools / lean mode → no skill surface. Zero installed packages
    # still gets catalog discovery, so installation can be requested.
    assert _build_skills(
        SimpleNamespace(lean=False, has_base_tools=False)) == []
    assert _build_skills(
        SimpleNamespace(lean=True, has_base_tools=True)) == []
    assert _build_skill_install(
        SimpleNamespace(lean=True, has_base_tools=True)) == []

    # Declared idempotent (read-only) via the ToolSpec registry.
    from lib.tools.registry import all_specs
    spec = next(s for s in all_specs() if s.key == 'skills')
    assert 'load_skill' in spec.idempotent_tools
    assert 'search_skills' in spec.idempotent_tools
    assert 'load_skill' in spec.provides
    assert 'activate_skill' not in spec.provides
    assert spec.write_tools == frozenset()
    install_spec = next(s for s in all_specs() if s.key == 'skill_install')
    assert install_spec.discovery_policy == 'searchable'
    assert install_spec.write_tools == frozenset({'request_skill_install'})
    assert install_spec.confirmation_tools == frozenset({
        'request_skill_install'})


@pytest.mark.unit
def test_display_wiring(isolated):
    from lib.tasks_pkg.tool_display._dispatch import (
        _TOOL_DISPLAY_DISPATCH, tool_round_label)
    from lib.tasks_pkg.tool_dispatch._labels import tool_label

    assert 'load_skill' in _TOOL_DISPLAY_DISPATCH
    assert 'search_skills' in _TOOL_DISPLAY_DISPATCH
    assert 'read_skill_resource' in _TOOL_DISPLAY_DISPATCH
    assert 'request_skill_install' in _TOOL_DISPLAY_DISPATCH
    assert tool_round_label('load_skill', {'skill_id': 'mypkg'}) == \
        'Loaded skill: mypkg'
    assert tool_label('load_skill') == 'Loading skill'
    assert tool_label('request_skill_install') == 'Installing skill'
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
    task = {'id': 'skill-test-task', '_userId': 1,
            'toolRounds': [], 'events': [],
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
