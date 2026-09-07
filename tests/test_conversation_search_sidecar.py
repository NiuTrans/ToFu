import pytest
import time
from tests._seed import seed_conversation

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]
pytest_plugins = ('tests._chat_sidecar',)


def _seed(client, cid, text, updated=1):
    del client
    seed_conversation(
        cid,
        messages=[{'role': 'user', 'content': text}],
        title=cid,
        created_at=1,
        updated_at=updated,
    )


def _search_until(client, params, *, minimum_hits=1, timeout_s=5.0):
    """Wait for the independent replayable projection, never the authority."""
    deadline = time.monotonic() + timeout_s
    hits = []
    while time.monotonic() < deadline:
        hits = client.query('conversation.search', params)
        if len(hits) >= minimum_hits:
            return hits
        time.sleep(0.025)
    return hits


def test_conversation_search_like_and_snippet(chat_sidecar):
    from lib.storage import get_storage_client as _gsc
    client = _gsc(write=True)
    _seed(client, 'c-alpha', 'the quick brown fox jumps', updated=2)
    _seed(client, 'c-beta', 'unrelated content here', updated=1)
    _seed(client, 'c-gamma', 'fox and hound are friends', updated=3)

    hits = _search_until(client, {
        'user_id': 1, 'query': 'fox', 'limit': 50, 'snippet_radius': 10},
        minimum_hits=2)
    ids = [h['id'] for h in hits]
    assert set(ids) == {'c-alpha', 'c-gamma'}, ids
    assert ids[0] == 'c-gamma'  # updated_at DESC ordering
    snip = next(h['snippet'] for h in hits if h['id'] == 'c-alpha')
    assert 'fox' in snip and snip.startswith('…') and snip.endswith('…')


def test_conversation_search_multiword_and(chat_sidecar):
    from lib.storage import get_storage_client as _gsc
    client = _gsc(write=True)
    _seed(client, 'c-mw', 'alpha beta gamma delta')
    # non-adjacent multi-word (legacy FTS-AND semantics)
    hits = _search_until(client, {
        'user_id': 1, 'query': 'alpha gamma', 'limit': 50, 'snippet_radius': 40})
    assert [h['id'] for h in hits] == ['c-mw']


def test_conversation_search_short_query_empty(chat_sidecar):
    from lib.storage import get_storage_client as _gsc
    client = _gsc(write=True)
    _seed(client, 'c-x', 'anything')
    assert client.query('conversation.search', {
        'user_id': 1, 'query': 'a', 'limit': 50, 'snippet_radius': 40}) == []
