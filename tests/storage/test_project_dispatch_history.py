"""Bounded passive history across authorities, direction changes and cutover."""
import pytest

from lib.project_dispatch.repository import ProjectDispatchError
from tests.project_mod.test_project_dispatch_repository import context
from tests.storage.test_project_dispatch_repository_segments import owned, authorize

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def test_every_assignment_is_reachable_without_retaining_previous_pages(owned):
    c = owned
    expected = [authorize(c, str(index)) for index in range(90)]
    head = c.store.head()
    first = c.repo.history_page()
    second = c.repo.history_page(first['olderCursor'])
    third = c.repo.history_page(second['olderCursor'])
    assert first['newerCursor'] is None and third['olderCursor'] is None
    assert first['rows'] + second['rows'] + third['rows'] == expected[::-1]
    assert len(first['rows']) == len(second['rows']) == 32
    assert c.repo.history_page(third['newerCursor']) == second
    assert c.repo.history_page(second['newerCursor']) == first
    assert c.store.head() == head
    authorize(c, 'new-arrival')
    assert c.repo.history_page(first['olderCursor']) == second
    with pytest.raises(ProjectDispatchError): c.repo.history_page('older/another-project:1')


def test_passive_legacy_pages_keep_their_cursor_after_cutover(owned):
    c = owned; c.repo.storage_version = 1
    expected = [authorize(c, str(index)) for index in range(40)]
    c.repo.storage_version = 2
    first = c.repo.history_page()
    assert c.store.head() is None
    authorize(c, 'after-cutover')
    second = c.repo.history_page(first['olderCursor'])
    assert second['rows'] == expected[:8][::-1] and second['olderCursor'] is None
    previous = c.repo.history_page(second['newerCursor'])
    assert previous['rows'] == first['rows'] and previous['newerCursor']


def test_service_page_preserves_exact_lookup_and_owner_authority(owned):
    from lib.project_dispatch.service import ProjectDispatchService
    from lib.identity import PrincipalContext
    c = owned
    expected = [authorize(c, str(index)) for index in range(35)]
    service = ProjectDispatchService(c.repo, turn_get=c.repo.turn_get, attempt_get=lambda *a, **kw: None)
    first = service.history_page(principal=c.principal)
    assert len(first['assignments']) == 32 and first['olderCursor'] and first['newerCursor'] is None
    head = c.store.head()
    exact = service.history_page(principal=c.principal, assignment_id=expected[0]['id'])
    replay = service.history_page(principal=c.principal, command_id='0')
    assert exact == replay and exact['assignments'][0]['assignmentId'] == expected[0]['id']
    assert exact['olderCursor'] is exact['newerCursor'] is None and c.store.head() == head
    foreign = PrincipalContext.user(subject_id='foreign', owner_user_id=c.repo.owner + 1, scopes=['chat'])
    with pytest.raises(PermissionError): service.history_page(principal=foreign, cursor=first['olderCursor'])


def test_cutover_during_passive_read_never_synthesizes_wrong_order_cursor(owned, monkeypatch):
    from lib.project_dispatch.segmented import SegmentedDispatch
    c = owned
    for number in range(2): authorize(c, str(number))
    request = {name: c.request[name] for name in ('projectPath', 'coordinatorId', 'authorizationSource', 'budget')}
    request.update(objective='One owned batch', workers=[{name: c.request[name] for name in ('recipientId', 'brief', 'model', 'budget')}])
    c.repo.authorize_batch(request, principal=c.principal, command_id='batch')
    for number in range(33): authorize(c, 'later-' + str(number))
    expected = c.repo.history_page()
    real = SegmentedDispatch.head; calls = []
    def head(store):
        calls.append(True)
        return None if len(calls) == 1 else real(store)
    monkeypatch.setattr(SegmentedDispatch, 'head', head)
    assert c.repo.history_page() == expected
