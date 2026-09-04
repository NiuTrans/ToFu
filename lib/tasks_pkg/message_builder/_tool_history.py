"""Tool-history restoration — replay interrupted tool rounds on "Continue…".

Extracted from ``orchestrator.py`` (see the package ``__init__`` for the
facade). Isolates :func:`inject_tool_history`, which splices previously
completed assistant→tool rounds back into the ``messages`` list.
"""

import copy
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from lib.log import get_logger
from lib.tool_round_replay import normalize_replay_tool_arguments
from lib.error_envelope import make_envelope
from lib.llm_errors import RequestScopedError
from lib.model_info import (
    model_requires_thinking_signature_replay,
    model_requires_thought_signature_on_tool_calls,
)
from lib.tool_caller_identity import (
    MAX_TOOL_CALLER_ID_CHARS,
    normalize_tool_caller,
    tool_caller_authority,
)

logger = get_logger(__name__)

_MAX_REPLAY_ID_CHARS = MAX_TOOL_CALLER_ID_CHARS
_MAX_REPLAY_TOOL_NAME_CHARS = 512


@dataclass(frozen=True)
class PreparedContinueToolHistory:
    """Validated provider messages detached from the request checkpoint."""

    model: str
    messages: tuple[dict[str, Any], ...]
    injected_calls: int
    injected_rounds: int
    thinking_blocks_attached: int
    thought_signatures_attached: int


class ContinueToolHistoryProtocolError(RequestScopedError):
    """The supplied checkpoint cannot form an exact causal tool history."""

    def __init__(self, detail: str):
        super().__init__(detail, status_code=422)
        self._user_message = make_envelope(
            'bad_request',
            message=('Continue checkpoint is malformed / '
                     '继续执行检查点格式错误'),
            detail=detail,
            context='continue-tool-history',
            source='task-message-builder',
            retryable=False,
            hint=('Regenerate from the Turn or start a fresh turn. No partial '
                  'checkpoint was sent to the model. / '
                  '请从该轮重新生成或新建一轮；后端未向模型发送任何残缺检查点。'),
        )


def _tool_history_json_text(value) -> str | None:
    """Return strict JSON text for a structured replay value, if possible."""
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        return None


# ══════════════════════════════════════════════════════════════
#  Continue / Resume: per-provider capability matrix
# ══════════════════════════════════════════════════════════════
#
# When the user clicks "Continue" on an interrupted assistant turn we
# replay the already-completed tool rounds so the model can pick up
# right after the last tool result.  What each provider's API accepts
# on that replayed assistant turn:
#
#   Provider            | tool_use replay | thinking replay          | Prefill
#   --------------------+-----------------+--------------------------+---------
#   Anthropic (Claude)  | required        | thinking{} block WITH    | NO
#                       | tool_calls +    |   opaque `signature` —   | (API rejects
#                       | tool results    |   mandatory when tools   |  trailing
#                       |                 |   ran with extended      |  assistant
#                       |                 |   thinking; else API 400 |  turn)
#   Gemini (openai-     | required        | extra_content.google.    | tolerated
#   compat proxy)       |                 |   thought_signature on   | (best-effort)
#   OpenAI / DeepSeek / | standard        | NOT re-accepted          | tolerated
#   Qwen / GLM / Kimi / | tool_calls +    |   (reasoning_content     | (best-effort
#   Doubao / MiniMax    | tool role msgs  |   stripped server-side)  |  — the model
#   ERNIE / LongCat     |                 |                          |  may or may
#                       |                 |                          |  not honour)
#
# Consequence for this builder: we ALWAYS prepare tool_calls + tool
# results (they're universally accepted), but we OPTIONALLY attach
# thinking / thought_signature / extra_content blocks only for the
# providers whose API actually consumes them.  Unsupported providers
# get the plain shape — exactly what they got before this change.
#
# Anthropic's "no assistant prefill" restriction is a hard ceiling:
# free-form text the model wrote BETWEEN tool batches can never be
# re-injected as a prefill against Claude.  We therefore do not try.

