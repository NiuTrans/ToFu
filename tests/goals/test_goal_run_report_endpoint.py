"""Owner/conversation-scoped full stop-report reads (fold lazy boundary)."""

import pytest

from lib.goal_runs.control import get_goal_run_report

pytestmark = pytest.mark.unit


class _FakeRepository:
    def __init__(self, user_id):
        self.user_id = user_id

    def report_for_run(self, run_id):
        if self.user_id != 7:
            return None
        if run_id != 'goal-known':
            return None
        return {
            'runId': 'goal-known',
            'conversationId': 'conv-goal',
            'report': 'the complete terminal report',
        }


@pytest.fixture
def fake_repository(monkeypatch):
    import lib.goal_runs.repository as repository_module
    monkeypatch.setattr(
        repository_module, 'SidecarGoalRunRepository', _FakeRepository)


def test_report_read_returns_the_full_text(fake_repository):
    assert get_goal_run_report('conv-goal', 'goal-known', user_id=7) == {
        'runId': 'goal-known',
        'report': 'the complete terminal report',
    }


def test_report_read_refuses_foreign_or_unknown_runs(fake_repository):
    # The run belongs to a different conversation.
    assert get_goal_run_report('conv-other', 'goal-known', user_id=7) == {
        'error': 'report_not_found'}
    # Unknown run id.
    assert get_goal_run_report('conv-goal', 'goal-missing', user_id=7) == {
        'error': 'report_not_found'}
    # Malformed ids fail closed without touching storage.
    assert get_goal_run_report('', 'goal-known', user_id=7) == {
        'error': 'report_unavailable'}
