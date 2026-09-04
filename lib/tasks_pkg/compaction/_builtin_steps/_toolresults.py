# HOT_PATH
"""Phase B — compress cold tool results.

Faithful extraction of the historical ``micro_compact`` Phase B body,
re-expressed against :class:`CompactionContext` and registered as the
``compact_tool_results`` step.  Records paired-assistant indices in
``ctx.scratch['paired_assistant_indices']`` so a later
``fold_paired_interstitial`` step can co-compact them.
"""

from __future__ import annotations

import json

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import CompactionContext, register_step
from lib.tasks_pkg.compaction._tokens import _estimate_msg_tokens, _human_size
from lib.tasks_pkg.compaction._builtin_steps._shared import _log_id

logger = get_logger(__name__)


def _archive_message_if_enabled(ctx: CompactionContext, msg: dict,
                                content, tool_name: str) -> str | None:
    """Message-aware wrapper so filenames and telemetry carry the call ID."""
    task = ctx.task if isinstance(ctx.task, dict) else None
    if task is None:
        return None
    try:
        from lib.context_experiment_flags import normalize_context_experiment_flags
        enabled = normalize_context_experiment_flags(
            task.get('config') or {})['compaction']['evidenceLedger']
        if not enabled:
            return None
        from lib.tasks_pkg.compaction._persist import _persist_to_disk
        raw = (content if isinstance(content, str) else
               json.dumps(content, ensure_ascii=False, sort_keys=True))
        tool_call_id = str(msg.get('tool_call_id') or '')
        placeholder = _persist_to_disk(
            raw, tool_name, tool_call_id, ctx.conv_id)
        if not isinstance(placeholder, str) or '[Persisted to:' not in placeholder:
            logger.warning('[L1] evidence archive for %s returned no durable '
                           'reference; using the normal compact placeholder',
                           tool_name)
            return None
        task.setdefault('_contextEvidenceArchives', []).append({
            'toolName': tool_name,
            'toolCallId': tool_call_id,
            'contentChars': len(raw),
            'reference': placeholder.splitlines()[0] if placeholder else '',
        })
        return placeholder
    except Exception as exc:
        logger.warning('[L1] evidence archive failed for %s: %s',
                       tool_name, exc)
        return None


def _find_paired_assistant(messages: list, tool_idx: int) -> int | None:
    """Walk backward from a tool index to its paired assistant(tool_calls)
    message.  Returns None if a user/system boundary is crossed first."""
    for j in range(tool_idx - 1, -1, -1):
        role_j = messages[j].get('role')
        if role_j == 'assistant':
            return j
        if role_j in ('user', 'system'):
            return None
    return None


def _newest_tool_batch_indices(messages: list, tool_indices: list[int]) -> set[int]:
    """Return the latest assistant(tool_calls) batch's result indices.

    This evidence is input to the immediately following model call and must
    survive even when one result alone exceeds the token-tail budget.
    """
    for assistant_idx in range(len(messages) - 1, -1, -1):
        assistant = messages[assistant_idx]
        if assistant.get('role') != 'assistant' or not assistant.get('tool_calls'):
            continue
        call_ids = {
            str(call.get('id') or '')
            for call in assistant.get('tool_calls') or ()
            if isinstance(call, dict) and call.get('id')
        }
        if not call_ids:
            continue
        batch = {
            idx for idx in tool_indices
            if idx > assistant_idx
            and str(messages[idx].get('tool_call_id') or '') in call_ids
        }
        if batch:
            # A later user/assistant boundary proves the model already consumed
            # this batch. It is no longer the evidence for the immediately
            # following call and may be compacted under the ordinary budget.
            last_result = max(batch)
            if any(messages[idx].get('role') in ('user', 'assistant')
                   for idx in range(last_result + 1, len(messages))):
                return set()
            return batch
    return set()


def _hot_tool_result_indices(ctx: CompactionContext,
                             tool_indices: list[int]) -> set[int]:
    """Select a count- and token-bounded hot tail plus the newest batch."""
    if not tool_indices:
        return set()
    count_limit = max(0, int(ctx.constants.MICRO_HOT_TAIL))
    token_limit = max(0, int(getattr(
        ctx.constants, 'MICRO_HOT_TAIL_TOKENS', 48_000)))
    protected = _newest_tool_batch_indices(ctx.messages, tool_indices)
    count_hot = set(tool_indices[-count_limit:]) if count_limit else set()

    token_hot: set[int] = set(protected)
    accumulated = sum(
        max(0, _estimate_msg_tokens(ctx.messages[idx])) for idx in protected)
    for idx in reversed(tool_indices):
        if idx in protected:
            continue
        estimate = max(0, _estimate_msg_tokens(ctx.messages[idx]))
        if accumulated + estimate > token_limit:
            break
        token_hot.add(idx)
        accumulated += estimate
    return protected | (count_hot & token_hot)


