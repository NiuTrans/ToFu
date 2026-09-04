"""New-chat shells must never persist the localized placeholder as a title.

Incident (2026-08-31, owner report: sidebar full of 新对话 rows that never
resolve to the first user message): conversations created through the
project modal kept "新对话" as their durable title forever.

Root cause chain:

  * ``project.js`` created its conversation shell with
    ``title: t('chat.newConversation')`` — the LOCALIZED display label —
    violating the "durable titles are language-neutral" rule documented in
    ``shell-localization.ts``.
  * ``titleForTurnConversationCreate`` (turn-runtime.ts) only strips the
    neutral ``'New Chat'`` sentinel, so "新对话" was uploaded as a real title
    on the first Turn command.
  * The server's first-message derivation (command_service.py) only runs
    when the client sends an EMPTY title, so it was skipped — and with
    auto-title generation opt-in (default off), nothing ever replaced it.

The append-settled Turn command path had the same laundering shape
(``conversation.title || 'New Chat'``); it now shares the stripping helper.

Pinned here at source level (the runtime sections are the retained browser
authority; ``npm run check:runtime`` proves the generated bundle matches).
"""

from __future__ import annotations

import re

import pytest

from tests._runtime_sections import (
    ROOT,
    SECTIONS,
    runtime_section_names,
    shipped_source_text,
)

pytestmark = pytest.mark.unit

_TURN_RUNTIME = ROOT / 'frontend' / 'src' / 'core' / 'turn-runtime.ts'

# A title field fed from the i18n display label — the laundering shape.
_LOCALIZED_TITLE_RE = re.compile(
    r'title\s*:\s*[^,\n]*t\(\s*[\'"]chat\.newConversation[\'"]')


def _section_sources() -> list[tuple[str, str]]:
    return [
        (name, shipped_source_text(
            f'frontend/src/runtime/sections/{name}'))
        for name in runtime_section_names()
        if (SECTIONS / name).is_file()
    ]


def test_no_section_launders_localized_label_into_durable_title():
    offenders = [
        f'{name}:{i + 1}'
        for name, src in _section_sources()
        for i, line in enumerate(src.splitlines())
        if _LOCALIZED_TITLE_RE.search(line)
    ]
    assert not offenders, (
        'localized chat.newConversation label assigned to a durable title '
        f'field: {offenders}')


def test_project_modal_shell_uses_neutral_sentinel():
    src = shipped_source_text('frontend/src/runtime/sections/project.js')
    shell = re.search(
        r'const conv = \{\n(.*?)_localOnly: true,', src, re.DOTALL)
    assert shell, 'project modal shell creation not found'
    assert "title: 'New Chat'" in shell.group(1), (
        'project modal shell lost the language-neutral title sentinel')


def test_turn_commands_strip_placeholder_on_both_create_paths():
    src = _TURN_RUNTIME.read_text(encoding='utf-8')
    uses = src.count('title: titleForTurnConversationCreate(conversation)')
    assert uses == 2, (
        f'expected both Turn conversation payloads to strip the placeholder, '
        f'found {uses}')
    assert "conversation.title || 'New Chat'" not in src, (
        'raw placeholder pass-through reintroduced in a Turn command payload')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
