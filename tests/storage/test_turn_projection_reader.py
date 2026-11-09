"""A Turn reassembles from its frame-bounded pages in process, at one revision.

The shared client reader of ``turn.projection.page`` (docs/STORAGE.md, "Paged
Turn storage", Decision 3) returns exactly one revision of a Turn. When page 0
advertises per-page revisions, a write that leaves a page unchanged never
discards it; otherwise every page is fenced by the projection revision, as
stage-1 authorities answer. The in-memory authority below serves the shared
planner's pages and derives per-page revisions itself; the last case runs the
real operation on SQLite, native and PostgreSQL.
"""
from __future__ import annotations

import hashlib

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage.turn_projection_pages import (
    TURN_PROJECTION_PAGE_CONTRACT,
    TurnProjectionRevisionMoved,
    read_paged_turn,
    read_turn_projection,
    turn_projection_page,
)

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = [pytest.mark.unit]

_ROUND_BYTES = 1 << 20


def _large_turn() -> dict:
    """About four frames of tool rounds, then text members on the last pages."""
    return {
        'turnId': 'turn-1', 'conversationId': 'paged-reader', 'projectionRevision': 1,
        'status': 'completed', 'marker': 'aaaa',
        'projection': {
            'toolRounds': [{'toolCallId': f'call-{index}',
                            'toolContent': f'{index:04d}' + 'r' * _ROUND_BYTES}
                           for index in range(32)],
            'content': 'answer ' * 1000,
            'thinking': 'reasoning ' * 100_000,
        },
    }


def _page_digest(page) -> str:
    stable = {key: value for key, value in page.items()
              if key not in ('projectionRevision', 'pageCount')}
    return hashlib.sha256(orjson.dumps(stable, option=orjson.OPT_SORT_KEYS)).hexdigest()


class _PagedAuthority:
    """``turn.projection.page`` over one in-memory Turn, planned by the shared planner."""

    def __init__(self, turn, *, page_fences):
        self.turn = turn
        self.page_fences = page_fences
        self.requests: list[dict] = []
        self._hooks: list[list] = []
        self._seen: dict[int, tuple[str, int]] = {}

    def before(self, page, action):
        """Run ``action(self)`` once, just before the first request for ``page``."""
        self._hooks.append([page, action])

    def pages(self) -> list[dict]:
        pages, index = [], 0
        while (page := turn_projection_page(self.turn, index)) is not None:
            pages.append(page)
            index += 1
        return pages

    def _page_revisions(self, pages) -> list[int]:
        revisions = []
        for index, page in enumerate(pages):
            digest = _page_digest(page)
            seen = self._seen.get(index)
            if seen is None or seen[0] != digest:
                seen = self._seen[index] = (digest, self.turn['projectionRevision'])
            revisions.append(seen[1])
        return revisions

    def __call__(self, fields):
        self.requests.append(dict(fields))
        for hook in list(self._hooks):
            if hook[0] == fields['page']:
                self._hooks.remove(hook)
                hook[1](self)
        if self.turn is None:
            return None
        revision = self.turn['projectionRevision']
        pages = self.pages()
        revisions = self._page_revisions(pages)
        index = fields['page']
        stale = {'contract': TURN_PROJECTION_PAGE_CONTRACT, 'stale': True,
                 'projectionRevision': revision}
        if fields.get('projection_revision') not in (None, revision):
            return stale
        if 'page_revision' in fields:
            assert self.page_fences, 'a stage-1 authority never receives a page fence'
            if index >= len(pages) or revisions[index] != fields['page_revision']:
                return stale
        if index >= len(pages):
            raise StorageError('database_protocol_error', 'Turn projection page is outside its revision')
        page = pages[index]
        if self.page_fences:
            page = page | {'pageRevision': revisions[index]}
            if index == 0:
                page['pageRevisions'] = revisions
        # A wire round trip: the reader never shares objects with the authority.
        return orjson.loads(orjson.dumps(page))


