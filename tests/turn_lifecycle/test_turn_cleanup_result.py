"""Attempt cleanup keeps a strict public count across authority response shapes."""
from types import SimpleNamespace
import pytest

pytestmark = pytest.mark.unit

@pytest.mark.parametrize('response', [3, {'deletedAttempts': 3, 'moreCandidates': True}])
def test_cleanup_count_preserves_deleted_rows(monkeypatch, response):
    import lib.turn_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, '_turn_client', lambda **kw: SimpleNamespace(command=lambda *a, **k: response))
    assert lifecycle.cleanup_superseded_attempts() == 3

@pytest.mark.parametrize('response', [None, {}, {'deletedAttempts': True}, -1, '3'])
def test_cleanup_malformed_count_is_not_reported_as_success(monkeypatch, response):
    import lib.turn_lifecycle as lifecycle
    from lib.storage.errors import StorageError
    monkeypatch.setattr(lifecycle, '_turn_client', lambda **kw: SimpleNamespace(command=lambda *a, **k: response))
    with pytest.raises(StorageError, match='Invalid Attempt cleanup count'):
        lifecycle.cleanup_superseded_attempts()
