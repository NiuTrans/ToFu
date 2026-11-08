"""A server restart pauses a task; it is not the task's outcome.

A restart-paused attempt records nothing terminal, so the resumed attempt of
the same workspace task settles delivery. A user Stop or a failure stays sticky:
both storage authorities refuse to change a recorded denial. Decision record:
"Restart is not a task outcome" in docs/modules/project_integration.md.
"""
import pytest

from lib import integration_state_repository as state
from lib.project_mod import delivery
from tests.storage.test_integration_delivery import (
    repo as repo_fixture, automatic_workspace,
)

repo = repo_fixture
pytestmark = pytest.mark.unit


def receipt(root):
    return state.get_workspace(str(root), 'automatic', user_id=1)['origin']['delivery']


def settle(root, outcome):
    delivery.settle_task_workspace(str(root), 'automatic', user_id=1, outcome=outcome)
    delivery.resume_pending_workspace_deliveries()
    return receipt(root)


def test_restart_pause_captures_nothing_and_the_resumed_completion_publishes(repo, monkeypatch):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'new.txt').write_text('first half\n')
    paused = settle(root, 'interrupted')
    assert (paused['state'], paused['taskOutcome']) == ('working', 'interrupted')
    # The receiver neither captures nor preserves paused work, however often it runs.
    for _ in range(2):
        delivery.resume_pending_workspace_deliveries()
    assert receipt(root) == paused
    assert not state.get_workspace(str(root), 'automatic', user_id=1).get('checkpoint_sha')
    (work / 'new.txt').write_text('first half\nsecond half\n')
    done = settle(root, 'completed')
    assert (done['state'], done['taskOutcome']) == ('delivered', 'completed')
    assert (root / 'new.txt').read_text() == 'first half\nsecond half\n'


def test_restart_pause_twice_still_publishes_the_final_completion(repo, monkeypatch):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'new.txt').write_text('resumed twice\n')
    assert settle(root, 'interrupted')['state'] == 'working'
    assert settle(root, 'interrupted')['state'] == 'working'
    assert settle(root, 'completed')['state'] == 'delivered'
    assert (root / 'new.txt').read_text() == 'resumed twice\n'


@pytest.mark.parametrize('outcome', ['cancelled', 'failed', 'stopped'])
def test_stop_or_failure_stays_sticky_across_a_restart_pause(repo, monkeypatch, outcome):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'new.txt').write_text('stopped work\n')
    assert settle(root, outcome)['state'] == 'preserved'
    assert settle(root, 'interrupted')['state'] == 'preserved'
    assert settle(root, 'completed')['state'] == 'preserved'
    assert receipt(root)['taskOutcome'] == outcome
    assert not (root / 'new.txt').exists()


def test_a_recorded_denial_is_never_replaced_by_a_restart_pause(repo, monkeypatch):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'new.txt').write_text('in flight\n')
    # A Stop recorded before capture keeps its denial even when a pause follows.
    delivery.record_task_workspace_intent(str(root), 'automatic', user_id=1, outcome='cancelled')
    held = delivery.record_task_workspace_intent(str(root), 'automatic', user_id=1,
                                                 outcome='interrupted')
    assert (held['state'], held['taskOutcome']) == ('pending', 'cancelled')
    assert settle(root, 'completed')['state'] == 'preserved'
    assert not (root / 'new.txt').exists()


def test_delivered_work_is_never_reopened_by_a_restart_pause(repo, monkeypatch):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'new.txt').write_text('published\n')
    delivered = settle(root, 'completed')
    assert delivered['state'] == 'delivered'
    assert settle(root, 'interrupted') == delivered
    assert (root / 'new.txt').read_text() == 'published\n'
