"""Boot audit must not race the immutable source cut issued to an executor."""
import pytest

from lib.orchestration.continuity import run_boot_continuity_pass
from tests.orchestration.test_continuity_durable_election import create, retire
from tests.orchestration.test_orchestration_continuity import _service_factories
from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('outcome', ['accepted', 'unknown', 'preflight_refused', 'already_claimed'])
def test_source_bound_submission_preserves_issued_cursor(semantic_authority, outcome):
    from lib.orchestration.execution_journal import RecoveryRefused
    client, owner, _ = semantic_authority
    create(client, owner, 'source-bound')
    candidates = retire(client).candidates
    runs, definitions = _service_factories(client())
    observed = []
    def resume(service, source, *, before_submit, **scope):
        if outcome == 'preflight_refused':
            raise RecoveryRefused('controlled refusal before admission')
        if outcome == 'already_claimed':
            page = service.replay(source, 0)
            assert service.append_event(source, page.next_cursor, dict(type='execution_recovery_claim', successor_run_id='successor'))
            observed.append(page.next_cursor+1)
            return 'successor'
        before_submit()
        page = service.replay(source, 0)
        assert page.events[-1]['type'] == 'continuity_deferred'
        observed.append(page.next_cursor)
        if outcome == 'unknown':
            raise RuntimeError('controlled unknown acknowledgement after submission')
        return 'successor'
    result = run_boot_continuity_pass(candidates=candidates, runtime=object(),
        auto_created_by={'chat_goal_mode'}, source_cursor_bound_created_by={'chat_goal_mode'},
        run_service_for_owner=runs, definition_service_for_owner=definitions,
        resume_for_created_by={'chat_goal_mode': resume})
    final = runs(owner, '').replay('source-bound', 0)
    if outcome == 'preflight_refused':
        assert result['refused'] == 1 and not observed
        assert final.events[-1]['type'] == 'continuity_refused'
        assert not retire(client).candidates
    else:
        assert result['deferred' if outcome == 'unknown' else 'resumed'] == 1
        assert final.next_cursor == observed[0]
        assert retire(client).candidates == (() if outcome == 'already_claimed' else candidates)
