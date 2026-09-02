"""Deterministic renderer for :class:`ContextBlock` objects."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.context_composer._models import (
    ComposeRequest,
    ComposeResult,
    ContextBlock,
    ContextPlanEntryV2,
    ContextPlanV2,
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
_LAYER_ORDER = {
    'objective_constraints': 0,
    'task_state': 1,
    'evidence': 2,
    'hot_tail': 3,
    'cold_history': 4,
}


def _count_tokens(text: str, model: str) -> int:
    try:
        from lib.token_counter import count_text
        return max(0, int(count_text(text, model=model)))
    except Exception as exc:
        logger.debug('[ContextComposer] token counter fallback: %s', exc)
        return max(1, (len(text) + 3) // 4) if text else 0


def _truncate(text: str, max_tokens: int | None, model: str) -> tuple[str, bool]:
    if max_tokens is None or int(max_tokens) <= 0 \
            or _count_tokens(text, model) <= int(max_tokens):
        return text, False
    # The block-local ceiling is a hard content-token limit. Use the same
    # provider-neutral counter as the manifest and bisect characters instead
    # of assuming a fixed chars/token ratio (which is unsafe for CJK/base64).
    limit = int(max_tokens)
    marker = '\n\n[context block truncated by budget]'
    suffix = marker if _count_tokens(marker, model) <= limit else ''
    low, high, best = 0, len(text), ''
    while low <= high:
        middle = (low + high) // 2
        candidate = text[:middle].rstrip() + suffix
        if _count_tokens(candidate, model) <= limit:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best, True


def _fit_global_budget(block: ContextBlock, text: str, budget: int,
                       model: str) -> tuple[str, bool] | None:
    """Fit one optional block into ``budget`` with deterministic bisection."""
    if budget <= 0:
        return None
    rendered = _envelope(block, text)
    if _count_tokens(rendered, model) <= budget:
        return text, False
    marker = '\n\n[context block truncated by global budget]'
    low, high = 0, len(text)
    best = ''
    while low <= high:
        middle = (low + high) // 2
        candidate = text[:middle].rstrip() + marker
        if _count_tokens(_envelope(block, candidate), model) <= budget:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    if not best or not text[:max(0, len(best) - len(marker))].strip():
        return None
    return best, True


def _hash_join(values: list[str]) -> str:
    return hashlib.sha256('\x00'.join(values).encode('utf-8')).hexdigest()[:24]


def _cache_epoch(request: ComposeRequest) -> int:
    task = request.task
    if task is None:
        return 0
    tool_manifest = (task.get('_tool_schema')
                     if isinstance(task.get('_tool_schema'), list) else None)
    tool_manifest_value = (
        json.dumps(tool_manifest, ensure_ascii=False, sort_keys=True,
                   separators=(',', ':'), default=str)
        if tool_manifest is not None else '\x00'.join(sorted(request.tool_names))
    )
    state = {
        'permissions': sorted(request.granted_permissions),
        'tools': sorted(request.tool_names),
        'toolManifestHash': hashlib.sha256(
            tool_manifest_value.encode('utf-8')).hexdigest()[:24],
        'compactionBoundary': str(task.get('_compactionBoundary') or ''),
    }
    key = hashlib.sha256(json.dumps(
        state, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()[:24]
    previous = str(task.get('_contextCacheEpochKey') or '')
    epoch = max(0, int(task.get('_contextCacheEpoch') or 0))
    if previous and previous != key:
        epoch += 1
    task['_contextCacheEpochKey'] = key
    task['_contextCacheEpoch'] = epoch
    return epoch


def _apply_global_budget(manifest: list[dict[str, Any]],
                         request: ComposeRequest) -> ContextPlanV2:
    """Select optional candidates while never evicting required blocks."""
    budget = max(0, int(request.global_budget_tokens or 0))
    base = max(0, int(request.base_context_tokens or 0))
    available = max(0, budget - base)
    candidates = [row for row in manifest if row.get('_rendered')]
    required = [row for row in candidates if row['_block'].required]
    optional = [row for row in candidates if not row['_block'].required]
    optional.sort(key=lambda row: (
        _LAYER_ORDER[row['_block'].layer],
        row['_block'].priority,
        -max(0, row['_block'].observed_at_ms),
        -max(0, row['_block'].access_count),
        row['tokens'],
        row['id'],
    ))

    selected_tokens = sum(row['tokens'] for row in required)
    remaining = max(0, available - selected_tokens)
    for row in optional:
        block = row['_block']
        if row['tokens'] <= remaining:
            selected_tokens += row['tokens']
            remaining -= row['tokens']
            continue
        fitted = _fit_global_budget(
            block, row['_raw_text'], remaining, request.model)
        if fitted is None:
            row['_rendered'] = ''
            row['injected'] = False
            row['chars'] = 0
            row['tokens'] = 0
            row['hash'] = hashlib.sha256(
                row['_raw_text'].encode('utf-8')).hexdigest()[:16]
            row['reason'] = 'global_budget_exhausted'
            continue
        fitted_text, global_truncated = fitted
        rendered = _envelope(block, fitted_text)
        tokens = _count_tokens(rendered, request.model)
        row['_rendered'] = rendered
        row['chars'] = len(rendered)
        row['tokens'] = tokens
        row['hash'] = hashlib.sha256(
            rendered.encode('utf-8')).hexdigest()[:16]
        if global_truncated:
            row['reason'] = 'global_budget_truncated'
            row['_global_truncated'] = True
        selected_tokens += tokens
        remaining = max(0, remaining - tokens)

    tool_manifest = ((request.task or {}).get('_tool_schema')
                     if isinstance((request.task or {}).get('_tool_schema'), list)
                     else None)
    tool_manifest_values = ([json.dumps(
        tool_manifest, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str)]
        if tool_manifest is not None else sorted(request.tool_names))
    segment_values: dict[str, list[str]] = {
        'staticPrefix': [], 'toolManifest': tool_manifest_values,
        'conversation': [], 'dynamicTail': [],
    }
    for row in manifest:
        if not row.get('_rendered'):
            continue
        block = row['_block']
        segment = ('staticPrefix' if block.stability == 'static' else
                   'conversation' if block.stability == 'conversation' else
                   'dynamicTail')
        segment_values[segment].append(row['_rendered'])
    segment_hashes = {
        name: _hash_join(values) for name, values in segment_values.items()
    }
    entries = tuple(ContextPlanEntryV2(
        id=row['id'],
        layer=row['_block'].layer,
        selected=bool(row.get('injected')),
        truncated=bool(row.get('reason') in {
            'truncated', 'global_budget_truncated'}),
        reason=str(row.get('reason') or ''),
        tokens=max(0, int(row.get('tokens') or 0)),
        content_hash=str(row.get('hash') or hashlib.sha256(
            row['_raw_text'].encode('utf-8')).hexdigest()[:16]),
        recovery_handle=row['_block'].recovery_handle,
        observed_at_ms=max(0, row['_block'].observed_at_ms),
        world_version=row['_block'].world_version,
    ) for row in manifest)
    return ContextPlanV2(
        contract_version='tofu.context-plan/v2',
        budget_tokens=budget,
        base_tokens=base,
        selected_tokens=selected_tokens,
        overflow_tokens=max(0, base + selected_tokens - budget),
        entries=entries,
        segment_hashes=segment_hashes,
        cache_epoch=_cache_epoch(request),
    )


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
                '_block': block, '_raw_text': _unwrap_reminder(block.content),
            })
            continue
        winners[key] = block
        missing_permissions = (
            block.required_permissions - request.granted_permissions)
        if missing_permissions:
            manifest.append({
                'id': block.id, 'source': block.source,
                'authority': block.authority, 'placement': block.placement,
                'stability': block.stability, 'lifecycle': block.lifecycle,
                'injected': False, 'chars': 0, 'tokens': 0,
                'reason': 'permission_denied:' + ','.join(
                    sorted(missing_permissions)),
                'provenance': dict(block.provenance),
                '_block': block, '_raw_text': _unwrap_reminder(block.content),
            })
            continue
        if block.suppressed_reason or not block.content.strip():
            manifest.append({
                'id': block.id, 'source': block.source,
                'authority': block.authority, 'placement': block.placement,
                'stability': block.stability, 'lifecycle': block.lifecycle,
                'injected': False, 'chars': 0, 'tokens': 0,
                'reason': block.suppressed_reason or 'empty',
                'provenance': dict(block.provenance),
                '_block': block, '_raw_text': _unwrap_reminder(block.content),
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
            '_block': block, '_raw_text': text,
        })
        manifest[-1]['_rendered'] = rendered

    plan = (_apply_global_budget(manifest, request)
            if request.global_budget_tokens is not None else None)

    system_texts = [row.pop('_rendered') for row in manifest
                    if row.get('_rendered') and row['placement'] == 'system']
    head_texts = [row.pop('_rendered') for row in manifest
                  if row.get('_rendered') and row['placement'] == 'head']
    tail_texts = [row.pop('_rendered') for row in manifest
                  if row.get('_rendered') and row['placement'] == 'tail']
    for order, row in enumerate(manifest):
        row['order'] = order
        row.pop('_block', None)
        row.pop('_raw_text', None)
        row.pop('_global_truncated', None)
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
    return ComposeResult(messages=messages, manifest=manifest, plan=plan)


__all__ = ['render_context']