def prepare_tool_history(cfg, task, model) -> PreparedContinueToolHistory:
    """Validate and build interrupted tool history without mutating messages.

    When the frontend sends a continuation request it includes a
    ``toolHistory`` list in the config.  Each entry describes one
    assistant→tool round that happened before the interruption.  This
    builder validates and detaches those rounds; the injection wrapper later
    appends them to *messages* so the LLM sees the full conversation context.

    Each ``toolHistory`` entry accepts (all optional except ``toolCalls``):

        {
          'assistantContent': str,    # text the model wrote alongside calls
          'thinking': str,            # reasoning trace (Claude-family only)
          'thinkingSignature': str,   # opaque signature for thinking block
          'toolCalls': [
              {'id', 'name', 'arguments',
               'extraContent': {...}  # Gemini thought_signature lives here
              }, ...
          ],
          'toolResults': [{'tool_call_id', 'content'}, ...],
        }

    Per-provider behaviour:

    * Claude extended-thinking models → the assistant turn is emitted with
      a ``thinking`` block containing the prior reasoning and its
      ``signature``.  Required by the Messages API when tools were used
      with extended thinking, otherwise the follow-up call 400s.
    * Gemini → each replayed ``tool_call`` entry is annotated with
      ``extra_content.google.thought_signature`` exactly as the live
      stream produced it.
    * All other OpenAI-compatible providers → the standard shape only.
      Extra fields are ignored silently rather than sent, since those
      APIs strip vendor extensions server-side anyway.

    Parameters
    ----------
    cfg : dict
        Task configuration dict (reads ``cfg['toolHistory']``).
    task : dict
        Live task dict (used for logging ``task['id']``).
    model : str
        Current model identifier (used for logging + capability probes).

    Returns
    -------
    PreparedContinueToolHistory
        Detached provider messages plus exact round/call occurrence counts.
        No caller-owned object is mutated, including on protocol failure.
    """
    tool_history = (
        cfg.get('toolHistory')
        if isinstance(cfg, dict) and 'toolHistory' in cfg else None
    )
    if tool_history is None or tool_history == []:
        return PreparedContinueToolHistory(str(model), (), 0, 0, 0, 0)
    if not isinstance(tool_history, list):
        raise ContinueToolHistoryProtocolError(
            'toolHistory must be a list, got '
            f'{type(tool_history).__name__}')

    task = task if isinstance(task, dict) else {}
    tid = str(task.get('id') or '')[:8]
    conv_id_short = str(task.get('convId') or '')[:8]

    # Per-provider capability gates — None→Python-bool so log output is clean.
    _wants_thinking_block = model_requires_thinking_signature_replay(model)
    _wants_thought_sig = model_requires_thought_signature_on_tool_calls(model)

    injected_msgs = []
    injected = 0
    injected_calls = 0
    _thinking_blocks_attached = 0
    _thought_sigs_attached = 0
    for round_position, th_round in enumerate(tool_history):
        if not isinstance(th_round, dict):
            raise ContinueToolHistoryProtocolError(
                f'toolHistory[{round_position}] must be an object')
        for text_field in (
            'assistantContent', 'thinking', 'thinkingSignature',
        ):
            if (text_field in th_round
                    and th_round.get(text_field) is not None
                    and not isinstance(th_round.get(text_field), str)):
                raise ContinueToolHistoryProtocolError(
                    f'toolHistory[{round_position}].{text_field} must be a string')
        raw_tc_list = th_round.get('toolCalls', [])
        raw_tr_list = th_round.get('toolResults', [])
        if not isinstance(raw_tc_list, list) \
                or not isinstance(raw_tr_list, list):
            raise ContinueToolHistoryProtocolError(
                f'toolHistory[{round_position}] calls/results must be lists')
        tc_list = raw_tc_list
        tr_list = raw_tr_list
        if not tc_list:
            if tr_list:
                raise ContinueToolHistoryProtocolError(
                    f'toolHistory[{round_position}] has results without calls')
            continue

        # Provider call ids are run-local correlation tokens, not global
        # identities. Pair result receipts by occurrence before validating the
        # envelope. Any malformed occurrence rejects the checkpoint; it can
        # neither shift its result onto a later duplicate nor be skipped as a
        # fictitious causal gap.
        tr_by_id = defaultdict(deque)
        for result_position, tool_result_entry in enumerate(tr_list):
            if not isinstance(tool_result_entry, dict):
                raise ContinueToolHistoryProtocolError(
                    f'toolHistory[{round_position}].toolResults['
                    f'{result_position}] must be an object')
            result_id = tool_result_entry.get('tool_call_id')
            if (not isinstance(result_id, str) or not result_id
                    or len(result_id) > _MAX_REPLAY_ID_CHARS):
                raise ContinueToolHistoryProtocolError(
                    f'toolHistory[{round_position}].toolResults['
                    f'{result_position}] has invalid identity')
            tr_by_id[result_id].append(tool_result_entry)

        retained = []
        for call_position, tc in enumerate(tc_list):
            if not isinstance(tc, dict):
                raise ContinueToolHistoryProtocolError(
                    f'toolHistory[{round_position}].toolCalls['
                    f'{call_position}] must be an object')
            tc_id = tc.get('id')
            matching_result = None
            if isinstance(tc_id, str) and tr_by_id.get(tc_id):
                matching_result = tr_by_id[tc_id].popleft()

            tool_name = tc.get('name')
            if (not isinstance(tc_id, str) or not tc_id
                    or len(tc_id) > _MAX_REPLAY_ID_CHARS
                    or not isinstance(tool_name, str) or not tool_name
                    or len(tool_name) > _MAX_REPLAY_TOOL_NAME_CHARS):
                raise ContinueToolHistoryProtocolError(
                    f'toolHistory[{round_position}].toolCalls['
                    f'{call_position}] has invalid identity or name')
            if matching_result is None:
                raise ContinueToolHistoryProtocolError(
                    f'toolHistory[{round_position}].toolCalls['
                    f'{call_position}] has no occurrence-matched result')

            extra_content = tc.get('extraContent') \
                if 'extraContent' in tc else None
            if extra_content is not None and (
                not isinstance(extra_content, dict)
                or _tool_history_json_text(extra_content) is None
            ):
                raise ContinueToolHistoryProtocolError(
                    f'toolHistory[{round_position}].toolCalls['
                    f'{call_position}] has invalid extraContent')

            call_caller, call_caller_error = normalize_tool_caller(
                tc.get('caller') if 'caller' in tc else None)
            result_caller, result_caller_error = normalize_tool_caller(
                (matching_result.get('caller')
                 if matching_result is not None
                 and 'caller' in matching_result else None))
            if call_caller_error or result_caller_error:
                raise ContinueToolHistoryProtocolError(
                    f'toolHistory[{round_position}].toolCalls['
                    f'{call_position}] has invalid caller authority')
            if (call_caller is not None and result_caller is not None
                    and tool_caller_authority(call_caller)
                    != tool_caller_authority(result_caller)):
                raise ContinueToolHistoryProtocolError(
                    f'toolHistory[{round_position}].toolCalls['
                    f'{call_position}] caller authorities disagree')

            args_text, arguments_repaired = normalize_replay_tool_arguments(
                tc.get('arguments'))
            if args_text is None:
                raise ContinueToolHistoryProtocolError(
                    f'toolHistory[{round_position}].toolCalls['
                    f'{call_position}] has invalid arguments')
            raw_result_content = matching_result.get('content')
            if isinstance(raw_result_content, str):
                result_content = raw_result_content
            elif raw_result_content is None:
                raise ContinueToolHistoryProtocolError(
                    f'toolHistory[{round_position}].toolResults has a missing '
                    f'result for call position {call_position}')
            else:
                result_content = _tool_history_json_text(raw_result_content)
                if result_content is None:
                    raise ContinueToolHistoryProtocolError(
                        f'toolHistory[{round_position}].toolResults has an '
                        f'unserializable result for call position {call_position}')

            retained.append((
                tc,
                call_caller or result_caller,
                args_text,
                arguments_repaired,
                result_content,
            ))

        unmatched_results = sum(len(queue) for queue in tr_by_id.values())
        if unmatched_results:
            raise ContinueToolHistoryProtocolError(
                f'toolHistory[{round_position}] has {unmatched_results} '
                'unmatched result occurrence(s)')
        if not retained:
            continue

        # ── Build occurrence-paired calls and results together ──
        built_tool_calls = []
        built_tool_results = []
        for tc, caller, _args_str, _args_repaired, tc_content in retained:
            # Defense-in-depth: if a checkpoint stored an args string that
            # isn't valid JSON (e.g. weak model emitted ``\d`` instead of
            # ``\\d``), replay it as ``'{}'`` so the upstream gateway
            # doesn't HTTP 400 ``invalid function arguments json string``.
            # The matching tool result still tells the model the original
            # call failed. See orchestrator.py:1364 (live sanitizer) and
            # the May 2026 incident memory.
            if _args_repaired:
                logger.warning(
                    '[Task %s] conv=%s Replaying tool_call %s with sanitized '
                    'arguments (original was malformed JSON, %d chars)',
                    tid, conv_id_short, tc.get('name', '?'),
                    len(_args_str) if isinstance(_args_str, str) else 0)
                _args_str = '{}'
            tc_entry = {
                'id': tc['id'],
                'type': 'function',
                'function': {'name': tc['name'], 'arguments': _args_str},
            }
            # Gemini: echo back thought_signature or the API 400s.
            extra = tc.get('extraContent')
            if (_wants_thought_sig and isinstance(extra, dict) and extra
                    and _tool_history_json_text(extra) is not None):
                tc_entry['extra_content'] = copy.deepcopy(dict(extra))
                _thought_sigs_attached += 1
            elif extra and not _wants_thought_sig:
                logger.debug(
                    '[Task %s] conv=%s model=%s — dropping extraContent on '
                    'replayed tool_call (%s) since provider does not require it',
                    tid, conv_id_short, model, tc.get('name', '?'),
                )
            elif extra and _wants_thought_sig:
                logger.warning(
                    '[Task %s] conv=%s model=%s — dropping malformed '
                    'extraContent on replayed tool_call (%s)',
                    tid, conv_id_short, model, tc.get('name', '?'))
            if caller is not None:
                tc_entry['caller'] = copy.deepcopy(dict(caller))
            built_tool_calls.append(tc_entry)

            tool_result = {
                'role': 'tool',
                'tool_call_id': tc['id'],
                'content': tc_content,
            }
            if caller is not None:
                tool_result['caller'] = copy.deepcopy(dict(caller))
            built_tool_results.append(tool_result)

        # Build assistant message with tool_calls
        clean_assistant = {'role': 'assistant', 'tool_calls': built_tool_calls}
        ac = th_round.get('assistantContent')
        if isinstance(ac, str) and ac:
            clean_assistant['content'] = ac

        # Anthropic extended-thinking: re-emit the thinking block with its
        # opaque signature so the API can verify tool-use continuity.
        # Without this, Claude 4.x with extended thinking returns HTTP 400
        # ("Expected `thinking` block with signature") on the follow-up
        # request that immediately calls a tool.
        raw_th_text = th_round.get('thinking')
        raw_th_sig = th_round.get('thinkingSignature')
        th_text = raw_th_text if isinstance(raw_th_text, str) else ''
        th_sig = raw_th_sig if isinstance(raw_th_sig, str) else ''
        if _wants_thinking_block and th_text and th_sig:
            # Structured block format — Anthropic Messages proxy translates
            # OpenAI-shape back to native blocks when it sees this list.
            clean_assistant['reasoning_content'] = th_text
            # `signature` is the field name the Anthropic API expects on
            # the thinking block — keep the wire name stable for the proxy.
            clean_assistant['thinking_signature'] = th_sig
            _thinking_blocks_attached += 1
        elif th_text and not _wants_thinking_block:
            logger.debug(
                '[Task %s] conv=%s model=%s — dropping %d chars of checkpoint '
                'thinking on replay; provider does not accept thinking replay',
                tid, conv_id_short, model, len(th_text),
            )
        elif _wants_thinking_block and th_text and not th_sig:
            # Claude: text without signature can't be replayed — the API
            # will still accept the message without a thinking block, but
            # continuity degrades to "fresh reasoning".  Warn once.
            logger.warning(
                '[Task %s] conv=%s model=%s — checkpoint has %d chars of '
                'thinking but NO signature; not re-injecting (Claude would '
                'reject without signature). This is a lossy continuation.',
                tid, conv_id_short, model, len(th_text),
            )

        injected_msgs.append(clean_assistant)
        injected_msgs.extend(built_tool_results)
        injected += 1
        injected_calls += len(retained)

    return PreparedContinueToolHistory(
        model=str(model),
        messages=tuple(injected_msgs),
        injected_calls=injected_calls,
        injected_rounds=injected,
        thinking_blocks_attached=_thinking_blocks_attached,
        thought_signatures_attached=_thought_sigs_attached,
    )


