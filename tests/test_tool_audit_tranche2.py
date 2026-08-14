"""Drift-guard tests for the 2026-07-11 tool-surface audit tranche 2.

Each test asserts the FIX is present AND (where meaningful) that a neutered
variant of the source would fail — the guard bites when the fix regresses.
Wiring/structure checks, following the project's verify-then-write discipline.

Fixes covered:
  1. spawn_agents role catalogue only advertises MANUALLY-SPAWNABLE roles.
     The 4 endpoint/autopilot-internal roles (planner/worker/critic/
     virtual_user) are used by get_role_config for endpoint mode but must NOT
     appear in format_role_catalogue() — the spawn_agents `role` param text
     lists 7 roles, so dumping 11 in the catalogue above it is self-
     contradictory and invites the model to spawn a role whose prompt makes no
     sense as a standalone sub-agent.
  2. update_memory description no longer references a non-existent
     `list_all_memories` TOOL (it's an internal storage fn, not in
     MEMORY_TOOL_NAMES).
  3. system_prompt_cc.py tool-substitution comments are accurate: todo_write
     IS ported; the interactive tool is `ask_human`, not `ask_user`.
"""

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/tasks_pkg/system_prompt_cc.py'}

import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_INTERNAL_ROLES = ('planner', 'worker', 'critic', 'virtual_user')
_SPAWNABLE_ROLES = ('researcher', 'coder', 'analyst', 'browser',
                    'reviewer', 'writer', 'general')


def _src(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as f:
        return f.read()


# ── 1. role catalogue lists only manually-spawnable roles ───────────

def test_role_catalogue_excludes_internal_roles():
    from lib.swarm.registry import format_role_catalogue
    cat = format_role_catalogue()
    for role in _INTERNAL_ROLES:
        assert f'- {role}:' not in cat, (
            f'{role!r} is endpoint/autopilot-internal and must not be offered '
            f'as a manually-spawnable role in the catalogue')


def test_role_catalogue_keeps_spawnable_roles():
    from lib.swarm.registry import format_role_catalogue
    cat = format_role_catalogue()
    for role in _SPAWNABLE_ROLES:
        assert f'- {role}:' in cat, (
            f'{role!r} must remain in the manual-spawn catalogue')


def test_internal_roles_still_resolvable_for_endpoint_mode():
    # The filter is catalogue-only: endpoint mode still needs the configs.
    from lib.swarm.registry import AGENT_ROLES, get_role_config
    for role in _INTERNAL_ROLES:
        assert role in AGENT_ROLES
        assert get_role_config(role).get('system_prompt_suffix')


def test_role_catalogue_neuter_would_bite():
    # If the exclusion set is removed, the internal roles reappear. Assert the
    # guard mechanism exists in source.
    src = _src('lib/swarm/registry.py')
    assert 'virtual_user' in src  # sanity — role still defined
    assert '_CATALOGUE_EXCLUDED_ROLES' in src, (
        'format_role_catalogue must consult an explicit exclusion set'
    )


# ── 2. update_memory has no phantom list_all_memories tool reference ─

def test_update_memory_desc_no_phantom_tool():
    from lib.memory.tools import MEMORY_TOOL_NAMES, UPDATE_MEMORY_TOOL
    assert 'list_all_memories' not in MEMORY_TOOL_NAMES  # confirms it's no tool
    mid_desc = (UPDATE_MEMORY_TOOL['function']['parameters']
                ['properties']['memory_id']['description'])
    assert 'list_all_memories' not in mid_desc, (
        'update_memory must not point the model at a non-existent tool')


# ── 3. system_prompt_cc tool-substitution comments accurate ─────────

def test_cc_tool_substitution_comments_accurate():
    # system_prompt_cc became a PACKAGE (system_prompt_cc/); its prompt sections
    # + substitution comments are spread across submodules. Concatenate every
    # .py in the package so the absence-checks cover wherever the comment lives.
    pkg_dir = os.path.join(REPO, 'lib', 'tasks_pkg', 'system_prompt_cc')
    if os.path.isdir(pkg_dir):
        src = ''
        for name in sorted(os.listdir(pkg_dir)):
            if name.endswith('.py'):
                src += _src(os.path.join('lib', 'tasks_pkg', 'system_prompt_cc', name))
    else:  # legacy single-file layout
        src = _src('lib/tasks_pkg/system_prompt_cc.py')
    assert 'Tofu has no todo tool' not in src, (
        'todo_write exists — the stale "no todo tool" comment must be fixed')
    assert 'ask_user via human_guidance' not in src, (
        'the real tool is ask_human, not ask_user')


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:napari']))
