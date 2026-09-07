"""tests/test_swarm_role_catalogue_tools.py — the spawn_agents description
must advertise what each role CAN DO (its tool list), not just when to use it.

WHY
---
2026-07-27 incident: the master spawned a ``researcher`` sub-agent to digest
four past project conversations via ``get_conversation``. The researcher
role's ``tools_hint`` is ``[web_search, fetch_url, browser_read_page,
browser_list_tabs]`` — no conversation tools — so the sub-agent correctly
reported "tool unavailable" and the turn died. Root cause: the role catalogue
inside the spawn_agents description rendered only ``role: when_to_use``
prose. The master model could not SEE that no specialist role carries
``get_conversation`` (only ``general`` does), and the description carried no
recovery rule for a mis-picked role.

Pins three things:

  A. ``format_role_catalogue()`` renders each spawnable role's hint tools,
     spells out what an unrestricted (empty-hint) role gets, and names the
     shared artifact tools — all derived from the single sources
     ``AGENT_ROLES`` / ``SUB_AGENT_DENYLIST`` / ``ARTIFACT_TOOLS``.
  B. The spawn_agents description embeds that tool-aware catalogue AND
     carries the selection rule (task needs a tool outside every
     specialist's list → use 'general') plus the mis-pick recovery rule
     (sub-agent reports a missing tool → re-spawn with role='general').
  C. The description tells the master which tools sub-agents NEVER have
     (the denylist), so objectives stay self-contained.

NEUTER evidence expected:
  * Removing the tool-name rendering from ``format_role_catalogue`` must
    make ``test_catalogue_lists_each_roles_hint_tools`` go red.
  * Removing the recovery paragraph from
    ``_build_spawn_agents_description`` must make
    ``test_description_carries_recovery_rule`` go red.
"""

import os
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.swarm.registry import (  # noqa: E402
    AGENT_ROLES,
    _CATALOGUE_EXCLUDED_ROLES,
    format_role_catalogue,
)
from lib.swarm.tools import (  # noqa: E402
    ARTIFACT_TOOLS,
    SPAWN_AGENTS_TOOL,
    SUB_AGENT_DENYLIST,
)

_DESC = SPAWN_AGENTS_TOOL['function']['description']
_DENY_STR = '/'.join(sorted(SUB_AGENT_DENYLIST))
_ARTIFACT_NAMES = [t['function']['name'] for t in ARTIFACT_TOOLS]


def _spawnable():
    return {r: c for r, c in AGENT_ROLES.items()
            if r not in _CATALOGUE_EXCLUDED_ROLES}


def _role_segment(catalogue: str, role: str) -> str:
    """Slice one role's block out of the catalogue text."""
    lines = catalogue.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.strip().startswith(f'- {role}:'))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip().startswith('- ')), len(lines))
    return '\n'.join(lines[start:end])


def test_catalogue_lists_each_roles_hint_tools():
    catalogue = format_role_catalogue()
    for role, cfg in _spawnable().items():
        segment = _role_segment(catalogue, role)
        hint = cfg.get('tools_hint') or []
        if hint:
            for tool_name in hint:
                assert tool_name in segment, (
                    f'role {role!r}: tool {tool_name!r} not advertised in '
                    f'the role catalogue')
        else:
            # Empty hint = unrestricted minus the denylist — the catalogue
            # must SAY so (naming the denylist), not leave it ambiguous.
            assert f'minus {_DENY_STR}' in segment, (
                f'role {role!r} has unrestricted tools but the catalogue '
                f'does not spell out "ALL tools minus {_DENY_STR}"')


def test_catalogue_names_shared_artifact_tools():
    catalogue = format_role_catalogue()
    for name in _ARTIFACT_NAMES:
        assert name in catalogue, (
            f'shared artifact tool {name!r} not advertised in the '
            f'role catalogue')


def test_description_embeds_tool_lists():
    # The tool-aware catalogue must actually land inside the description
    # the master reads — not rendered into a void.
    for role, cfg in _spawnable().items():
        assert f'- {role}:' in _DESC, (
            f'role {role!r} missing from spawn_agents description')
        for tool_name in (cfg.get('tools_hint') or []):
            assert tool_name in _DESC, (
                f'role {role!r} tool {tool_name!r} missing from '
                f'spawn_agents description')
    assert f'minus {_DENY_STR}' in _DESC, (
        'spawn_agents description never tells the master which tools '
        'sub-agents never have')


def test_description_carries_selection_rule():
    assert 'needs a tool not listed' in _DESC, (
        'spawn_agents description lacks the selection rule: task needs a '
        'tool outside every specialist list → use general')
    assert "use 'general'" in _DESC


def test_description_carries_recovery_rule():
    assert 'reports a missing tool' in _DESC, (
        'spawn_agents description lacks the mis-pick recovery paragraph')
    assert 'Re-spawn the same task' in _DESC, (
        'recovery rule must say: re-spawn the task with a covering role '
        "('general'), not work around or abandon")


def test_catalogue_has_one_prompt_owner_and_bounded_control_prose():
    from lib.tasks_pkg.context_composer._providers import _swarm_guidance
    from lib.token_counter import count_text
    from lib.tools.gateway import tool_schema_tokens

    catalogue = format_role_catalogue()
    guidance = _swarm_guidance(SimpleNamespace(task={
        'config': {'orchestration': {'multiAgent': 'read_only'}},
    }), 'parallel research')
    assert catalogue in _DESC
    assert catalogue not in guidance
    assert 'tool schema is the sole authority' in guidance
    for contract in (
        '2+ independent', 'trivial or sequential', 'fire-and-forget',
        'do not poll/sleep', 'predict results', 'output_file', 'depends_on',
        'await_agents', 'get_agent_result', 'root synthesizes',
    ):
        assert contract in _DESC
    assert tool_schema_tokens([SPAWN_AGENTS_TOOL], model='kimi-k3') <= 1_050
    assert count_text(guidance, model='kimi-k3') <= 128


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
