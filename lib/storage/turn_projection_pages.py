"""Frame-bounded pages of one public Turn projection: plan and reassembly.

Responsibility: the ``turn.projection.page`` contract (docs/STORAGE.md, "Paged
Turn storage"). :func:`turn_projection_page` splits one public Turn record
into an ordered page sequence whose encoded responses each fit one storage.v2
frame payload, so no reader needs a whole Turn in one storage response; the SQL
authorities serve the operation through it and the native engine mirrors this
plan in ``packages/tofu-db/src/turn/projection_page.rs``.
:func:`read_turn_projection` (or :func:`read_paged_turn` over a storage port) is
the one client reader that reassembles a Turn from those pages in process, at
exactly one projection revision.

Page 0 carries the Turn record without its paged members plus each member's
length; every page carries contiguous slices of the members in member order.
A page always admits one indivisible unit (one array item or one code point),
so only an item that alone exceeds the frame can exceed it. Pure: no storage
client, network or application dependency; callers pass the page reader or
storage port.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import orjson

from lib.storage.errors import StorageError
from lib.storage.generated_v2_semantics import MAX_FRAME_PAYLOAD_BYTES

if TYPE_CHECKING:
    from lib.storage.port import StoragePort


TURN_PROJECTION_PAGE_CONTRACT = 'tofu.turn-projection-page/v1'
# The members that grow with tool rounds. Every other projection field is
# bounded by its own schema and stays in the head on page 0.
PAGED_SEQUENCE_MEMBERS = ('toolRounds', 'segments', 'apiRounds')
PAGED_TEXT_MEMBERS = ('content', 'thinking')


def stale_turn_projection_page(projection_revision: int) -> dict[str, Any]:
    return {
        'contract': TURN_PROJECTION_PAGE_CONTRACT,
        'stale': True,
        'projectionRevision': projection_revision,
    }


def _encoded_bytes(value: Any) -> int:
    return len(orjson.dumps(value))


def _split_head(turn: Mapping[str, Any]) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    head = dict(turn)
    projection = turn.get('projection')
    if not isinstance(projection, Mapping):
        return head, []
    head_projection = dict(projection)
    members = []
    for name, kind in ([(name, list) for name in PAGED_SEQUENCE_MEMBERS]
                       + [(name, str) for name in PAGED_TEXT_MEMBERS]):
        if type(head_projection.get(name)) is kind:
            members.append((name, head_projection.pop(name)))
    head['projection'] = head_projection
    return head, members


def _envelope(revision: int, page_bound: int) -> dict[str, Any]:
    return {
        'contract': TURN_PROJECTION_PAGE_CONTRACT,
        'stale': False,
        'projectionRevision': revision,
        'page': page_bound,
        'pageCount': page_bound,
    }


def _slice_opening_bytes(name: str, start: int, text: bool) -> int:
    """``"name":{"start":S,"items":[]}`` or its text form, without separators."""
    return _encoded_bytes({name: {'start': start, ('text' if text else 'items'): '' if text else []}}) - 2


class _Planner:
    """Greedy page boundaries; the native engine applies the same accounting."""

    def __init__(self, first_page_bytes: int, page_bytes: int):
        self.page_bytes = page_bytes
        self.pages: list[list[list[Any]]] = []
        self.slices: list[list[Any]] = []
        self.used = first_page_bytes
        self.units = 0

    def _fits(self, cost: int) -> bool:
        return self.used + cost <= MAX_FRAME_PAYLOAD_BYTES

    def _must_close(self) -> bool:
        # A later page always admits one unit; only page 0 may hold just the head.
        return self.units > 0 or not self.pages

    def _close(self) -> None:
        self.pages.append(self.slices)
        self.slices = []
        self.used = self.page_bytes
        self.units = 0

    def _opening(self, name: str, start: int, text: bool) -> int:
        return _slice_opening_bytes(name, start, text) + (1 if self.slices else 0)

    def add_item(self, name: str, index: int, item_bytes: int) -> None:
        if self.slices and self.slices[-1][0] == name:
            cost = item_bytes + 1
        else:
            cost = self._opening(name, index, False) + item_bytes
        if not self._fits(cost) and self._must_close():
            self._close()
            cost = self._opening(name, index, False) + item_bytes
        if self.slices and self.slices[-1][0] == name:
            self.slices[-1][2] = index + 1
        else:
            self.slices.append([name, index, index + 1])
        self.used += cost
        self.units += 1

    def add_text(self, name: str, text: str) -> None:
        start = 0
        while start < len(text):
            opening = self._opening(name, start, True)
            end = self._largest_fitting_end(text, start, MAX_FRAME_PAYLOAD_BYTES - self.used - opening)
            if end == start and self._must_close():
                self._close()
                continue
            end = max(end, start + 1)
            self.slices.append([name, start, end])
            self.used += opening + _encoded_bytes(text[start:end]) - 2
            self.units += end - start
            if end < len(text):
                self._close()
            start = end

    @staticmethod
    def _largest_fitting_end(text: str, start: int, budget: int) -> int:
        """Longest prefix whose escaped JSON string body fits ``budget`` bytes."""
        low, high = start, min(len(text), start + max(budget, 0))
        while low < high:
            middle = (low + high + 1) // 2
            if _encoded_bytes(text[start:middle]) - 2 <= budget:
                low = middle
            else:
                high = middle - 1
        return low

    def finish(self) -> list[list[list[Any]]]:
        self.pages.append(self.slices)
        return self.pages


def turn_projection_page(turn: Mapping[str, Any], page: int) -> dict[str, Any] | None:
    """Return page ``page`` of one public Turn, or ``None`` past the last page."""
    revision = turn['projectionRevision']
    head, members = _split_head(turn)
    lengths = {name: len(value) for name, value in members}
    # Page numbers never exceed the unit count, so this bounds their digits.
    page_bound = sum(lengths.values()) + 1
    later = _envelope(revision, page_bound) | {'members': {}}
    first = _envelope(revision, page_bound) | {
        'turn': head, 'pagedMembers': lengths, 'members': {}}
    planner = _Planner(_encoded_bytes(first), _encoded_bytes(later))
    for name, value in members:
        if isinstance(value, str):
            planner.add_text(name, value)
            continue
        for index, item in enumerate(value):
            planner.add_item(name, index, _encoded_bytes(item))
    pages = planner.finish()
    if page >= len(pages):
        return None
    values = dict(members)
    response = _envelope(revision, page) | {'pageCount': len(pages)}
    if page == 0:
        response |= {'turn': head, 'pagedMembers': lengths}
    response['members'] = {
        name: ({'start': start, 'text': values[name][start:end]}
               if isinstance(values[name], str)
               else {'start': start, 'items': values[name][start:end]})
        for name, start, end in pages[page]
    }
    return response


class TurnProjectionRevisionMoved(Exception):
    """A read pinned to one projection revision found the Turn at another."""

    def __init__(self, projection_revision: int):
        super().__init__(f'Turn projection moved to revision {projection_revision}')
        self.projection_revision = projection_revision


#: Sends one ``turn.projection.page`` request whose fields (``page`` plus an
#: optional ``projection_revision`` or ``page_revision``) extend the Turn
#: identity, and returns the authority's answer.
TurnProjectionPageReader = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]


def _protocol_error(message: str) -> StorageError:
    return StorageError('database_protocol_error', message)


def _checked_page(page: Any, index: int) -> Mapping[str, Any]:
    if not isinstance(page, Mapping) or page.get('contract') != TURN_PROJECTION_PAGE_CONTRACT:
        raise _protocol_error(f'Turn projection page {index} has another contract')
    count = page.get('pageCount')
    if (page.get('stale') is not False or page.get('page') != index
            or type(count) is not int or count <= index
            or not isinstance(page.get('members'), Mapping)):
        raise _protocol_error(f'Turn projection page {index} is not that page')
    if index == 0 and not (isinstance(page.get('turn'), Mapping)
                           and isinstance(page.get('pagedMembers'), Mapping)):
        raise _protocol_error('Turn projection page 0 carries no Turn head')
    return page


def _page_revisions(head: Mapping[str, Any]) -> list[int] | None:
    """Page 0's per-page fences, or ``None`` from an authority without them."""
    revisions = head.get('pageRevisions')
    if revisions is None:
        return None
    if (not isinstance(revisions, list) or len(revisions) != head['pageCount']
            or any(type(revision) is not int for revision in revisions)
            or head.get('pageRevision') != revisions[0]):
        raise _protocol_error('Turn projection page 0 has malformed page revisions')
    return revisions


