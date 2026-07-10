"""lib/conversations/vu_translate_backfill.py — the ONE "which VU turns still
need translating" primitive.

Autopilot virtual-user (VU) turns are persisted on a path SEPARATE from
``manager._sync_result_to_conversation`` (which owns the assistant/critic
auto-translate safety net). Before the ``_maybe_auto_translate_vu`` wire
(autopilot.py, 2026-07-10) that path had ZERO ``_maybe_auto_translate_*`` calls,
so every VU turn in an autopilot run was left untranslated unless a viewer
happened to fire a manual translate — the reported "this conversation never
triggers auto-translate" bug (conv mre58lxth33ncr).

The forward wire fixes NEW turns; already-persisted VU turns need a one-shot
backfill (``tests/_migrate_backfill_vu_translations.py``). Per the conv-OOM
lesson ("a write-path fix is incomplete without a backfill that REUSES, not
copies, the write-path logic"), the shared "which rows qualify" predicate lives
HERE so the migration can never drift from the eventual live gate. This module
owns ONLY the pure selection; the migration fetches the rows and delegates the
ACTUAL translation to the same ``_maybe_auto_translate_vu`` the live path uses,
so the safety net's own gates (autoTranslate off / already-Chinese / existing
translatedContent / in-flight dedup) still apply — this predicate is a cheap
pre-filter, NOT a second gate.

A VU turn is stored ``role='user'`` + ``_isVirtualUser=True`` and is
DISPLAY-translated: ``content`` = model-language original (原文 toggle), and the
safety net writes the UI-language ``translatedContent`` (outer 译文 bubble) —
the OPPOSITE of a normal user message. So a row "needs translating" when it has
non-empty ``content`` but no ``translatedContent``.

Pure: no Flask, no DB handle, no LLM. The caller supplies the messages list.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


def collect_untranslated_vu_turns(messages: list[Any]) -> list[dict]:
    """Return ``[{idx, msgId, content}]`` for each autopilot VU turn that lacks
    a ``translatedContent`` but carries non-empty ``content``.

    A VU turn is ``role='user'`` + ``_isVirtualUser=True``. Returns an empty
    list when nothing qualifies (so a caller can early-out). This is a
    pre-filter only: the migration hands each hit to ``_maybe_auto_translate_vu``,
    whose own gates (autoTranslate resolution, already-Chinese short-circuit,
    stale-partial re-translate, in-flight dedup) make the final decision — so
    this deliberately does NOT re-implement the language / settings checks.
    """
    out: list[dict] = []
    if not isinstance(messages, list):
        return out
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        if m.get('role') != 'user' or not m.get('_isVirtualUser'):
            continue
        content = m.get('content') or ''
        if not content:
            continue
        tc = m.get('translatedContent') or ''
        if tc:
            continue  # already display-translated
        out.append({
            'idx': i,
            'msgId': m.get('_msgId') or '',
            'content': content,
        })
    return out
