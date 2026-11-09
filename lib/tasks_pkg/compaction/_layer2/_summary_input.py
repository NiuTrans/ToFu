"""Assemble the evidence sent to a query-aware L2 summary.

Entry points: compose_summary_input, build_summary_input and the compatibility
archive-receipt reader. This owner combines preserved intent, prior receipts and
bounded tool evidence; the conversation repository remains the sole archive
reader. It performs no model call or history adoption. Input policy bindings are
explicit so existing entrypoint injections still govern formatting and degraded
archive reads. Optional-spend quote pricing and dispatch share the pure
composition, so a quote prices the exact request its route receives.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from lib.log import get_logger
from lib.tasks_pkg.compaction._ownership import compaction_owner_key
from lib.tasks_pkg.compaction._tokens import _human_size
from lib.tasks_pkg.compaction._layer2._prompt import _receipt_body_from_text

logger = get_logger('lib.tasks_pkg.compaction._layer2._summary')


@dataclass(frozen=True, slots=True)
class SummaryInputPorts:
    input_char_budget: Callable
    prior_receipt: Callable
    archived_receipt: Callable
    latest_assistant: Callable
    format_history: Callable
    user_content: Callable
    system_prompt: str


@dataclass(frozen=True, slots=True)
class SummaryInput:
    """One composed summary request and the facts its dispatch reports."""
    prompt: list
    prior_receipt_archived: bool
    prior_receipt_chars: int
    latest_assistant_chars: int
    formatted_chars: int
    full_chars: int
    history_budget: int
    ledger: dict | None
    ledger_error: Exception | None


def _archived_prior_receipt(task: dict | None) -> str:
    """Newest owner-scoped archive summary with structured receipt sections.

    Used only when live messages have no compact tool result (typical after a
    cold rebuild that dropped the synthetic pair). Loads metadata only —
    never the archived transcript payload. Fail-closed: any store error
    returns ''.
    """
    if not isinstance(task, dict):
        return ''
    conv_id = compaction_owner_key(task)
    user_id = task.get('_userId')
    if (not conv_id or not isinstance(user_id, int)
            or isinstance(user_id, bool) or user_id <= 0):
        return ''
    archive_scope = task.get('_archiveScope', '')
    archive_conv_id = task.get('convId') if archive_scope else conv_id
    scoped = {'archive_scope': archive_scope} if archive_scope else {}
    try:
        from lib.agent_core.store import get_conversation_store
        store = get_conversation_store()
        archives = store.list_compaction_archives(
            archive_conv_id, user_id=user_id, limit=8, **scoped)
    except Exception as exc:
        logger.debug('%s archive receipt lookup failed: %s',
                     '[Summary]', exc)
        return ''
    if not isinstance(archives, list) or not archives:
        return ''
    for meta in reversed(archives):
        if not isinstance(meta, dict):
            continue
        archive_id = meta.get('id') or meta.get('archiveId') or meta.get('archive_id')
        if not archive_id:
            continue
        try:
            payload = store.get_compaction_archive(
                archive_conv_id, archive_id, user_id=user_id,
                include_messages=False, **scoped)
        except Exception as exc:
            logger.debug('%s archive %s load failed: %s',
                         '[Summary]', archive_id, exc)
            continue
        if not isinstance(payload, dict):
            continue
        archive = payload.get('archive')
        if not isinstance(archive, dict):
            archive = payload
        body = _receipt_body_from_text(archive.get('summary'))
        if body:
            return body
    return ''


def resolve_prior_receipt(
    messages: list, *, task: dict | None, source_messages: list | None,
    ports: SummaryInputPorts,
) -> tuple[str, bool]:
    """Return ``(receipt, archived)``: the live carrier, else the archive's."""
    receipt_messages = (source_messages if source_messages is not None
                        else messages)
    prior_receipt = ports.prior_receipt(receipt_messages)
    if prior_receipt:
        return prior_receipt, False
    prior_receipt = ports.archived_receipt(task)
    return prior_receipt, bool(prior_receipt)


def compose_summary_input(
    messages: list, current_query: str, *, task: dict | None,
    source_messages: list | None, anchor_text: str, ports: SummaryInputPorts,
    prior_receipt: str | None = None, continuity_fold: frozenset = frozenset(),
) -> SummaryInput:
    """Compose the summary request without clipping user messages or receipts.

    Pure apart from the archive receipt read: no logging and no task writes, so
    a preflight quote can price the request that dispatch later builds. An
    explicit ``prior_receipt`` (a rolling pass's predecessor) replaces lookup.
    ``continuity_fold`` names the observations the planned packet no longer
    carries; they follow it as evidence the narrative must carry instead.
    """
    _char_budget = ports.input_char_budget(task)
    receipt_messages = (source_messages if source_messages is not None
                        else messages)
    prior_receipt_archived = False
    if prior_receipt is None:
        prior_receipt, prior_receipt_archived = resolve_prior_receipt(
            messages, task=task, source_messages=source_messages, ports=ports)
    latest_assistant = ports.latest_assistant(receipt_messages)
    from lib.tasks_pkg.compaction._continuity import (
        build_continuity, fold_continuity, observation_key, render_continuity,
        render_folded_observations)
    full_state = build_continuity(messages, task)
    state = fold_continuity(full_state, continuity_fold)
    ledger = {'version': 1, 'entries': state['observations'],
              'evidenceIds': [row['id'] for row in state['observations']],
              'truncated': False}
    ledger_error = None
    formatted_ledger = render_continuity(state)
    folded_rows = [row for row in full_state['observations']
                   if observation_key(row) in continuity_fold]
    if folded_rows:
        formatted_ledger += '\n\n' + render_folded_observations(folded_rows)

    # MESSAGE-AWARE elision: pass the budget INTO the formatter so it trims by
    # dropping middle ASSISTANT/context content while keeping EVERY real user
    # message. The old code sliced the joined string blindly,
    # which could cut a user turn in half or drop it entirely — losing user
    # instructions. See _format_messages_for_summary / _elide_to_budget.
    history_budget = max(
        4_000,
        _char_budget - len(formatted_ledger) - len(current_query or '')
        - len(anchor_text or '') - len(prior_receipt)
        - len(latest_assistant) - 900,
    )
    format_measurement = {}
    formatted = ports.format_history(
        messages, char_budget=history_budget, measurement_out=format_measurement)

    user_content = ports.user_content(
        anchor_text=anchor_text,
        latest_user_message=current_query,
        formatted_history=formatted,
        formatted_ledger=formatted_ledger,
        prior_receipt=prior_receipt,
        latest_assistant_conclusions=latest_assistant,
    )

    return SummaryInput(
        prompt=[
            {'role': 'system', 'content': ports.system_prompt},
            {'role': 'user', 'content': user_content},
        ],
        prior_receipt_archived=prior_receipt_archived,
        prior_receipt_chars=len(prior_receipt),
        latest_assistant_chars=len(latest_assistant),
        formatted_chars=len(formatted),
        full_chars=format_measurement['fullChars'],
        history_budget=history_budget,
        ledger=ledger,
        ledger_error=ledger_error,
    )