def _reassembled(pages: list[Mapping[str, Any]]) -> dict[str, Any]:
    head = pages[0]
    lengths = head['pagedMembers']
    pieces: dict[str, list[Any]] = {}
    for name, length in lengths.items():
        if (name not in PAGED_SEQUENCE_MEMBERS + PAGED_TEXT_MEMBERS
                or type(length) is not int or length < 0):
            raise _protocol_error(f'Turn projection page 0 names no paged member {name!r}')
        pieces[name] = []
    filled = dict.fromkeys(lengths, 0)
    for page in pages:
        for name, piece in page['members'].items():
            text = name in PAGED_TEXT_MEMBERS
            values = piece.get('text' if text else 'items') if isinstance(piece, Mapping) else None
            if (name not in pieces or piece.get('start') != filled[name]
                    or not isinstance(values, str if text else list)):
                raise _protocol_error(
                    f'Turn projection page {page["page"]} does not continue {name!r}')
            pieces[name].append(values)
            filled[name] += len(values)
    if filled != dict(lengths):
        raise _protocol_error('Turn projection pages do not cover every paged member')
    turn = dict(head['turn'])
    if not pieces:
        return turn
    projection = turn.get('projection')
    if not isinstance(projection, Mapping) or set(pieces) & set(projection):
        raise _protocol_error('Turn projection page 0 keeps a paged member in its head')
    turn['projection'] = dict(projection) | {
        name: (''.join(parts) if name in PAGED_TEXT_MEMBERS
               else [item for part in parts for item in part])
        for name, parts in pieces.items()}
    return turn


