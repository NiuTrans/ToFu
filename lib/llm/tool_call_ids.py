# HOT_PATH
"""Provider wire contracts for tool-call correlation IDs.

Entry points:
  * ``tool_call_id_contract(protocol, logical_model)`` — the contract one
    outbound request must satisfy;
  * ``wire_tool_call_ids(messages, contract)`` — the wire spelling of every ID
    in a canonical history that the contract rejects;
  * ``portable_tool_call_id(stem, suffix)`` — an ID Tofu mints itself, valid
    under every contract except Mistral's fixed-width format.

Persisted history keeps the IDs it was given: native IDs of whichever route
produced a round (kimi-k3's Messages face returns ``watchdog_snapshot:90``),
execute_tools labels the model chose, and reminted local IDs. A provider
validates only what it is sent, so each outbound adapter respells exactly the
IDs its destination would reject; valid IDs reach the wire byte-identical.

A respelling depends only on the original ID and the contract, so a growing
conversation keeps a stable wire prefix; a call and its result are respelled
by one map; a collision check keeps every wire ID of a request unique.
Pure; the only dependency is creator-family attribution in ``lib.model_catalog``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any

_DIGEST_ALPHABET = ('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                    'abcdefghijklmnopqrstuvwxyz')

#: Mistral accepts only exactly nine ``[a-zA-Z0-9]`` (HTTP 400 "Tool call id
#: was call_0fypS1hVX but must be a-z, A-Z, 0-9, with a length of 9").  That
#: fixed format is the whole respelling for Mistral, and every other contract
#: appends the same digest so a respelled ID never depends on its neighbours.
MISTRAL_TOOL_CALL_ID_CHARS = 9


@dataclass(frozen=True)
class ToolCallIdContract:
    """Validation one provider applies to each tool-call ID it receives."""

    name: str
    invalid_char: re.Pattern[str] | None = None
    max_length: int | None = None
    fixed_length: bool = False

    def accepts(self, value: str) -> bool:
        if not value:
            return False
        if self.max_length is not None and len(value) > self.max_length:
            return False
        if self.fixed_length and len(value) != self.max_length:
            return False
        return self.invalid_char is None or self.invalid_char.search(value) is None

    def narrowed(self, other: 'ToolCallIdContract') -> 'ToolCallIdContract':
        """Return the contract satisfied only by IDs both contracts accept."""
        patterns = {pattern.pattern: pattern for pattern
                    in (self.invalid_char, other.invalid_char) if pattern}
        invalid_char = (next(iter(patterns.values())) if len(patterns) == 1
                        else re.compile('|'.join(f'(?:{key})' for key in patterns))
                        if patterns else None)
        lengths = [length for length in (self.max_length, other.max_length)
                   if length is not None]
        return replace(self, name=f'{self.name}+{other.name}',
                       invalid_char=invalid_char,
                       max_length=min(lengths) if lengths else None,
                       fixed_length=self.fixed_length or other.fixed_length)


#: Anthropic Messages ``tool_use.id`` / ``tool_result.tool_use_id`` must match
#: ``^[a-zA-Z0-9_-]+$`` (HTTP 400 "String should match pattern"; Bedrock-served
#: Claude through your-llm-gateway.example.com, 2026-11-05/06).
ANTHROPIC_MESSAGES = ToolCallIdContract(
    'anthropic_messages', invalid_char=re.compile(r'[^a-zA-Z0-9_-]'))
#: OpenAI Chat Completions ``tool_calls[].id``: "string too long. Expected a
#: string with maximum length 40" (code ``string_above_max_length``).
OPENAI_CHAT_COMPLETIONS = ToolCallIdContract(
    'openai_chat_completions', max_length=40)
#: OpenAI Responses ``function_call.call_id``: "maximum length 64"; the Codex
#: backend itself emits longer call IDs that it then refuses on replay.
OPENAI_RESPONSES = ToolCallIdContract('openai_responses', max_length=64)
MISTRAL = ToolCallIdContract(
    'mistral', invalid_char=re.compile(r'[^a-zA-Z0-9]'),
    max_length=MISTRAL_TOOL_CALL_ID_CHARS, fixed_length=True)
#: What Tofu mints: accepted by every contract above except Mistral's.
PORTABLE = ANTHROPIC_MESSAGES.narrowed(OPENAI_CHAT_COMPLETIONS).narrowed(
    OPENAI_RESPONSES)

_PROTOCOL_CONTRACTS = {
    'anthropic': ANTHROPIC_MESSAGES,
    'responses': OPENAI_RESPONSES,
}


def tool_call_id_contract(protocol: str, logical_model: str) -> ToolCallIdContract:
    """Return the contract for one request on ``protocol`` to ``logical_model``.

    The protocol's reference schema always applies. Two creator families add
    their own rule on any protocol: Mistral's serving stack enforces its fixed
    format, and a Chat/Responses gateway in front of Claude translates the
    request into Anthropic Messages, whose pattern then applies.
    """
    from lib.model_catalog._creator_identity import creator_family

    family = creator_family(logical_model)
    if family == 'mistral':
        return MISTRAL
    contract = _PROTOCOL_CONTRACTS.get(protocol, OPENAI_CHAT_COMPLETIONS)
    if family == 'anthropic' and contract is not ANTHROPIC_MESSAGES:
        contract = contract.narrowed(ANTHROPIC_MESSAGES)
    return contract


def portable_tool_call_id(stem: str, suffix: str) -> str:
    """Spell ``{stem}_{suffix}`` within ``PORTABLE``; ``suffix`` is kept whole.

    ``suffix`` carries the uniqueness and must itself be portable; ``stem``
    (typically the provider's original ID) is sanitized and shortened.
    """
    room = max(0, PORTABLE.max_length - len(suffix) - 1)
    stem = PORTABLE.invalid_char.sub('_', stem or '')[:room]
    return f'{stem}_{suffix}' if stem else suffix


def _digest(text: str) -> str:
    number = int.from_bytes(
        hashlib.sha256(text.encode('utf-8', 'surrogatepass')).digest(), 'big')
    chars = []
    for _ in range(MISTRAL_TOOL_CALL_ID_CHARS):
        number, index = divmod(number, len(_DIGEST_ALPHABET))
        chars.append(_DIGEST_ALPHABET[index])
    return ''.join(chars)


def _respelled(original: str, contract: ToolCallIdContract, attempt: int) -> str:
    digest = _digest(original if not attempt else f'{original}\x00{attempt}')
    if contract.fixed_length:
        return digest
    stem = (contract.invalid_char.sub('_', original)
            if contract.invalid_char is not None else original)
    if contract.max_length is not None:
        stem = stem[:max(0, contract.max_length - len(digest) - 1)]
    return f'{stem}_{digest}' if stem else digest


def _ids(message: Mapping[str, Any]) -> Iterator[str]:
    """Correlation IDs an adapter emits for one canonical message."""
    calls = message.get('tool_calls')
    for call in calls if isinstance(calls, list) else ():
        if isinstance(call, Mapping) and isinstance(call.get('id'), str):
            yield call['id']
    if isinstance(message.get('tool_call_id'), str):
        yield message['tool_call_id']
    blocks = message.get('_anthropic_content_blocks')
    for block in blocks if isinstance(blocks, list) else ():
        if (isinstance(block, Mapping) and block.get('type') == 'tool_use'
                and isinstance(block.get('id'), str)):
            yield block['id']


def _opaque_ids(message: Mapping[str, Any]) -> Iterator[str]:
    """IDs of native protocol state, which is replayed exactly as returned."""
    blocks = message.get('_anthropic_content_blocks')
    for block in blocks if isinstance(blocks, list) else ():
        if isinstance(block, Mapping) and block.get('type') != 'tool_use':
            for key in ('id', 'tool_use_id'):
                if isinstance(block.get(key), str):
                    yield block[key]
    items = message.get('_responses_items')
    for item in items if isinstance(items, list) else ():
        if isinstance(item, Mapping):
            for key in ('call_id', 'id'):
                if isinstance(item.get(key), str):
                    yield item[key]


def wire_tool_call_ids(messages: Iterable[Any],
                       contract: ToolCallIdContract) -> dict[str, str]:
    """Map every ID the contract rejects to its wire spelling.

    IDs the contract accepts are absent (emit them unchanged); so is ``''``,
    which ``lib.llm_sanitize`` heals by position. Callers apply the one map to
    every call and every result, so pairing is preserved.
    """
    originals: dict[str, None] = {}
    taken: set[str] = set()
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        for value in _ids(message):
            originals.setdefault(value, None)
        taken.update(_opaque_ids(message))
    rejected = [value for value in originals
                if value and not contract.accepts(value)]
    if not rejected:
        return {}
    taken.update(value for value in originals if contract.accepts(value))
    spelled: dict[str, str] = {}
    for original in rejected:
        attempt = 0
        candidate = _respelled(original, contract, attempt)
        while candidate in taken:
            attempt += 1
            candidate = _respelled(original, contract, attempt)
        taken.add(candidate)
        spelled[original] = candidate
    return spelled


def respell_chat_message(message: dict, wire_ids: Mapping[str, str]) -> dict:
    """Return ``message`` with Chat Completions correlation IDs respelled.

    The input is never mutated; an unchanged message is returned as is so its
    sealed encoding stays reusable across requests.
    """
    changed: dict[str, Any] = {}
    calls = message.get('tool_calls')
    if isinstance(calls, list) and any(
            isinstance(call, Mapping) and call.get('id') in wire_ids
            for call in calls):
        changed['tool_calls'] = [
            {**call, 'id': wire_ids[call['id']]}
            if isinstance(call, Mapping) and call.get('id') in wire_ids
            else call
            for call in calls]
    if message.get('tool_call_id') in wire_ids:
        changed['tool_call_id'] = wire_ids[message['tool_call_id']]
    return {**message, **changed} if changed else message


__all__ = [
    'ANTHROPIC_MESSAGES',
    'MISTRAL',
    'MISTRAL_TOOL_CALL_ID_CHARS',
    'OPENAI_CHAT_COMPLETIONS',
    'OPENAI_RESPONSES',
    'PORTABLE',
    'ToolCallIdContract',
    'portable_tool_call_id',
    'respell_chat_message',
    'tool_call_id_contract',
    'wire_tool_call_ids',
]