@register_step('compact_tool_results')
def compact_tool_results(ctx: CompactionContext) -> int:
    """Compress cold tool results outside the hot tail.  Records the
    paired-assistant indices in ``ctx.scratch['paired_assistant_indices']``
    so a later ``fold_paired_interstitial`` step can co-compact them."""
    _c = ctx.constants
    messages = ctx.messages
    conv_id = ctx.conv_id
    tokens_before: int | None = None

    def capture_tokens_before_mutation() -> None:
        """Pay the conversation-sized telemetry count only on a write pass."""
        nonlocal tokens_before
        if tokens_before is not None:
            return
        try:
            from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
            tokens_before = _estimate_total_tokens(messages)
        except Exception as exc:
            logger.debug('[L1] pre-compaction token estimate unavailable: %s', exc)
            tokens_before = 0

    paired_assistant_indices: set[int] = ctx.scratch.setdefault(
        'paired_assistant_indices', set())

    tool_indices = [i for i, m in enumerate(messages) if m.get('role') == 'tool']

    hot_indices = _hot_tool_result_indices(ctx, tool_indices)
    cold_indices = [idx for idx in tool_indices if idx not in hot_indices]
    if not cold_indices:
        logger.debug('[L1] %d tool results fit count=%d/token=%d hot tail; '
                     'skipping Phase B (Phase C image strip may still run)',
                     len(tool_indices), _c.MICRO_HOT_TAIL,
                     int(getattr(_c, 'MICRO_HOT_TAIL_TOKENS', 48_000)))

    compacted_count = 0
    skipped_short = 0
    skipped_already = 0
    tool_tokens_saved = 0

    for idx in cold_indices:
        if ctx.is_in_cache_prefix(idx):
            skipped_already += 1
            continue

        msg = messages[idx]
        content = msg.get('content', '')
        tool_name = msg.get('name', 'tool')
        mutated = False

        # ── Multimodal content (list of content blocks) ──
        if isinstance(content, list):
            text_parts = []
            image_count = 0
            image_chars = 0
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'text':
                    text_parts.append(b.get('text', ''))
                elif b.get('type') == 'image_url':
                    image_count += 1
                    image_chars += len(b.get('image_url', {}).get('url', ''))

            text_len = sum(len(t) for t in text_parts)

            if image_count > 0:
                _before_chars = text_len + image_chars
                text_preview = ' '.join(text_parts).strip()[:200]
                capture_tokens_before_mutation()
                archived = _archive_message_if_enabled(
                    ctx, msg, content, tool_name)
                msg['content'] = archived or (
                    f'[{tool_name} result compacted — had {image_count} '
                    f'image(s) ({_human_size(image_chars)} base64) + '
                    f'{text_len:,} chars text — re-call tool if needed]\n'
                    f'Text was: {text_preview}'
                )
                tool_tokens_saved += text_len // 4 + image_count * _c._IMAGE_TOKENS_DEFAULT
                compacted_count += 1
                mutated = True
                ctx.stamp(msg, _before_chars, len(msg['content']))
            elif text_len <= _c.MICRO_COMPACT_THRESHOLD:
                skipped_short += 1
            else:
                _before_chars = text_len
                capture_tokens_before_mutation()
                archived = _archive_message_if_enabled(
                    ctx, msg, content, tool_name)
                msg['content'] = archived or (
                    f'[{tool_name} result compacted — was {text_len:,} chars'
                    f' — re-call tool if full content needed]'
                )
                tool_tokens_saved += max(0, text_len - len(msg['content'])) // 4
                compacted_count += 1
                mutated = True
                ctx.stamp(msg, _before_chars, len(msg['content']))

        # ── Plain-string content ──
        elif isinstance(content, str):
            if content.startswith('[') and 'compacted' in content[:80]:
                skipped_already += 1
            elif content.startswith('[Persisted to:'):
                skipped_already += 1
            elif tool_name == 'load_skill' and content.startswith('Skill loaded:'):
                old_len = len(content)
                capture_tokens_before_mutation()
                skill_id = ''
                content_hash = ''
                for line in content.splitlines()[:8]:
                    if line.startswith('id: '):
                        skill_id = line[4:].strip()
                    elif line.startswith('content_hash: '):
                        content_hash = line[14:].strip()
                msg['content'] = (
                    '[load_skill receipt — full workflow compacted]\n'
                    f'id: {skill_id or "unknown"}\n'
                    f'content_hash: {content_hash or "unknown"}\n'
                    'Call load_skill with this exact id if the full workflow '
                    'is needed again.'
                )
                tool_tokens_saved += max(
                    0, old_len - len(msg['content'])) // 4
                compacted_count += 1
                mutated = True
                ctx.stamp(msg, old_len, len(msg['content']))
            elif len(content) <= _c.MICRO_COMPACT_THRESHOLD:
                skipped_short += 1
            else:
                old_len = len(content)
                capture_tokens_before_mutation()
                first_two = '\n'.join(content.split('\n')[:2])
                if len(first_two) > 120:
                    first_two = first_two[:120] + '…'
                placeholder = (
                    f'[{tool_name} result compacted — was {old_len:,} chars]\n'
                    f'Preview: {first_two}\n'
                    f'[Re-call tool if full content needed]'
                )
                msg['content'] = (_archive_message_if_enabled(
                    ctx, msg, content, tool_name) or placeholder)
                tool_tokens_saved += max(0, old_len - len(msg['content'])) // 4
                compacted_count += 1
                mutated = True
                ctx.stamp(msg, old_len, len(msg['content']))

        if mutated:
            paired_idx = _find_paired_assistant(messages, idx)
            if paired_idx is not None and not ctx.is_in_cache_prefix(paired_idx):
                paired_assistant_indices.add(paired_idx)

    # A no-op is the overwhelmingly common steady-state outcome. Keep it
    # available for diagnosis without emitting one INFO record per model round.
    _log_pass = logger.info if compacted_count else logger.debug
    _log_pass('[L1] conv=%s  cold=%d  compacted=%d  '
              'skipped_short=%d  skipped_already=%d  '
              '~%d tokens saved',
              _log_id(conv_id),
              len(cold_indices), compacted_count,
              skipped_short, skipped_already, tool_tokens_saved)
    if compacted_count and isinstance(ctx.task, dict):
        try:
            from lib.context_telemetry import record_compaction_event
            from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
            record_compaction_event(
                ctx.task, trigger='l1', reason='cold_tool_results',
                tokens_before=int(tokens_before or 0),
                tokens_after=_estimate_total_tokens(messages))
        except Exception as exc:
            logger.debug('[L1] context telemetry failed: %s', exc)
    return tool_tokens_saved
