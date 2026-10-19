"""Sidebar preset contract: creation does not require an existing project."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest
from lib.conversations.folders import FolderRepository, FolderRevisionConflict
from lib.request_parser import BadRequest
from lib.json_store import JsonStoreReadError

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path):
    return FolderRepository(str(tmp_path / 'folders.json'), legacy_owner_user_id=1)


def test_name_only_create_and_round_trip(repo):
    folder = repo.create(7, {'name': ' My project '})
    assert folder['name'] == 'My project' and folder['projectPaths'] == []
    assert folder['readOnlyPaths'] == [] and folder['revision'] == 1
    assert repo.list(7) == [folder]
    assert repo.list(8) == []


def test_roots_order_primary_access_and_revision(repo):
    folder = repo.create(7, {'name': 'Work', 'projectPaths': [' /code ', '/ref', '/code'], 'readOnlyPaths': ['/ref']})
    assert folder['projectPaths'] == ['/code', '/ref'] and folder['projectPath'] == '/code'
    updated = repo.update(7, folder['id'], {'projectPaths': ['/ref', '/code'], 'expectedRevision': 1})
    assert updated['projectPath'] == '/ref' and updated['readOnlyPaths'] == ['/ref']
    assert updated['revision'] == 2
    with pytest.raises(FolderRevisionConflict): repo.update(7, folder['id'], {'name': 'stale', 'expectedRevision': 1})
    assert repo.list(7)[0]['name'] == 'Work'
    cleared = repo.update(7, folder['id'], {'projectPaths': [], 'expectedRevision': 2})
    assert cleared['projectPaths'] == [] and cleared['readOnlyPaths'] == []


@pytest.mark.parametrize('updates', [
    {'projectPaths': 'not an array'}, {'projectPaths': ['/ok', 4]},
    {'projectPaths': ['/ok', '']}, {'projectPaths': ['/x' * 300]},
    {'projectPaths': [f'/root{i}' for i in range(33)]},
    {'projectPaths': ['/ok'], 'readOnlyPaths': ['/foreign']},
    {'projectPaths': ['/a'], 'projectPath': '/b'}, {'name': '   '},
    {'expectedRevision': True}, {'collapsed': 'false'},
])
def test_bad_edits_do_not_clear_or_partially_save_defaults(repo, updates):
    folder = repo.create(7, {'name': 'confirmed', 'projectPaths': ['/old']})
    before = repo.list(7)
    with pytest.raises(BadRequest): repo.update(7, folder['id'], updates)
    assert repo.list(7) == before


def test_ownership_on_mutation_and_reorder(repo):
    one = repo.create(7, {'name': 'A'}); two = repo.create(8, {'name': 'B'})
    assert repo.update(8, one['id'], {'name': 'foreign'}) is None
    assert not repo.delete(8, one['id'])
    repo.reorder(8, [one['id'], two['id']])
    assert repo.list(7) == [one]
    assert repo.list(8)[0]['order'] == 0


def test_unowned_legacy_records_belong_only_to_explicit_bootstrap_owner(repo):
    with open(repo.path, 'w') as file:
        json.dump([{'id': 'legacy', 'name': 'Legacy', 'projectPath': '/legacy'}], file)
    assert repo.list(8) == []
    assert repo.list(1)[0]['projectPaths'] == ['/legacy']
    assert repo.update(8, 'legacy', {'name': 'claim'}) is None
    assert repo.update(1, 'legacy', {'name': 'kept', 'expectedRevision': 0})['revision'] == 1
    assert repo.list(1)[0]['projectPath'] == '/legacy'


def test_corrupt_store_never_becomes_successful_empty_create(repo):
    with open(repo.path, 'w') as file: file.write('{not valid json')
    with pytest.raises(JsonStoreReadError): repo.create(7, {'name': 'unsafe'})
    assert open(repo.path).read() == '{not valid json'


def test_two_concurrent_editors_get_exactly_one_cas_success(repo):
    folder = repo.create(7, {'name': 'start'})
    def write(name):
        try: return repo.update(7, folder['id'], {'name': name, 'expectedRevision': 1})
        except FolderRevisionConflict: return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(write, ['first', 'second']))
    assert sum(row is not None for row in result) == 1
    assert repo.list(7)[0]['revision'] == 2