def _write(authority, change) -> None:
    turn = orjson.loads(orjson.dumps(authority.turn))
    change(turn)
    turn['projectionRevision'] += 1
    authority.turn = turn


def _grow_tail(authority):
    _write(authority, lambda turn: turn['projection'].update(
        thinking=turn['projection']['thinking'] + ' and more reasoning'))


def _touch_head(authority):
    """A head-only write of the same encoded size: every page keeps its slices."""
    _write(authority, lambda turn: turn.update(marker='bbbb'))


def _rewrite_round(index):
    """Replace one tool round with another of the same encoded size."""
    def rewrite(authority):
        def change(turn):
            rounds = turn['projection']['toolRounds']
            rounds[index] = rounds[index] | {
                'toolContent': f'{index:04d}' + 'w' * _ROUND_BYTES}
        _write(authority, change)
    return rewrite


def _page_holding_round(pages, index) -> int:
    for page in pages:
        piece = page['members'].get('toolRounds')
        if piece and piece['start'] <= index < piece['start'] + len(piece['items']):
            return page['page']
    raise AssertionError(f'no page holds tool round {index}')


def test_a_stage_one_authority_reassembles_the_turn_page_by_page():
    authority = _PagedAuthority(_large_turn(), page_fences=False)
    count = len(authority.pages())
    assert count >= 4

    assert read_turn_projection(authority) == authority.turn
    assert authority.requests == [{'page': 0}] + [
        {'page': index, 'projection_revision': 1} for index in range(1, count)]


def test_without_page_revisions_a_moved_turn_restarts_the_whole_read():
    authority = _PagedAuthority(_large_turn(), page_fences=False)
    authority.before(2, _grow_tail)

    result = read_turn_projection(authority)

    assert result == authority.turn and result['projectionRevision'] == 2
    count = len(authority.pages())
    assert authority.requests == [
        {'page': 0}, {'page': 1, 'projection_revision': 1}, {'page': 2, 'projection_revision': 1},
        {'page': 0}] + [{'page': index, 'projection_revision': 2} for index in range(1, count)]


def test_page_revisions_ignore_a_head_write_that_leaves_every_page_unchanged():
    authority = _PagedAuthority(_large_turn(), page_fences=True)
    original = orjson.loads(orjson.dumps(authority.turn))
    count = len(authority.pages())
    authority.before(1, _touch_head)

    result = read_turn_projection(authority)

    # The read completes at its first revision without one extra request.
    assert result == original and authority.turn['projectionRevision'] == 2
    assert [request['page'] for request in authority.requests] == list(range(count))


def test_page_revisions_keep_every_unchanged_page_while_the_tail_grows():
    authority = _PagedAuthority(_large_turn(), page_fences=True)
    last = len(authority.pages()) - 1
    authority.before(last, _grow_tail)

    result = read_turn_projection(authority)

    assert result == authority.turn and result['projectionRevision'] == 2
    pages = [request['page'] for request in authority.requests]
    assert pages[:last + 1] == list(range(last + 1))
    # After the stale tail only page 0 and the pages the write changed are read again.
    assert pages[last + 1] == 0
    assert set(pages[last + 2:]) and not set(pages[last + 2:]) & set(range(1, last))


def test_a_held_page_rewritten_later_still_yields_one_revision():
    authority = _PagedAuthority(_large_turn(), page_fences=True)
    original = orjson.loads(orjson.dumps(authority.turn))
    pages = authority.pages()
    held = _page_holding_round(pages, 10)
    assert 0 < held < len(pages) - 1
    authority.before(held + 1, _rewrite_round(10))

    # Every page read after the write is unchanged, so this is revision 1 exactly.
    assert read_turn_projection(authority) == original


