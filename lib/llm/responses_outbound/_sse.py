"""lib/llm/responses_outbound/_sse.py — Responses API SSE → OpenAI chunks.

``ResponsesSSETranslator`` is a stateful per-request translator plugged into
``SSEAccumulator`` exactly like ``AnthropicSSETranslator`` (same
``translate(data_str) -> list`` contract — the accumulator's shared
``_feed_translated`` path consumes both).

Extracted from ``lib/oauth/codex.py:CodexSSETranslator`` (2026-07-31, epic
pt_b7a29ea7) with four generalisations the Codex-only original lacked:

  1. **``response.reasoning_text.delta``** — DeepSeek's reasoning channel
     (no summary variant), mapped to ``reasoning_content`` like summaries.
  2. **item_id routing** — parallel function calls interleave their
     ``function_call_arguments.delta`` events; each delta is routed by its
     ``item_id`` to the slot allocated at ``output_item.added`` (the old
     "current index" routing concatenated parallel calls into one).
  3. **terminal failures are errors, not silence** — ``response.failed`` /
     ``response.error`` / ``response.incomplete``(non-token reasons) emit
     an OpenAI-shaped ``{'error': …}`` chunk so the accumulator's shared
     ``_handle_sse_error`` classifier (429 → RateLimitError, 5xx →
     RetryableAPIError, …) decides retry/failover. The old path dropped
     them and the stream ended EMPTY — the '无结果' failure shape.
  4. **usage details** — cache read/write and reasoning token counts are
     carried into the OpenAI usage spelling (cost accounting depends on them).
  5. **opaque state** — completed ``reasoning`` / ``compaction`` output items
     are retained for stateless replay on the next Responses request.

Terminal events (``response.completed`` / ``response.incomplete``) emit
the finish chunk + the ``'[DONE]'`` sentinel — a Responses stream has no
``[DONE]`` frame of its own, so the sentinel is synthesised here.
"""

from __future__ import annotations

import json
import time

from lib.log import get_logger
from lib.llm.responses_outbound._to_responses import (
    _REPLAY_RESPONSE_ITEM_TYPES,
)

logger = get_logger(__name__)

__all__ = ['ResponsesSSETranslator']

#: Responses error ``code`` → the HTTP status the shared SSE classifier
#: understands (``_handle_sse_error`` reads ``http_code`` first).
_ERROR_HTTP = {
    'rate_limit_exceeded': '429',
    'insufficient_quota': '429',
    'server_error': '500',
    'overloaded': '529',
}

_CAPTURE_ITEM_TYPES = _REPLAY_RESPONSE_ITEM_TYPES | frozenset({'compaction'})
_KNOWN_IGNORED_EVENTS = frozenset({
    'response.created', 'response.in_progress', 'response.queued',
    'response.content_part.added', 'response.content_part.done',
    'response.output_text.done', 'response.refusal.done',
    'response.reasoning_summary_part.done',
    'response.reasoning_summary_text.done', 'response.reasoning_text.done',
    'response.function_call_arguments.done',
})
_KNOWN_PROGRESS_PREFIXES = (
    'response.tool_search_call.', 'response.web_search_call.',
    'response.file_search_call.', 'response.code_interpreter_call.',
    'response.shell_call.', 'response.computer_call.',
    'response.multi_agent_',
)


def _usage_to_openai(usage: dict) -> dict:
    """Responses usage → OpenAI Chat Completions usage spelling."""
    if not isinstance(usage, dict) or not usage:
        return {}
    out = {
        'prompt_tokens': usage.get('input_tokens', 0),
        'completion_tokens': usage.get('output_tokens', 0),
        'total_tokens': usage.get(
            'total_tokens',
            usage.get('input_tokens', 0) + usage.get('output_tokens', 0)),
    }
    itd = usage.get('input_tokens_details')
    if isinstance(itd, dict):
        details = {}
        if 'cached_tokens' in itd:
            details['cached_tokens'] = itd['cached_tokens']
        if 'cache_write_tokens' in itd:
            details['cache_write_tokens'] = itd['cache_write_tokens']
            out['cache_write_tokens'] = itd['cache_write_tokens']
        if details:
            out['prompt_tokens_details'] = details
    otd = usage.get('output_tokens_details')
    if isinstance(otd, dict) and 'reasoning_tokens' in otd:
        out['completion_tokens_details'] = {
            'reasoning_tokens': otd['reasoning_tokens']}
    return out


