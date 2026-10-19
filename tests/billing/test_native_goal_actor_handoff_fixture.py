"""The native whole-actor handoff uses the actual shared-account producer."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tests.billing.test_recovered_actor_fee_transfer import root, child

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).resolve().parents[2] / 'packages/tofu-db/tests/fixtures'


def produce():
    source = json.loads((FIXTURES / 'native_goal_actor_claim.json').read_text())['cut']
    task, budget = root('target-root')
    budget.restore_pending_frontier(task, source, owner=7)
    ordinary, batches = [], []
    budget.bind_account_frontier_observer(lambda cut: ordinary.append(deepcopy(cut)),
        actor_rebind_observer=lambda cuts: batches.append(deepcopy(cuts)), actor_rebind_bytes=1024 * 1024)
    actors = [(child(task, 'target-a'), 'pending-a'), (child(task, 'target-b'), 'pending-b')]
    before = deepcopy(ordinary[-1])
    budget.rebind_all_actor_prefixes(actors, owner=7)
    return dict(before=before, frontiers=list(batches[0]))


def test_native_goal_actor_handoff_fixture_matches_shared_budget_batch():
    assert produce() == json.loads((FIXTURES / 'native_goal_actor_handoff.json').read_text())
