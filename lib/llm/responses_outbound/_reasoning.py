"""Readable Responses reasoning reconciliation; opaque replay stays in _sse.

ReasoningSummaryReconciler consumes text/part lifecycle events and item/terminal
snapshots. Each output occurrence and summary/content part owns a constant-size
prefix proof. Only text blocked behind an unfinished earlier occurrence is held,
under the shared SSE event-byte bound. Paragraphs begin when text is emitted,
never at an empty/encrypted-only lifecycle marker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib

from lib.llm._sse_framer import MAX_SSE_EVENT_BYTES


class ReasoningSummaryError(ValueError):
    """A malformed, conflicting or unrepresentable readable reasoning stream."""


@dataclass(eq=False, slots=True)
class _Part:
    chars: int = 0
    digest: object = field(default_factory=hashlib.sha256)
    pending: list[str] = field(default_factory=list)
    closed: bool = False
    started: bool = False

    emitted_order: int | None = None

@dataclass(eq=False, slots=True)
class _Item:
    output_index: int | None = None
    item_id: str | None = None
    implicit: bool = False
    closed: bool = False
    parts: dict[tuple[str, int], _Part] = field(default_factory=dict)
    current_parts: dict[str, int] = field(default_factory=dict)


class ReasoningSummaryReconciler:
    """Per-attempt readable state with explicit identity and bounded storage.

    Public methods return newly emitted strings or raise ReasoningSummaryError;
    the translator alone creates canonical chunks and typed error envelopes.
    Omitted/null/empty item arrays are metadata, not proof of erased deltas.
    An explicit string, including an empty one, must agree with its own prefix.
    """

    def __init__(self, *, max_output_index: int, max_item_id_chars: int):
        self._max_index = max_output_index
        self._max_id_chars = max_item_id_chars
        self._items: list[_Item] = []
        self._by_index: dict[int, _Item] = {}
        self._by_id: dict[str, list[_Item]] = {}
        self._current_item: _Item | None = None
        self._part_count = 0
        self._pending_bytes = 0

        self._emitted_part_count = 0
        self._last_emitted: tuple[_Item, tuple[str, int]] | None = None

    def _index(self, value: object, field_name: str) -> int | None:
        if value is None:
            return None
        if (not isinstance(value, int) or isinstance(value, bool)
                or not 0 <= value <= self._max_index):
            raise ReasoningSummaryError(
                f'reasoning {field_name} must be a bounded integer or null')
        return value

    def _identity(self, value: object) -> str | None:
        if value is None or value == '':
            return None
        if not isinstance(value, str) or len(value) > self._max_id_chars:
            raise ReasoningSummaryError('reasoning item_id must be bounded text or null')
        return value

    @staticmethod
    def _text(value: object) -> str | None:
        if value is not None and not isinstance(value, str):
            raise ReasoningSummaryError('reasoning text must be text or null')
        return value

    @staticmethod
    def _encoded(text: str) -> bytes:
        try:
            return text.encode('utf-8')
        except UnicodeEncodeError as error:
            raise ReasoningSummaryError('reasoning text must be valid UTF-8') from error

    def _bind(self, item: _Item, item_id: str | None, index: int | None) -> _Item:
        if index is not None:
            if item.output_index is not None and item.output_index != index:
                raise ReasoningSummaryError('reasoning output_index changed its identity')
            item.output_index = index
            self._by_index[index] = item
        if item_id:
            if item.item_id and item.item_id != item_id:
                raise ReasoningSummaryError('reasoning item_id and output_index identify different items')
            if not item.item_id:
                item.item_id = item_id
                self._by_id.setdefault(item_id, []).append(item)
        return item

    def _new_item(self, *, implicit: bool) -> _Item:
        if len(self._items) >= self._max_index + 1:
            raise ReasoningSummaryError('reasoning item state exceeds the response-position bound')
        item = _Item(implicit=implicit)
        self._items.append(item)
        return item

    def _resolve_item(self, route: dict, *, source: str) -> _Item:
        index = self._index(route.get('output_index'), 'output_index')
        item_id = self._identity(route.get('item_id'))
        candidates = self._by_id.get(item_id, []) if item_id else []
        indexed = self._by_index.get(index) if index is not None else None
        lifecycle = source in ('added', 'item_done', 'terminal')
        if indexed is not None:
            if item_id and indexed.item_id and indexed.item_id != item_id:
                raise ReasoningSummaryError(
                    'reasoning item_id and output_index identify different items')
            if (item_id and not indexed.item_id and candidates
                    and indexed not in candidates and not lifecycle):
                raise ReasoningSummaryError('reasoning routing identities resolve to different items')
            return self._bind(indexed, item_id, index)
        if candidates:
            if index is None:
                if len(candidates) != 1:
                    raise ReasoningSummaryError(
                        'ambiguous reasoning item_id; output_index is required')
                return candidates[0]
            unindexed = [item for item in candidates if item.output_index is None]
            if len(unindexed) == 1:
                return self._bind(unindexed[0], item_id, index)
            if not lifecycle:
                raise ReasoningSummaryError(
                    'reasoning item_id and output_index resolve to different identities')
            # Lifecycle snapshots at distinct positions prove independent
            # occurrences, even when the provider recycles an item id.
        else:
            unbound = [item for item in self._items
                       if item.output_index is None and (item_id is None or item.item_id is None)]
            if source == 'terminal' and unbound:
                return self._bind(unbound[0], item_id, index)
            compatible = [item for item in self._items
                          if not item.closed and item.implicit
                          and (index is None or item.output_index in (None, index))
                          and (item_id is None or item.item_id in (None, item_id))]
            if len(compatible) == 1 and (index is not None or item_id):
                return self._bind(compatible[0], item_id, index)
        if index is None and item_id is None and source != 'terminal':
            current = self._current_item
            if current is not None and (
                    source not in ('added', 'item_done') or not current.closed):
                if source != 'added' or current.implicit:
                    return current
        item = self._bind(self._new_item(implicit=not lifecycle), item_id, index)
        if self._current_item is None or source != 'terminal':
            self._current_item = item
        return item

    @staticmethod
    def _part_order(key: tuple[str, int]) -> tuple[int, int]:
        channel, index = key
        return (0 if channel == 'summary' else 1, index)

    def _ordered_items(self) -> list[_Item]:
        if all(item.output_index is not None for item in self._items):
            return sorted(self._items, key=lambda item: item.output_index)
        # Compatibility events with no positions retain occurrence order.
        # A terminal may bind these positions, but never by text equality.
        return self._items

    def _validate_emitted_order(self) -> None:
        # Unknown positions keep occurrence order, but cannot erase a proven
        # relative order between indexed items. Validate both views without
        # assigning speculative positions to the unbound items.
        indexed = sorted(self._by_index.values(), key=lambda item: item.output_index)
        for items in (self._ordered_items(), indexed):
            previous = -1
            for item in items:
                for key in sorted(item.parts, key=self._part_order):
                    order = item.parts[key].emitted_order
                    if order is None:
                        continue
                    if order <= previous:
                        raise ReasoningSummaryError(
                            'reasoning terminal order contradicts the emitted paragraph prefix')
                    previous = order

    def _part(self, item: _Item, channel: str, index: int) -> _Part:
        for preceding_index in range(index + 1):
            key = (channel, preceding_index)
            if key not in item.parts:
                if self._part_count >= self._max_index + 1:
                    raise ReasoningSummaryError('reasoning part state exceeds the response-position bound')
                item.parts[key] = _Part()
                self._part_count += 1
        return item.parts[(channel, index)]

    def _resolve_part(self, item: _Item, event: dict, channel: str,
                      *, added: bool = False) -> tuple[tuple[str, int], _Part]:
        field_name = 'summary_index' if channel == 'summary' else 'content_index'
        index = self._index(event.get(field_name), field_name)
        current_index = item.current_parts.get(channel, 0)
        if index is None:
            index = current_index
            current = item.parts.get((channel, index))
            if added and current is not None and (
                    current.started or current.chars or current.closed):
                # Unindexed providers expose paragraph boundaries only through
                # their lifecycle order. Empty markers still emit no text.
                current.closed = True
                index += 1
                self._index(index, field_name)
        key = (channel, index)
        part = self._part(item, channel, index)
        item.current_parts[channel] = index
        if added:
            part.started = True
        return key, part

    def _can_emit(self, target: _Item, target_key: tuple[str, int]) -> bool:
        for item in self._ordered_items():
            for key in sorted(item.parts, key=self._part_order):
                if item is target and key == target_key:
                    return True
                if not item.parts[key].closed:
                    return False
            if not item.closed:
                return False
        return False

    def _append(self, item: _Item, key: tuple[str, int], part: _Part, text: str) -> None:
        if not text:
            return
        if self._last_emitted is not None:
            last_item, last_key = self._last_emitted
            ordered = self._ordered_items()
            if (ordered.index(item) < ordered.index(last_item)
                    or (item is last_item and self._part_order(key) < self._part_order(last_key))):
                raise ReasoningSummaryError(
                    'reasoning snapshot extends a prefix before an already emitted paragraph')
        if item.output_index is not None and any(
                index > item.output_index
                and any(previous.emitted_order is not None for previous in indexed.parts.values())
                for index, indexed in self._by_index.items()):
            raise ReasoningSummaryError(
                'reasoning snapshot inserts text before an emitted indexed paragraph prefix')
        encoded = self._encoded(text)

        if (self._pending_bytes + len(encoded) > MAX_SSE_EVENT_BYTES
                and not self._can_emit(item, key)):
            raise ReasoningSummaryError('reasoning pending text exceeds the SSE byte bound')
        part.chars += len(text)
        part.digest.update(encoded)
        part.pending.append(text)
        self._pending_bytes += len(encoded)

    def _complete(self, item: _Item, key: tuple[str, int], part: _Part,
                  text: str | None) -> None:
        if text is None:
            return
        prefix = text[:part.chars]
        if (len(prefix) != part.chars
                or hashlib.sha256(self._encoded(prefix)).digest() != part.digest.digest()):
            raise ReasoningSummaryError('reasoning snapshot contradicts its streamed text prefix')
        self._append(item, key, part, text[part.chars:])

    def _drain(self) -> list[str]:
        emitted = []
        for item in self._ordered_items():
            for key in sorted(item.parts, key=self._part_order):
                part = item.parts[key]
                if part.pending:
                    if self._last_emitted is not None and self._last_emitted != (item, key):
                        emitted.append('\n\n')
                    text = ''.join(part.pending)
                    part.pending.clear()
                    self._pending_bytes -= len(self._encoded(text))
                    emitted.append(text)

                    if part.emitted_order is None:
                        part.emitted_order = self._emitted_part_count
                        self._emitted_part_count += 1
                    self._last_emitted = (item, key)
                if not part.closed:
                    return emitted
            if not item.closed:
                return emitted
        return emitted

    def _finish_event(self) -> list[str]:

        self._validate_emitted_order()
        emitted = self._drain()

        # Newly released buffered paragraphs receive their ordinals in drain.
        # Refuse contradictions before returning any chunks to the accumulator.
        self._validate_emitted_order()
        if self._pending_bytes > MAX_SSE_EVENT_BYTES:
            raise ReasoningSummaryError('reasoning pending text exceeds the SSE byte bound')
        return emitted

    def event(self, event: dict) -> list[str]:
        """Consume readable text deltas/done and summary-part lifecycle events."""
        event_type = event['type']
        channel = 'content' if event_type.startswith('response.reasoning_text.') else 'summary'
        item = self._resolve_item(event, source='event')
        is_part = event_type.startswith('response.reasoning_summary_part.')
        added = is_part and event_type.endswith('.added')
        key, part = self._resolve_part(item, event, channel, added=added)
        if is_part:
            snapshot = event.get('part')
            if 'part' in event:
                if not isinstance(snapshot, dict):
                    raise ReasoningSummaryError('reasoning summary part must be an object')
                text = self._text(snapshot.get('text'))
                # Part starts commonly carry empty text, including transport
                # replays after deltas. Only done snapshots prove final text.
                if not added or text:
                    self._complete(item, key, part, text)
        elif event_type.endswith('.delta'):
            text = self._text(event.get('delta'))
            self._append(item, key, part, text or '')
        else:
            self._complete(item, key, part, self._text(event.get('text')))
        if event_type.endswith('.done'):
            part.closed = True
        return self._finish_event()

    def _snapshot_parts(self, item: dict) -> dict[str, list[str | None]]:
        snapshots = {}
        for channel in ('summary', 'content'):
            parts = item.get(channel)
            if parts is None:
                continue
            if not isinstance(parts, list):
                raise ReasoningSummaryError(f'reasoning {channel} must be an array or null')
            if len(parts) > self._max_index + 1:
                raise ReasoningSummaryError('reasoning snapshot parts exceed the response-position bound')
            texts = []
            for part in parts:
                if not isinstance(part, dict):
                    raise ReasoningSummaryError('reasoning snapshot part must be an object')
                texts.append(self._text(part.get('text')))
            snapshots[channel] = texts
        return snapshots

    def _snapshot(self, item: _Item, snapshots: dict[str, list[str | None]],
                  *, done: bool) -> None:
        for channel, texts in snapshots.items():
            if texts and all(text is not None for text in texts):
                if any(key[0] == channel and key[1] >= len(texts) and part.chars
                       for key, part in item.parts.items()):
                    raise ReasoningSummaryError('reasoning snapshot omitted a streamed part prefix')
            for index, text in enumerate(texts):
                key = (channel, index)
                part = self._part(item, channel, index)
                if done or text:
                    self._complete(item, key, part, text)
                if done:
                    part.closed = True
        if done:
            item.closed = True
            for part in item.parts.values():
                part.closed = True

    def item(self, item: dict, output_index: object, *, added: bool = False) -> list[str]:
        """Reconcile one reasoning output item; metadata never erases text."""
        snapshots = self._snapshot_parts(item)
        state = self._resolve_item(
            {'item_id': item.get('id'), 'output_index': output_index},
            source='added' if added else 'item_done')
        if added:
            state.implicit = False
            self._current_item = state
        self._snapshot(state, snapshots, done=not added)
        return self._finish_event()

    def terminal(self, output: list[dict]) -> list[str]:
        """Validate all final reasoning snapshots before returning any text."""
        snapshots = [(index, item, self._snapshot_parts(item))
                     for index, item in enumerate(output) if item.get('type') == 'reasoning']
        for index, item, parts in snapshots:
            state = self._resolve_item(
                {'item_id': item.get('id'), 'output_index': index}, source='terminal')
            self._snapshot(state, parts, done=True)
        # Some providers finish with metadata-only output (or no output).
        # Retain their proven deltas and release any ordered pending text,
        # without manufacturing summaries from encrypted state/token usage.
        for item in self._items:
            item.closed = True
            for part in item.parts.values():
                part.closed = True
        return self._finish_event()