def _program_needs_followup(output) -> bool:
    """A program_output without a final message requires another response."""
    items = [item for item in (output or []) if isinstance(item, dict)]
    return (any(item.get('type') == 'program_output' for item in items)
            and not any(item.get('type') == 'message' for item in items))


def _multi_agent_message_is_user_visible(item: dict) -> bool:
    """Return whether a Responses message belongs on the user surface.

    Ordinary Responses messages have no ``agent`` attribution and remain
    visible.  Native Multi-agent responses can contain root commentary and
    subagent messages; the public result is only the root ``final_answer``.
    """
    agent = item.get('agent')
    if not isinstance(agent, dict) or not agent.get('agent_name'):
        return True
    return (str(agent['agent_name']) == '/root'
            and item.get('phase') == 'final_answer')


class ResponsesSSETranslator:
    """Translate Responses-API SSE events into OpenAI chunk dicts.

    Usage::

        translator = ResponsesSSETranslator(model='deepseek-v4-flash')
        for chunk in translator.translate(raw_data_str):
            ...  # OpenAI-shaped chat.completion.chunk dicts (+ '[DONE]')
    """

    def __init__(self, model: str = ''):
        self.model = model
        # Function-call slots: how many function_call items have been seen
        # (the OpenAI chunk's ``index``), and the item_id → slot map for
        # routing argument deltas of PARALLEL calls.
        self._tc_count = 0
        self._item_slot: dict = {}
        # Multi-agent streams interleave root and subagent text.  The API's
        # output_index is the stable routing key for deciding which deltas
        # belong on the user-visible assistant surface.
        self._hidden_text_output_indices: set[int] = set()
        # Per-request {truncated: original} tool-name map, stamped by the
        # caller from the request converter's second return value — the
        # model echoes the TRUNCATED name and the executor's lookup would
        # miss without the restore (mirrors AnthropicSSETranslator's
        # ``tool_name_reverse``).
        self.tool_name_reverse: dict = {}
        # Final Responses output items that must be replayed when store=false.
        # SSEAccumulator copies this list onto the canonical assistant message.
        self.response_items: list[dict] = []
        self.response_id = ''
        self.response_output: list[dict] = []
        self.unknown_event_types: set[str] = set()
        self.unknown_item_types: set[str] = set()
        # Reasoning-summary paragraph tracking: OpenAI emits a summary as N
        # parts, each a markdown headline ('**…**'), and the text deltas
        # carry NO separator — concatenating parts fuses adjacent headlines
        # into '**A****B**'. Part/item boundaries re-insert the '\n\n'.
        # ``_reasoning_open`` = unseparated reasoning text since the last
        # boundary, so an item boundary followed by its first part boundary
        # emits ONE separator, not two.
        self._reasoning_open = False

    def _capture_response_items(self, items) -> None:
        """Upsert opaque replay items by their stable id."""
        for item in items or ():
            if (not isinstance(item, dict)
                    or item.get('type') not in _CAPTURE_ITEM_TYPES):
                continue
            saved = dict(item)
            item_id = saved.get('id')
            if item_id:
                for index, prior in enumerate(self.response_items):
                    if prior.get('id') == item_id:
                        self.response_items[index] = saved
                        break
                else:
                    self.response_items.append(saved)
            elif saved not in self.response_items:
                self.response_items.append(saved)

    def _observe_item_type(self, item) -> None:
        if not isinstance(item, dict):
            return
        item_type = str(item.get('type') or '')
        if (not item_type
                or item_type in _CAPTURE_ITEM_TYPES
                or item_type in ('message', 'function_call')):
            return
        if item_type not in self.unknown_item_types:
            self.unknown_item_types.add(item_type)
            logger.warning('[Responses] unhandled output item type=%s; '
                           'preserving the stream but not replaying the item',
                           item_type)

    # ──────────────────────────────────────────────────────────

    def _chunk(self, delta: dict | None = None,
               finish_reason: str | None = None,
               usage: dict | None = None) -> dict:
        chunk = {
            'id': 'chatcmpl-responses',
            'object': 'chat.completion.chunk',
            'created': int(time.time()),
            'model': self.model,
            'choices': [{'index': 0, 'delta': delta or {},
                         'finish_reason': finish_reason}],
        }
        if usage:
            chunk['usage'] = usage
        return chunk

    def _slot_for(self, event: dict):
        """Resolve which tool-call slot an arguments delta belongs to."""
        item_id = event.get('item_id')
        if item_id:
            slot = self._item_slot.get(item_id)
            if slot is not None:
                return slot
            logger.debug('[Responses] arguments delta for unknown item_id %s '
                         '— falling back to current slot', item_id)
        return self._tc_count - 1 if self._tc_count > 0 else None

    # ──────────────────────────────────────────────────────────

    def translate(self, data_str: str) -> list:
        """Translate one Responses-API SSE ``data:`` payload.

        Returns a list of OpenAI-shaped chunk dicts (plus the ``'[DONE]'``
        sentinel after a terminal event). Unknown event types are ignored
        by design — the Responses event vocabulary is large (56 types) and
        grows; unrecognised structure must never kill the stream.
        """
        try:
            event = json.loads(data_str)
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Responses] SSE JSON parse failed: %s', e)
            return []

        etype = event.get('type', '')
        out: list = []

        if etype == 'response.output_text.delta':
            output_index = event.get('output_index')
            agent = event.get('agent')
            hidden_by_agent = (isinstance(agent, dict)
                               and agent.get('agent_name') != '/root')
            if (output_index not in self._hidden_text_output_indices
                    and not hidden_by_agent):
                out.append(self._chunk(
                    delta={'content': event.get('delta', '')}))

        elif etype == 'response.refusal.delta':
            out.append(self._chunk(delta={'content': event.get('delta', '')}))

        elif etype in ('response.reasoning_summary_text.delta',
                       'response.reasoning_text.delta'):
            out.append(self._chunk(
                delta={'reasoning_content': event.get('delta', '')}))
            self._reasoning_open = True

        elif etype == 'response.reasoning_summary_part.added':
            # A new summary paragraph begins — separate it from the previous
            # one (no-op for the very first part).
            if self._reasoning_open:
                self._reasoning_open = False
                out.append(self._chunk(
                    delta={'reasoning_content': '\n\n'}))

        elif etype == 'response.output_item.added':
            item = event.get('item') or {}
            self._observe_item_type(item)
            output_index = event.get('output_index')
            if (item.get('type') == 'message'
                    and isinstance(output_index, int)
                    and not _multi_agent_message_is_user_visible(item)):
                self._hidden_text_output_indices.add(output_index)
            if (item.get('type') == 'reasoning'
                    and self._reasoning_open):
                # A second reasoning block in one response — same boundary.
                self._reasoning_open = False
                out.append(self._chunk(
                    delta={'reasoning_content': '\n\n'}))
            if item.get('type') == 'function_call':
                slot = self._tc_count
                self._tc_count += 1
                item_id = item.get('id') or ''
                if item_id:
                    self._item_slot[item_id] = slot
                name = item.get('name', '')
                if self.tool_name_reverse:
                    name = self.tool_name_reverse.get(name, name)
                tool_call = {
                    'index': slot,
                    'id': item.get('call_id', ''),
                    'type': 'function',
                    'function': {'name': name,
                                 'arguments': ''},
                }
                if isinstance(item.get('caller'), dict):
                    tool_call['caller'] = dict(item['caller'])
                agent = item.get('agent') or event.get('agent')
                if isinstance(agent, dict) and agent.get('agent_name'):
                    tool_call.setdefault('caller', {
                        'type': 'multi_agent',
                    })['agent_name'] = str(agent['agent_name'])
                out.append(self._chunk(delta={'tool_calls': [tool_call]}))

        elif etype == 'response.function_call_arguments.delta':
            slot = self._slot_for(event)
            if slot is not None:
                out.append(self._chunk(delta={'tool_calls': [{
                    'index': slot,
                    'function': {'arguments': event.get('delta', '')}}]}))

        elif etype == 'response.output_item.done':
            item = event.get('item') or {}
            self._observe_item_type(item)
            self._capture_response_items([item])

        elif etype == 'response.completed':
            resp = event.get('response') or {}
            response_output = resp.get('output') or []
            self.response_id = str(resp.get('id') or '')
            self.response_output = [dict(item) for item in response_output
                                    if isinstance(item, dict)]
            for item in response_output:
                self._observe_item_type(item)
            self._capture_response_items(response_output)
            finish = 'tool_calls' if self._tc_count > 0 else 'stop'
            usage = _usage_to_openai(resp.get('usage') or {})
            if _program_needs_followup(response_output):
                # OpenAI may deliver the final assistant message in the next
                # response after program_output. The orchestrator recognizes
                # this private marker and replays the opaque item once more.
                usage['_program_pending'] = True
            out.append(self._chunk(finish_reason=finish,
                                   usage=usage or None))
            out.append('[DONE]')

        elif etype == 'response.incomplete':
            resp = event.get('response') or {}
            response_output = resp.get('output') or []
            self.response_id = str(resp.get('id') or '')
            self.response_output = [dict(item) for item in response_output
                                    if isinstance(item, dict)]
            for item in response_output:
                self._observe_item_type(item)
            self._capture_response_items(response_output)
            reason = (resp.get('incomplete_details') or {}).get('reason', '')
            if reason and reason != 'max_output_tokens':
                # content_filter & friends are failures, not finishes.
                out.append({'error': {
                    'message': f'response.incomplete: {reason}',
                    'type': reason,
                    'http_code': _ERROR_HTTP.get(reason, '')}})
            else:
                finish = 'length' if reason == 'max_output_tokens' else (
                    'tool_calls' if self._tc_count > 0 else 'stop')
                usage = _usage_to_openai(resp.get('usage') or {})
                if _program_needs_followup(response_output):
                    usage['_program_pending'] = True
                out.append(self._chunk(finish_reason=finish,
                                       usage=usage or None))
            out.append('[DONE]')

        elif etype in ('response.failed', 'response.error'):
            resp = event.get('response') or {}
            err = resp.get('error') or event.get('error') or {}
            code = err.get('code', '') or etype
            message = err.get('message', '') or etype
            out.append({'error': {
                'message': f'{code}: {message}',
                'type': code,
                'http_code': _ERROR_HTTP.get(code, '')}})

        elif (etype and etype not in _KNOWN_IGNORED_EVENTS
              and not any(etype.startswith(prefix)
                          for prefix in _KNOWN_PROGRESS_PREFIXES)):
            if etype not in self.unknown_event_types:
                self.unknown_event_types.add(etype)
                logger.warning('[Responses] unhandled SSE event type=%s; '
                               'stream remains active', etype)

        # Everything else — response.created / in_progress / queued,
        # content_part.*, output_text.done, reasoning_summary_part.done,
        # function_call_arguments.done, reasoning_summary_text.done,
        # web_search_call.*, … — carries no delta we need.
        return out
