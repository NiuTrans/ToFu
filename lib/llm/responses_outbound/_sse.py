"""lib/llm/responses_outbound/_sse.py — Responses API SSE → OpenAI chunks.

``ResponsesSSETranslator`` is a stateful per-request translator plugged into
``SSEAccumulator`` exactly like ``AnthropicSSETranslator`` (same
``translate(data_str) -> list`` contract — the accumulator's shared
``_feed_translated`` path consumes both).

Extracted from ``lib/oauth/codex.py:CodexSSETranslator`` (2026-07-31, epic
) with four generalisations the Codex-only original lacked:

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

import copy
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
_MAX_RESPONSE_OUTPUT_INDEX = 4095
_MAX_RESPONSE_ITEM_ID_CHARS = 512
_MAX_RESPONSE_TOOL_NAME_CHARS = 512


def _usage_to_openai(usage: dict) -> dict:
    """Responses usage → OpenAI Chat Completions usage spelling."""
    if not isinstance(usage, dict) or not usage:
        return {}

    def _token_count(value: object) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    input_tokens = _token_count(usage.get('input_tokens'))
    output_tokens = _token_count(usage.get('output_tokens'))
    total_tokens = _token_count(usage.get('total_tokens'))
    if 'total_tokens' not in usage:
        total_tokens = input_tokens + output_tokens
    out = {
        'prompt_tokens': input_tokens,
        'completion_tokens': output_tokens,
        'total_tokens': total_tokens,
    }
    itd = usage.get('input_tokens_details')
    if isinstance(itd, dict):
        details = {}
        if 'cached_tokens' in itd:
            details['cached_tokens'] = _token_count(itd['cached_tokens'])
        if 'cache_write_tokens' in itd:
            cache_write_tokens = _token_count(itd['cache_write_tokens'])
            details['cache_write_tokens'] = cache_write_tokens
            out['cache_write_tokens'] = cache_write_tokens
        if details:
            out['prompt_tokens_details'] = details
    otd = usage.get('output_tokens_details')
    if isinstance(otd, dict) and 'reasoning_tokens' in otd:
        out['completion_tokens_details'] = {
            'reasoning_tokens': _token_count(otd['reasoning_tokens'])}
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
    if 'agent' not in item or item.get('agent') is None:
        return True
    agent = item.get('agent')
    if not isinstance(agent, dict):
        return False
    agent_name = agent.get('agent_name')
    if not isinstance(agent_name, str) or not agent_name:
        return False
    return (agent_name == '/root'
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
        self._ambiguous_item_ids: set[str] = set()
        self._output_index_slot: dict[int, int] = {}
        self._slot_arguments: dict[int, str] = {}
        self._slot_identity: dict[int, dict] = {}
        # ``output_item.added`` is a one-shot lifecycle event.  Its item id
        # (or, as a fallback, output_index) identifies a provider response
        # position.  Reusing that position with identical immutable fields is
        # proven transport replay; reusing it with different fields is a
        # corrupt stream.  Equal payloads at DIFFERENT positions remain
        # independent calls.
        self._function_position_slot: dict[tuple, int] = {}
        self._function_position_identity: dict[tuple, dict] = {}
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
        self._response_item_positions: dict[tuple, int] = {}
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

    @staticmethod
    def _protocol_error(message: str) -> list[dict]:
        logger.warning('[Responses] invalid SSE event: %s', message[:200])
        return [{'error': {
            'message': f'invalid Responses stream: {message}',
            'type': 'server_error',
            'http_code': '500',
        }}]

    def _terminal_response(self, event: dict, event_type: str):
        """Validate a terminal envelope before mutating translator state."""
        response = event.get('response')
        if not isinstance(response, dict):
            return None, None, self._protocol_error(
                f'{event_type}.response must be an object')
        raw_output = response.get('output', [])
        if raw_output is None:
            output = []
        elif isinstance(raw_output, list):
            output = raw_output
        else:
            return None, None, self._protocol_error(
                f'{event_type}.response.output must be an array or null')
        if any(not isinstance(item, dict) for item in output):
            return None, None, self._protocol_error(
                f'{event_type}.response.output contains a non-object item')
        return response, output, None

    def _capture_response_items(
        self, items, *, output_indices=None, authoritative=False,
    ) -> None:
        """Capture opaque replay items without collapsing response positions.

        Incremental ``output_item.done`` events are provisional and may update
        a stable item id.  A terminal response's ordered ``output`` array is
        authoritative, so it replaces that provisional view occurrence for
        occurrence. Equal payloads (and even recycled ids) at different output
        positions remain distinct protocol items.
        """
        if authoritative:
            self.response_items = [
                dict(item) for item in (items or ())
                if (isinstance(item, dict)
                    and item.get('type') in _CAPTURE_ITEM_TYPES)
            ]
            self._response_item_positions.clear()
            return
        raw_indices = (
            list(output_indices)
            if isinstance(output_indices, (list, tuple)) else [])
        for item_position, item in enumerate(items or ()):
            if (not isinstance(item, dict)
                    or item.get('type') not in _CAPTURE_ITEM_TYPES):
                continue
            saved = dict(item)
            output_index = (
                raw_indices[item_position]
                if item_position < len(raw_indices) else None)
            item_id = saved.get('id')
            if (isinstance(output_index, int)
                    and not isinstance(output_index, bool)
                    and output_index >= 0):
                position_key = ('output_index', output_index)
            elif isinstance(item_id, str) and item_id:
                position_key = ('item_id', item_id)
            else:
                position_key = None
            prior_position = (
                self._response_item_positions.get(position_key)
                if position_key is not None else None)
            if prior_position is not None:
                self.response_items[prior_position] = saved
            else:
                if position_key is not None:
                    self._response_item_positions[position_key] = len(
                        self.response_items)
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

    def _slot_for(self, event: dict) -> tuple[int | None, str | None]:
        """Resolve a call slot without guessing across explicit identities."""
        item_id = event.get('item_id')
        output_index = event.get('output_index')
        if item_id is not None and not isinstance(item_id, str):
            return None, 'item_id must be text or null'
        if isinstance(item_id, str) and len(item_id) > _MAX_RESPONSE_ITEM_ID_CHARS:
            return None, 'item_id exceeds the bounded identity size'
        if (output_index is not None
                and (not isinstance(output_index, int)
                     or isinstance(output_index, bool)
                     or not 0 <= output_index <= _MAX_RESPONSE_OUTPUT_INDEX)):
            return None, 'output_index must be a bounded integer or null'

        slots = []
        if item_id:
            if item_id in self._ambiguous_item_ids:
                if output_index is None:
                    return None, (
                        f'ambiguous function-call item_id {item_id[:80]!r}; '
                        'output_index is required')
                # The provider recycled this correlation token at several
                # response positions. Only the explicit position may route it.
            else:
                slot = self._item_slot.get(item_id)
                if slot is None:
                    return None, (
                        f'unknown function-call item_id {item_id[:80]!r}')
                slots.append(slot)
        if output_index is not None:
            slot = self._output_index_slot.get(output_index)
            if slot is None:
                return None, f'unknown function-call output_index {output_index}'
            slots.append(slot)
        if slots:
            if any(slot != slots[0] for slot in slots[1:]):
                return None, 'item_id and output_index resolve to different calls'
            return slots[0], None
        # Compatibility-only fallback for providers that omit BOTH routing
        # identities.  This is unambiguous only as "the current call"; an
        # explicit but unknown identity above is never borrowed.
        return (self._tc_count - 1 if self._tc_count > 0 else None), None

    def _complete_arguments(
        self, slot: int, complete: object, event_type: str,
    ) -> tuple[list[dict], str | None]:
        """Append only the missing suffix from an authoritative done value."""
        if not isinstance(complete, str):
            return [], f'{event_type} complete arguments must be text'
        accumulated = self._slot_arguments.get(slot, '')
        if complete == accumulated:
            return [], None
        if not complete.startswith(accumulated):
            return [], (
                f'{event_type} complete arguments disagree with streamed '
                f'prefix for slot {slot}')
        suffix = complete[len(accumulated):]
        self._slot_arguments[slot] = complete
        if not suffix:
            return [], None
        return [self._chunk(delta={'tool_calls': [{
            'index': slot,
            'function': {'arguments': suffix},
        }]})], None

    def _reconcile_terminal_function_calls(
        self, output: list[dict], event_type: str,
        *, output_indices: list[object] | None = None,
    ) -> tuple[list[dict], str | None]:
        """Validate/fill final function items against their started slots."""
        chunks: list[dict] = []
        for position, item in enumerate(output):
            if item.get('type') != 'function_call':
                continue
            output_index = (output_indices[position]
                            if output_indices is not None
                            and position < len(output_indices)
                            else position)
            route = {'item_id': item.get('id')}
            # Some compatibility providers omit output_index on start and use
            # an item id exclusively. Avoid requiring an index map in that
            # case; the stable item id is sufficient.
            if (output_index is not None
                    and output_index in self._output_index_slot):
                route['output_index'] = output_index
            slot, route_error = self._slot_for(route)
            if route_error or slot is None:
                return [], (route_error or
                            f'{event_type} function call had no started slot')
            identity = self._slot_identity.get(slot, {})
            for field in ('call_id', 'name'):
                value = item.get(field, '')
                if not isinstance(value, str):
                    return [], f'{event_type} function_call.{field} must be text'
                if value and identity.get(field) != value:
                    return [], (
                        f'{event_type} function_call.{field} changed for slot '
                        f'{slot}')
            if 'caller' in item:
                if (not identity.get('caller_present')
                        or identity.get('caller') != item.get('caller')):
                    return [], (
                        f'{event_type} function_call.caller changed for slot '
                        f'{slot}')
            if 'arguments' in item:
                completed, complete_error = self._complete_arguments(
                    slot, item.get('arguments'), event_type)
                if complete_error:
                    return [], complete_error
                chunks.extend(completed)
        return chunks, None

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

        if not isinstance(event, dict):
            return self._protocol_error('event must be an object')

        etype = event.get('type', '')
        if not isinstance(etype, str):
            return self._protocol_error('event.type must be text')
        out: list = []

        if etype == 'response.output_text.delta':
            output_index = event.get('output_index')
            if (output_index is not None
                    and (not isinstance(output_index, int)
                         or isinstance(output_index, bool)
                         or not 0 <= output_index <= _MAX_RESPONSE_OUTPUT_INDEX)):
                return self._protocol_error(
                    'output_text.delta output_index must be a bounded integer')
            agent = event.get('agent')
            if ('agent' in event and agent is not None
                    and not isinstance(agent, dict)):
                return self._protocol_error(
                    'output_text.delta agent must be an object or null')
            if isinstance(agent, dict) and (
                    not isinstance(agent.get('agent_name'), str)
                    or not agent.get('agent_name')):
                return self._protocol_error(
                    'output_text.delta agent_name must be non-empty text')
            delta = event.get('delta', '')
            if delta is None:
                delta = ''
            if not isinstance(delta, str):
                return self._protocol_error(
                    'output_text.delta delta must be text or null')
            hidden_by_agent = (isinstance(agent, dict)
                               and agent.get('agent_name') != '/root')
            if (output_index not in self._hidden_text_output_indices
                    and not hidden_by_agent):
                out.append(self._chunk(
                    delta={'content': delta}))

        elif etype == 'response.refusal.delta':
            delta = event.get('delta', '')
            if delta is None:
                delta = ''
            if not isinstance(delta, str):
                return self._protocol_error(
                    'refusal.delta delta must be text or null')
            out.append(self._chunk(delta={'content': delta}))

        elif etype in ('response.reasoning_summary_text.delta',
                       'response.reasoning_text.delta'):
            delta = event.get('delta', '')
            if delta is None:
                delta = ''
            if not isinstance(delta, str):
                return self._protocol_error(
                    f'{etype} delta must be text or null')
            out.append(self._chunk(
                delta={'reasoning_content': delta}))
            self._reasoning_open = True

        elif etype == 'response.reasoning_summary_part.added':
            # A new summary paragraph begins — separate it from the previous
            # one (no-op for the very first part).
            if self._reasoning_open:
                self._reasoning_open = False
                out.append(self._chunk(
                    delta={'reasoning_content': '\n\n'}))

        elif etype == 'response.output_item.added':
            item = event.get('item')
            if not isinstance(item, dict):
                return self._protocol_error(
                    'output_item.added item must be an object')
            self._observe_item_type(item)
            output_index = event.get('output_index')
            if (output_index is not None
                    and (not isinstance(output_index, int)
                         or isinstance(output_index, bool)
                         or not 0 <= output_index <= _MAX_RESPONSE_OUTPUT_INDEX)):
                return self._protocol_error(
                    'output_item.added output_index must be a bounded integer')
            item_agent = item.get('agent')
            if ('agent' in item and item_agent is not None
                    and (not isinstance(item_agent, dict)
                         or not isinstance(item_agent.get('agent_name'), str)
                         or not item_agent.get('agent_name'))):
                return self._protocol_error(
                    'output_item.added agent must carry a non-empty text agent_name')
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
                raw_item_id = item.get('id')
                item_id = '' if raw_item_id is None else raw_item_id
                if not isinstance(item_id, str):
                    return self._protocol_error(
                        'function_call item.id must be text or null')
                if len(item_id) > _MAX_RESPONSE_ITEM_ID_CHARS:
                    return self._protocol_error(
                        'function_call item.id exceeds the bounded identity size')
                call_id = item.get('call_id', '')
                name = item.get('name', '')
                initial_arguments = item.get('arguments', '')
                if initial_arguments is None:
                    initial_arguments = ''
                if not isinstance(call_id, str):
                    return self._protocol_error(
                        'function_call call_id must be text')
                if len(call_id) > _MAX_RESPONSE_ITEM_ID_CHARS:
                    return self._protocol_error(
                        'function_call call_id exceeds the bounded identity size')
                if not isinstance(name, str):
                    return self._protocol_error(
                        'function_call name must be text')
                if len(name) > _MAX_RESPONSE_TOOL_NAME_CHARS:
                    return self._protocol_error(
                        'function_call name exceeds the bounded identity size')
                if not isinstance(initial_arguments, str):
                    return self._protocol_error(
                        'function_call arguments must be text or null')
                caller_present = ('caller' in item
                                  and item.get('caller') is not None)
                raw_caller = item.get('caller')
                agent = item.get('agent')
                if agent is None:
                    agent = event.get('agent')
                if (agent is not None and not isinstance(agent, dict)):
                    return self._protocol_error(
                        'function_call agent must be an object or null')
                if isinstance(agent, dict) and (
                        not isinstance(agent.get('agent_name'), str)
                        or not agent.get('agent_name')):
                    return self._protocol_error(
                        'function_call agent_name must be non-empty text')
                position_key = None
                if (isinstance(output_index, int)
                        and not isinstance(output_index, bool)
                        and output_index >= 0):
                    position_key = ('output_index', output_index)
                elif item_id:
                    position_key = ('item_id', item_id)
                identity = {
                    'call_id': call_id,
                    'name': name,
                    'caller_present': caller_present,
                    'caller': raw_caller,
                    'agent': agent,
                    'initial_arguments': initial_arguments,
                }
                if position_key in self._function_position_slot:
                    if (self._function_position_identity[position_key]
                            != identity):
                        return self._protocol_error(
                            'function_call response position was reused with '
                            'different identity fields')
                    # Same lifecycle event replayed on the transport.  Do not
                    # advance the response-position counter or emit another
                    # executable shell.
                    return out
                slot = self._tc_count
                self._tc_count += 1
                if position_key is not None:
                    self._function_position_slot[position_key] = slot
                    self._function_position_identity[position_key] = copy.deepcopy(
                        identity)
                if item_id:
                    prior_item_slot = self._item_slot.get(item_id)
                    if item_id in self._ambiguous_item_ids:
                        pass
                    elif prior_item_slot is None:
                        self._item_slot[item_id] = slot
                    elif prior_item_slot != slot:
                        # Recycled item ids cannot route deltas on their own.
                        # Keep both output positions and require output_index.
                        self._item_slot.pop(item_id, None)
                        self._ambiguous_item_ids.add(item_id)
                if output_index is not None:
                    prior_slot = self._output_index_slot.get(output_index)
                    if prior_slot is not None and prior_slot != slot:
                        return self._protocol_error(
                            'function_call output_index was reused by another call')
                    self._output_index_slot[output_index] = slot
                self._slot_arguments[slot] = initial_arguments
                self._slot_identity[slot] = copy.deepcopy(identity)
                if self.tool_name_reverse:
                    name = self.tool_name_reverse.get(name, name)
                tool_call = {
                    'index': slot,
                    'id': call_id,
                    'type': 'function',
                    'function': {'name': name,
                                 'arguments': initial_arguments},
                }
                if caller_present:
                    tool_call['caller'] = (dict(raw_caller)
                                           if isinstance(raw_caller, dict)
                                           else raw_caller)
                if isinstance(agent, dict) and agent.get('agent_name'):
                    caller = tool_call.get('caller')
                    if 'caller' not in tool_call:
                        caller = {'type': 'multi_agent'}
                        tool_call['caller'] = caller
                    if isinstance(caller, dict):
                        caller['agent_name'] = str(agent['agent_name'])
                out.append(self._chunk(delta={'tool_calls': [tool_call]}))

        elif etype == 'response.function_call_arguments.delta':
            delta = event.get('delta', '')
            if delta is None:
                delta = ''
            if not isinstance(delta, str):
                return self._protocol_error(
                    'function_call_arguments.delta delta must be text or null')
            slot, route_error = self._slot_for(event)
            if route_error:
                return self._protocol_error(
                    f'function_call_arguments.delta {route_error}')
            if slot is None:
                return self._protocol_error(
                    'function_call_arguments.delta arrived before a function call')
            self._slot_arguments[slot] = (
                self._slot_arguments.get(slot, '') + delta)
            out.append(self._chunk(delta={'tool_calls': [{
                'index': slot,
                'function': {'arguments': delta}}]}))

        elif etype == 'response.function_call_arguments.done':
            slot, route_error = self._slot_for(event)
            if route_error:
                return self._protocol_error(
                    f'function_call_arguments.done {route_error}')
            if slot is None:
                return self._protocol_error(
                    'function_call_arguments.done arrived before a function call')
            completed, complete_error = self._complete_arguments(
                slot, event.get('arguments'), etype)
            if complete_error:
                return self._protocol_error(complete_error)
            out.extend(completed)

        elif etype == 'response.output_item.done':
            item = event.get('item')
            if not isinstance(item, dict):
                return self._protocol_error(
                    'output_item.done item must be an object')
            self._observe_item_type(item)
            if item.get('type') == 'function_call':
                completed, complete_error = (
                    self._reconcile_terminal_function_calls(
                        [item], etype,
                        output_indices=[event.get('output_index')]))
                if complete_error:
                    return self._protocol_error(complete_error)
                out.extend(completed)
            self._capture_response_items(
                [item], output_indices=[event.get('output_index')])

        elif etype == 'response.completed':
            resp, response_output, error = self._terminal_response(event, etype)
            if error:
                return error
            self.response_id = str(resp.get('id') or '')
            self.response_output = [dict(item) for item in response_output
                                    if isinstance(item, dict)]
            for item in response_output:
                self._observe_item_type(item)
            self._capture_response_items(response_output, authoritative=True)
            completed, complete_error = self._reconcile_terminal_function_calls(
                response_output, etype)
            if complete_error:
                return self._protocol_error(complete_error)
            out.extend(completed)
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
            resp, response_output, error = self._terminal_response(event, etype)
            if error:
                return error
            self.response_id = str(resp.get('id') or '')
            self.response_output = [dict(item) for item in response_output
                                    if isinstance(item, dict)]
            for item in response_output:
                self._observe_item_type(item)
            self._capture_response_items(response_output, authoritative=True)
            completed, complete_error = self._reconcile_terminal_function_calls(
                response_output, etype)
            if complete_error:
                return self._protocol_error(complete_error)
            out.extend(completed)
            details = resp.get('incomplete_details')
            if not isinstance(details, dict):
                return self._protocol_error(
                    'response.incomplete incomplete_details must be an object')
            reason = details.get('reason', '')
            if not isinstance(reason, str):
                return self._protocol_error(
                    'response.incomplete reason must be text')
            if reason != 'max_output_tokens':
                # content_filter & friends are failures, not finishes.
                out.append({'error': {
                    'message': f'response.incomplete: {reason or "missing reason"}',
                    'type': reason or 'server_error',
                    'http_code': _ERROR_HTTP.get(reason, '')}})
            else:
                finish = 'length'
                usage = _usage_to_openai(resp.get('usage') or {})
                if _program_needs_followup(response_output):
                    usage['_program_pending'] = True
                out.append(self._chunk(finish_reason=finish,
                                       usage=usage or None))
            out.append('[DONE]')

        elif etype in ('response.failed', 'response.error'):
            resp = event.get('response')
            if resp is not None and not isinstance(resp, dict):
                return self._protocol_error(
                    f'{etype}.response must be an object or null')
            err = ((resp or {}).get('error') or event.get('error') or {})
            if not isinstance(err, dict):
                err = {}
            raw_code = err.get('code')
            raw_message = err.get('message')
            code = raw_code if isinstance(raw_code, str) and raw_code else etype
            message = (raw_message
                       if isinstance(raw_message, str) and raw_message else etype)
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