def inject_tool_history(
    messages,
    cfg,
    task,
    model,
    *,
    prepared_history: PreparedContinueToolHistory | None = None,
):
    """Append one fully validated Continue history after current context.

    Preparation is reusable so the orchestrator can reject malformed state
    before expensive startup work, then inject the exact prepared occurrences
    here without a second parse. Direct callers retain the original API.
    """
    prepared = (
        prepared_history
        if prepared_history is not None else prepare_tool_history(cfg, task, model)
    )
    if prepared.model != str(model):
        raise ContinueToolHistoryProtocolError(
            'prepared toolHistory was built for a different model')
    if not prepared.messages:
        return 0

    insert_idx = len(messages)
    messages[insert_idx:insert_idx] = list(prepared.messages)
    task = task if isinstance(task, dict) else {}
    logger.debug(
        '[Task %s] conv=%s Restored %d tool round(s) (%d tool calls) '
        'from continue context, inserted at position %d, model=%s, '
        'thinking_blocks=%d thought_sigs=%d',
        str(task.get('id') or '')[:8],
        str(task.get('convId') or '')[:8],
        prepared.injected_rounds,
        prepared.injected_calls,
        insert_idx,
        model,
        prepared.thinking_blocks_attached,
        prepared.thought_signatures_attached,
    )
    return prepared.injected_calls


__all__ = [
    'ContinueToolHistoryProtocolError',
    'PreparedContinueToolHistory',
    'inject_tool_history',
    'prepare_tool_history',
]