def read_turn_projection(read_page: TurnProjectionPageReader, *,
                         projection_revision: int | None = None) -> dict[str, Any] | None:
    """Reassemble one public Turn from its pages, equal to ``turn.get`` at one revision.

    Returns the Turn at exactly the revision of the page 0 that completed the
    read, or ``None`` when it is absent or was deleted while being read. When
    page 0 advertises ``pageRevisions``, every later page is fenced by its own
    revision, so a write that leaves a page unchanged (head fields, the growing
    tail) never discards it; otherwise every page is fenced by the projection
    revision, as stage-1 authorities answer. A stale answer restarts at page 0
    and keeps each held page whose revision page 0 still advertises. A restart
    follows only a committed write, so the read completes once the pages it
    needs stop changing. ``projection_revision`` pins the read to that revision
    instead: the Turn at any other raises :class:`TurnProjectionRevisionMoved`.
    """
    held: dict[int, tuple[int, Mapping[str, Any]]] = {}
    while True:
        first: dict[str, Any] = {'page': 0}
        if projection_revision is not None:
            first['projection_revision'] = projection_revision
        head = read_page(first)
        if head is None:
            return None
        if isinstance(head, Mapping) and head.get('stale') is True:
            if projection_revision is None:
                raise _protocol_error('An unfenced Turn projection page 0 answered stale')
            raise TurnProjectionRevisionMoved(int(head['projectionRevision']))
        head = _checked_page(head, 0)
        page_revisions = _page_revisions(head)
        pages = [head]
        for index in range(1, head['pageCount']):
            fields: dict[str, Any] = {'page': index}
            if page_revisions is None:
                fields['projection_revision'] = head['projectionRevision']
            else:
                kept = held.get(index)
                if kept is not None and kept[0] == page_revisions[index]:
                    pages.append(kept[1])
                    continue
                fields['page_revision'] = page_revisions[index]
            page = read_page(fields)
            if page is None:
                return None
            if isinstance(page, Mapping) and page.get('stale') is True:
                if projection_revision is not None:
                    raise TurnProjectionRevisionMoved(int(page['projectionRevision']))
                break
            page = _checked_page(page, index)
            if page_revisions is not None:
                if page.get('pageRevision') != page_revisions[index]:
                    raise _protocol_error(f'Turn projection page {index} answered another revision')
                held[index] = (page_revisions[index], page)
            pages.append(page)
        else:
            return _reassembled(pages)


def read_paged_turn(client: StoragePort, *, user_id: Any, conversation_id: str, turn_id: str,
                    projection_revision: int | None = None,
                    deadline: float | None = None) -> dict[str, Any] | None:
    """:func:`read_turn_projection` through one storage port's ``turn.projection.page``."""
    identity = {'user_id': user_id, 'conversation_id': conversation_id, 'turn_id': turn_id}

    def read_page(fields: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return client.query('turn.projection.page', identity | dict(fields), deadline=deadline)

    return read_turn_projection(read_page, projection_revision=projection_revision)
