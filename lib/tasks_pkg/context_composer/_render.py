"""Deterministic renderer for :class:`ContextBlock` objects."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.context_composer._models import (
    ComposeRequest,
    ComposeResult,
    ContextBlock,
)

logger = get_logger(__name__)

_MANAGED_MARKER = '<!-- tofu-context:'
_MANAGED_SECTION_RE = re.compile(
    r'<!-- tofu-context:[^>]+:start -->.*?'
    r'<!-- tofu-context:[^>]+:end -->',
    re.DOTALL,
)
_AUTHORITY_ORDER = {
    # Lower-authority evidence is rendered first; higher-authority contracts
    # are physically closer to the generation boundary within each placement.
    'evidence': 10,
    'ambient': 20,
    'preference': 30,
    'workflow': 40,
    'project': 50,
    'user': 60,
    'platform': 70,
}
_PLACEMENT_ORDER = {'system': 0, 'head': 1, 'tail': 2, 'tool_result': 3}


def _count_tokens(text: str, model: str) -> int:
    try:
        from lib.token_counter import count_text
        return max(0, int(count_text(text, model=model)))
    except Exception as exc:
        logger.debug('[ContextComposer] token counter fallback: %s', exc)
        return max(1, (len(text) + 3) // 4) if text else 0


def _truncate(text: str, max_tokens: int | None, model: str) -> tuple[str, bool]:
    if not max_tokens or _count_tokens(text, model) <= max_tokens:
        return text, False
    # Tokenizers are intentionally not asked to decode here. A conservative
    # char floor keeps rendering provider-neutral and deterministic.
    cap = max(128, max_tokens * 3)
    return text[:cap].rstrip() + '\n\n[context block truncated by budget]', True


def _strip_managed(messages: list[dict[str, Any]]) -> None:
    """Remove a previous render during Planner/Worker/Critic re-entry."""
    kept: list[dict[str, Any]] = []
    for message in messages:
        if message.get('_contextComposer'):
            continue
        content = message.get('content')
        if isinstance(content, list):
            blocks = [
                block for block in content
                if not (isinstance(block, dict)
                        and _MANAGED_MARKER in str(block.get('text') or ''))
            ]
            message['content'] = blocks
        elif isinstance(content, str) and _MANAGED_MARKER in content:
            # Managed system blocks are normally separate structured blocks.
            # This fallback handles snapshots produced by providers that
            # flattened them.
            message['content'] = _MANAGED_SECTION_RE.sub('', content).strip()
        kept.append(message)
    messages[:] = kept


def _envelope(block: ContextBlock, text: str) -> str:
    return (
        f'<!-- tofu-context:{block.id}:start -->\n'
        '<system-reminder>\n'
        f'[Context authority: {block.authority}; source: {block.source}]\n'
        f'{text}\n'
        '</system-reminder>\n'
        f'<!-- tofu-context:{block.id}:end -->'
    )


def _unwrap_reminder(text: str) -> str:
    """Avoid nested ``<system-reminder>`` wrappers from legacy providers."""
    stripped = text.strip()
    start = '<system-reminder>'
    end = '</system-reminder>'
    if stripped.startswith(start) and stripped.endswith(end):
        return stripped[len(start):-len(end)].strip()
    return stripped


def _ensure_system(messages: list[dict[str, Any]]) -> dict[str, Any]:
    if messages and messages[0].get('role') == 'system':
        system = messages[0]
    else:
        system = {'role': 'system', 'content': []}
        messages.insert(0, system)
    content = system.get('content')
    if isinstance(content, str):
        system['content'] = ([{'type': 'text', 'text': content}]
                             if content.strip() else [])
    elif not isinstance(content, list):
        system['content'] = []
    return system


def _emit_context_summary(request: ComposeRequest, names: str,
                          total: int) -> None:
    """Best-effort instrumentation; logging must never block a model turn."""
    try:
        round_num = len(((request.task or {}).get('toolRounds') or []))
        logger.info('[Context] conv=%s round=%d blocks=[%s] total=%d',
                    (request.conv_id or '?')[:8], round_num, names, total)
    except Exception:
        # This handler guards the logging backend itself. Logging here could
        # recurse into the same failure and must never break prompt assembly.
        return


def _insert_head(messages: list[dict[str, Any]], texts: list[str]) -> None:
    if not texts:
        return
    idx = 0
    while idx < len(messages) and messages[idx].get('role') == 'system':
        idx += 1
    messages.insert(idx, {
        'role': 'user',
        'content': [{'type': 'text', 'text': text} for text in texts],
        '_isMeta': True,
        '_contextComposer': True,
    })


def _append_tail(messages: list[dict[str, Any]], texts: list[str]) -> None:
    if not texts:
        return
    messages.append({
        'role': 'user',
        'content': [{'type': 'text', 'text': text} for text in texts],
        '_isMeta': True,
        '_contextComposer': True,
    })


def render_context(messages: list[dict[str, Any]], blocks: list[ContextBlock],
                   request: ComposeRequest, *,
                   replace_managed: bool = True) -> ComposeResult:
    """Render blocks once, returning the messages and their exact manifest."""
    if replace_managed:
        _strip_managed(messages)
    manifest: list[dict[str, Any]] = []
    winners: dict[str, ContextBlock] = {}
    ordered = sorted(
        blocks,
        key=lambda b: (
            _PLACEMENT_ORDER[b.placement], _AUTHORITY_ORDER[b.authority],
            b.priority, b.id,
        ),
    )
    for block in ordered:
        key = block.dedupe_key or block.id
        if key in winners:
            manifest.append({
                'id': block.id, 'source': block.source,
                'authority': block.authority, 'placement': block.placement,
                'stability': block.stability, 'lifecycle': block.lifecycle,
                'injected': False, 'chars': 0, 'tokens': 0,
                'reason': f'duplicate_of:{winners[key].id}',
                'provenance': dict(block.provenance),
            })
            continue
        winners[key] = block
        if block.suppressed_reason or not block.content.strip():
            manifest.append({
                'id': block.id, 'source': block.source,
                'authority': block.authority, 'placement': block.placement,
                'stability': block.stability, 'lifecycle': block.lifecycle,
                'injected': False, 'chars': 0, 'tokens': 0,
                'reason': block.suppressed_reason or 'empty',
                'provenance': dict(block.provenance),
            })
            continue
        text, truncated = _truncate(_unwrap_reminder(block.content), block.max_tokens,
                                    request.model)
        rendered = _envelope(block, text)
        block_tokens = _count_tokens(rendered, request.model)
        manifest.append({
            'id': block.id, 'source': block.source,
            'authority': block.authority, 'placement': block.placement,
            'stability': block.stability, 'lifecycle': block.lifecycle,
            'injected': True, 'chars': len(rendered), 'tokens': block_tokens,
            'hash': hashlib.sha256(rendered.encode('utf-8')).hexdigest()[:16],
            'reason': 'truncated' if truncated else '',
            'provenance': dict(block.provenance),
        })
        manifest[-1]['_rendered'] = rendered

    system_texts = [row.pop('_rendered') for row in manifest
                    if row.get('_rendered') and row['placement'] == 'system']
    head_texts = [row.pop('_rendered') for row in manifest
                  if row.get('_rendered') and row['placement'] == 'head']
    tail_texts = [row.pop('_rendered') for row in manifest
                  if row.get('_rendered') and row['placement'] == 'tail']
    for order, row in enumerate(manifest):
        row['order'] = order
    if system_texts:
        system = _ensure_system(messages)
        # Preserve the established authority order: an operator-provided
        # system prompt is followed by the platform's safety/capability
        # contract. ``replace`` mode suppresses the platform block upstream.
        system['content'].extend(
            {'type': 'text', 'text': text} for text in system_texts)
    _insert_head(messages, head_texts)
    _append_tail(messages, tail_texts)

    total = sum(row['chars'] for row in manifest if row['injected'])
    names = ','.join(f"{row['id']}:{row['chars']}" for row in manifest
                     if row['injected'])
    _emit_context_summary(request, names, total)
    return ComposeResult(messages=messages, manifest=manifest)


__all__ = ['render_context']
