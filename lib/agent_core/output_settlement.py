"""Seal execution output without replacing the already-observed transcript.

Pure value contract shared by execution/persistence adapters. Live blocks and
occurrences own recorded evidence; completion may extend a prefix or append
new evidence, not replace it. Editing/compaction is a separate operation.
No storage, provider, transport or browser dependencies live here.
"""
#
# Cross-process contract: the storage sidecar (a separate process) also
# imports this module; keep dependencies to the standard library and other
# sidecar-shared modules (enforced by scripts/check_architecture.py).
from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from lib.tool_results.tool_round_identity import execution_identity, execution_llm_round

_OUTPUT_FIELDS = frozenset({'content', 'thinking', 'toolRounds', 'segments'})


def _occurrence_key(row: Mapping) -> tuple:
    ref = row.get('inspectorRef')
    ref = ref if isinstance(ref, Mapping) else {}
    return (*execution_identity(row), ref.get('turn'), execution_llm_round(row),
            row.get('roundNum'), row.get('toolCallId') or '', row.get('toolName'))


def _compatible_occurrence(left: Mapping, right: Mapping) -> bool:
    """An omitted old-producer stamp is unknown, never a conflicting identity."""
    return all(a in (None, '') or b in (None, '') or a == b
               for a, b in zip(_occurrence_key(left), _occurrence_key(right)))


def _seal_rounds(rounds: list, additions: Any) -> list:
    exact, by_id = defaultdict(deque), defaultdict(list)
    by_ordinal = defaultdict(deque)
    for index, row in enumerate(rounds):
        if isinstance(row, Mapping):
            exact[_occurrence_key(row)].append(index)
            if row.get('toolCallId'):
                by_id[str(row['toolCallId'])].append(index)
            else:
                by_ordinal[(row.get('roundNum'), row.get('toolName'))].append(index)
    matched = set()
    for source in additions or ():
        if not isinstance(source, Mapping):
            continue
        row = deepcopy(dict(source))
        call_id = str(row.get('toolCallId') or '')
        if not call_id and not isinstance(row.get('roundNum'), int):
            raise ValueError('Completion tool occurrence needs a call ID or ordinal')
        # Queues retain sibling occurrences even in historical rows whose
        # provider recycled IDs. A full completion replay matches each once.
        queue = exact[_occurrence_key(row)]
        while queue and queue[0] in matched:
            queue.popleft()
        index = queue.popleft() if queue else None
        candidates = by_id.get(call_id, []) if call_id else []
        if (index is None and len(candidates) == 1
                and candidates[0] not in matched
                and _compatible_occurrence(rounds[candidates[0]], row)):
            index = candidates[0]
        if index is None and not call_id:
            ordinal_queue = by_ordinal[(row.get('roundNum'), row.get('toolName'))]
            while ordinal_queue and ordinal_queue[0] in matched:
                ordinal_queue.popleft()
            if ordinal_queue and _compatible_occurrence(rounds[ordinal_queue[0]], row):
                index = ordinal_queue.popleft()
        if index is None:
            rounds.append(row)
        else:
            matched.add(index)
            for key, value in row.items():
                if key not in rounds[index]:
                    rounds[index][key] = value
    return rounds


def _append_completion_blocks(segments: list, additions: Any) -> None:
    by_id = {block.get('blockId'): block for block in segments
             if isinstance(block, dict) and block.get('blockId')}
    for position, source in enumerate(additions or ()):
        if not isinstance(source, Mapping):
            continue
        block = deepcopy(dict(source))
        identity = block.get('blockId') or f'{block.get("type")}:completion-block-{position}'
        block['blockId'] = identity
        previous = by_id.get(identity)
        if previous is None:
            segments.append(block)
            by_id[identity] = block
        elif (block.get('type') in {'text', 'thinking'}
              and previous.get('type') == block['type']
              and isinstance(block.get('text'), str)
              and block['text'].startswith(str(previous.get('text') or ''))):
            previous['text'] = block['text']


def seal_output_projection(
    live: Mapping[str, Any], completion: Mapping[str, Any],
) -> dict[str, Any]:
    """Idempotently seal one invocation's live projection and completion facts.

    Tool previews never replace recorded results. Identity-bearing completion
    blocks append in supplied order; cumulative text/reasoning may extend a
    recorded prefix. A different final answer gets a new block, leaving the
    prior terminal text visible under its original identity.
    """
    result = deepcopy(dict(live))
    for key, value in completion.items():
        if key not in _OUTPUT_FIELDS:
            if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
                result[key] = {**result[key], **deepcopy(dict(value))}
            else:
                result[key] = deepcopy(value)

    segments = result.get('segments')
    segments = segments if isinstance(segments, list) else []
    _append_completion_blocks(segments, completion.get('segments'))
    terminal_text = next((block for block in reversed(segments)
                          if block.get('type') == 'text' and block.get('terminal')), {})
    old_content = str(live.get('content') or '')
    final_content = str(completion.get('content') or terminal_text.get('text') or '')
    if final_content and final_content != old_content:
        recorded = next((block for block in reversed(segments)
                         if block.get('type') == 'text'
                         and block.get('text') == final_content), None)
        extends = bool(old_content and final_content.startswith(old_content))
        preserved = False
        for block in segments:
            if block.get('type') == 'text' and block.get('terminal'):
                if extends and block.get('text') == old_content and recorded is None:
                    block['text'] = final_content
                    recorded = block
                if block is not recorded:
                    block.pop('terminal', None)
                    block['deliverable'] = False
                preserved = preserved or block.get('text') == old_content
        if old_content and not extends and not preserved and not any(
                block.get('type') == 'text' and block.get('text') == old_content
                for block in segments):
            segments.append({'type': 'text', 'text': old_content, 'deliverable': False})
        if recorded is None:
            used = {block.get('blockId') for block in segments}
            suffix = len(segments)
            while f'text:completion-{suffix}' in used:
                suffix += 1
            recorded = {'type': 'text', 'text': final_content,
                        'blockId': f'text:completion-{suffix}'}
            segments.append(recorded)
        recorded.update(terminal=True, deliverable=True)
        result['content'] = final_content
    else:
        result.setdefault('content', old_content)

    terminal_thinking = next((block for block in reversed(segments)
                              if block.get('type') == 'thinking' and block.get('terminal')), {})
    old_thinking = str(live.get('thinking') or '')
    final_thinking = str(completion.get('thinking') or terminal_thinking.get('text') or '')
    result['thinking'] = (final_thinking if final_thinking.startswith(old_thinking)
                          else old_thinking)
    if segments:
        result['segments'] = segments
    rounds = result.get('toolRounds')
    result['toolRounds'] = _seal_rounds(
        rounds if isinstance(rounds, list) else [], completion.get('toolRounds'))
    return result


__all__ = ['seal_output_projection']