_PASS_CONTINUES = '\n[... this message continues in the next summary pass]'
_PASS_CONTINUED = '[... continued from the previous summary pass]\n'


def next_summary_pass(
    messages: list, current_query: str, *, task: dict | None,
    source_messages: list | None, anchor_text: str, ports: SummaryInputPorts,
    prior_receipt: str, capacity_tokens: int, estimate: Callable,
    continuity_fold: frozenset = frozenset(),
) -> tuple[list, list] | None:
    """Split off the longest leading slice whose one request fits capacity.

    Returns ``(slice, remainder)``. A slice ends on a message boundary and does
    not open the remainder with orphaned tool results. A single user message
    too large for any request is split verbatim at whitespace, both halves
    marked. ``None`` means not even one part fits beside the fixed evidence
    (query, anchor, receipt, latest conclusions, continuity ledger).
    """
    def fits(candidate: list) -> bool:
        composed = compose_summary_input(
            candidate, current_query, task=task, source_messages=source_messages,
            anchor_text=anchor_text, ports=ports, prior_receipt=prior_receipt,
            continuity_fold=continuity_fold)
        return estimate(composed.prompt) <= capacity_tokens

    if fits(messages):
        return list(messages), []
    low, high = 0, len(messages) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if fits(messages[:middle]):
            low = middle
        else:
            high = middle - 1
    end = low
    while 0 < end < len(messages) and messages[end].get('role') == 'tool':
        end -= 1
    end = end or low
    if end:
        return list(messages[:end]), list(messages[end:])
    split = _split_user_message(messages[0], fits)
    if split is None:
        return None
    head, tail = split
    return [head], [tail, *messages[1:]]


def _split_user_message(message, fits: Callable) -> tuple[dict, dict] | None:
    """Largest verbatim head of one user message that fits, cut at whitespace."""
    if not isinstance(message, dict) or message.get('role') != 'user':
        return None
    content = message.get('content')
    if isinstance(content, list):
        content = '\n'.join(
            block.get('text', '') for block in content
            if isinstance(block, dict) and block.get('type') == 'text'
            and isinstance(block.get('text'), str))
    if not isinstance(content, str) or len(content) < 2:
        return None
    low, high = 0, len(content) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if fits([dict(message, content=content[:middle] + _PASS_CONTINUES)]):
            low = middle
        else:
            high = middle - 1
    if not low:
        return None
    cut = max(content.rfind(' ', 0, low), content.rfind('\n', 0, low)) + 1 or low
    return (dict(message, content=content[:cut] + _PASS_CONTINUES),
            dict(message, content=_PASS_CONTINUED + content[cut:]))


def build_summary_input(
    messages: list, current_query: str, *, task: dict | None,
    source_messages: list | None, anchor_text: str, log_prefix: str,
    ports: SummaryInputPorts, prior_receipt: str | None = None,
    continuity_fold: frozenset = frozenset(),
) -> list:
    """Compose the dispatched request, then report it without mutating task state."""
    composed = compose_summary_input(
        messages, current_query, task=task, source_messages=source_messages,
        anchor_text=anchor_text, ports=ports, prior_receipt=prior_receipt,
        continuity_fold=continuity_fold)
    if composed.prior_receipt_archived:
        logger.info('%s Prior receipt recovered from compaction archive',
                    log_prefix)
    if composed.ledger_error is not None:
        logger.warning('%s evidence ledger generation failed: %s',
                       log_prefix, composed.ledger_error)

    logger.info('%s Formatting %d messages for summary (%s; '
                'prior_receipt=%s latest_assistant=%s)',
                log_prefix, len(messages), _human_size(composed.formatted_chars),
                _human_size(composed.prior_receipt_chars),
                _human_size(composed.latest_assistant_chars))
    if composed.formatted_chars < composed.full_chars:
        logger.info('%s Input elided to budget (message-aware, all user msgs '
                    'kept): %s → %s (budget %s)',
                    log_prefix, _human_size(composed.full_chars),
                    _human_size(composed.formatted_chars),
                    _human_size(composed.history_budget))
    return composed.prompt
