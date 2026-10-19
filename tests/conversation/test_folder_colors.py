"""Folder colors are palette-compatible hex tokens, never markup or arbitrary CSS."""
import json
from pathlib import Path

import pytest

from lib.conversations.folders import FolderRepository
from lib.request_parser import BadRequest

pytestmark = pytest.mark.unit
MALICIOUS_COLOR = '\"><svg/onload=alert(1)>'
VALID_COLORS = ['', '#abc', '#ABC', '#123', '#aBcDeF', '#123456',
                '#6e56cf', '#3b82f6', '#10b981', '#f59e0b', '#ef4444',
                '#ec4899', '#8b5cf6', '#14b8a6']
INVALID_COLORS = [MALICIOUS_COLOR, 'red', '#abcd', '#12345678', '#ggg', 'abc',
                  ' #abc', '#abc ', '#abc\n', 'var(--text-primary)',
                  '#abc;background:red', None, 123, [], {}]


@pytest.fixture
def repo(tmp_path):
    return FolderRepository(str(tmp_path / 'folders.json'), legacy_owner_user_id=1)


@pytest.mark.parametrize('color', VALID_COLORS)
def test_valid_colors_round_trip_create_and_update(repo, color):
    created = repo.create(7, {'name': 'Palette', 'color': color})
    assert created['color'] == color
    assert repo.list(7)[0]['color'] == color
    updated = repo.update(7, created['id'], {'color': color, 'expectedRevision': 1})
    assert updated['color'] == color
    assert repo.list(7)[0]['color'] == color


@pytest.mark.parametrize('color', INVALID_COLORS)
def test_invalid_colors_reject_create_and_update_without_storage_changes(repo, color):
    created = repo.create(7, {'name': 'Confirmed', 'color': '#abc'})
    store = Path(repo.path)
    before = store.read_bytes()
    with pytest.raises(BadRequest) as create_error:
        repo.create(7, {'name': 'Unsafe', 'color': color})
    assert create_error.value.field == 'color'
    with pytest.raises(BadRequest) as update_error:
        repo.update(7, created['id'], {'name': 'Partial change', 'color': color})
    assert update_error.value.field == 'color'
    assert store.read_bytes() == before
    assert repo.list(7) == [created]


@pytest.mark.parametrize('color', INVALID_COLORS)
def test_unsafe_historical_color_is_sanitized_in_views_not_storage(repo, color):
    store = Path(repo.path)
    row = {'id': 'legacy', 'name': 'Historical', 'color': color, 'projectPath': '/code'}
    store.write_text(json.dumps([row]))
    before = store.read_bytes()
    assert repo.list(1)[0]['color'] == ''
    assert store.read_bytes() == before
    # A name-only update must not rewrite the historical color either; its ACK is safe.
    updated = repo.update(1, 'legacy', {'name': 'Renamed'})
    assert updated['color'] == ''
    assert json.loads(store.read_text())[0]['color'] == color


@pytest.mark.parametrize('color', VALID_COLORS)
def test_safe_historical_colors_are_preserved(repo, color):
    store = Path(repo.path)
    store.write_text(json.dumps([{'id': 'legacy', 'name': 'Historical', 'color': color}]))
    before = store.read_bytes()
    assert repo.list(1)[0]['color'] == color
    assert store.read_bytes() == before