def test_an_unread_page_rewritten_mid_read_restarts_at_the_new_revision():
    authority = _PagedAuthority(_large_turn(), page_fences=True)
    pages = authority.pages()
    target = _page_holding_round(pages, 20)
    assert target > 1
    authority.before(1, _rewrite_round(20))

    result = read_turn_projection(authority)

    assert result == authority.turn and result['projectionRevision'] == 2
    requested = [request['page'] for request in authority.requests]
    restart = requested.index(0, 1)
    # The held page 1 is kept; the read resumes at the rewritten page.
    assert requested[restart + 1:] == list(range(target, len(pages)))


def test_a_pinned_revision_reads_exactly_that_revision_or_raises():
    authority = _PagedAuthority(_large_turn(), page_fences=True)
    original = orjson.loads(orjson.dumps(authority.turn))
    authority.before(1, _touch_head)
    # Unchanged pages still serve a pinned read after the head moved.
    assert read_turn_projection(authority, projection_revision=1) == original

    with pytest.raises(TurnProjectionRevisionMoved) as moved:
        read_turn_projection(authority, projection_revision=1)
    assert moved.value.projection_revision == 2

    last = len(authority.pages()) - 1
    authority.before(last, _grow_tail)
    with pytest.raises(TurnProjectionRevisionMoved) as moved:
        read_turn_projection(authority, projection_revision=2)
    assert moved.value.projection_revision == 3


def test_an_absent_or_deleted_turn_reads_none():
    absent = _PagedAuthority(None, page_fences=True)
    assert read_turn_projection(absent) is None

    deleted = _PagedAuthority(_large_turn(), page_fences=True)
    deleted.before(2, lambda authority: setattr(authority, 'turn', None))
    assert read_turn_projection(deleted) is None


def _tampered(change):
    authority = _PagedAuthority(_large_turn(), page_fences=False)

    def read(fields):
        page = authority(fields)
        change(fields['page'], page)
        return page
    return read


@pytest.mark.parametrize('change', [
    pytest.param(lambda index, page: index == 1 and page['members']['toolRounds'].update(
        start=page['members']['toolRounds']['start'] + 1), id='gap'),
    pytest.param(lambda index, page: index == 3 and page['members'].clear(), id='short'),
    pytest.param(lambda index, page: index == 1 and page.update(page=2), id='page-number'),
    pytest.param(lambda index, page: index == 0 and page.update(contract='other/v1'), id='contract'),
    pytest.param(lambda index, page: index == 0 and page['pagedMembers'].update(extra=1),
                 id='unknown-member'),
])
def test_an_inconsistent_page_sequence_is_a_typed_protocol_error(change):
    with pytest.raises(StorageError) as refused:
        read_turn_projection(_tampered(change))
    assert refused.value.code == 'database_protocol_error'


def test_an_unfenced_page_zero_never_answers_stale():
    def read(fields):
        return {'contract': TURN_PROJECTION_PAGE_CONTRACT, 'stale': True, 'projectionRevision': 4}

    with pytest.raises(StorageError) as refused:
        read_turn_projection(read)
    assert refused.value.code == 'database_protocol_error'


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native', 'postgres'], indirect=True)
def test_every_authority_reassembles_turn_get_from_its_pages(semantic_authority, monkeypatch):
    from lib.turn_lifecycle import append_settled_turn
    from tests.storage.test_turn_projection_pages import _large_projection

    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    conversation = 'paged-turn-reader'
    append_settled_turn(
        conversation, command_id='human-request', actor='human', user_id=owner,
        projection={'content': 'Inspect every module'},
        conversation_defaults={'allowCreate': True})
    large = append_settled_turn(
        conversation, command_id='large-reply', actor='assistant', user_id=owner,
        projection=_large_projection())['turn']
    identity = {'user_id': owner, 'conversation_id': conversation, 'turn_id': large['turnId']}
    whole = client().query('turn.get', identity)

    assert read_paged_turn(client(), **identity) == whole
    assert read_paged_turn(client(), **identity,
                           projection_revision=whole['projectionRevision']) == whole
    assert read_paged_turn(client(), **identity | {'turn_id': 'missing-turn'}) is None
