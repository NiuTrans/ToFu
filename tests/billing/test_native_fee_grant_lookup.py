"""Fee evidence keeps exact producer, round and owner identity across lookups."""

import pytest

from lib.billing.native_cost import native_task_cost, transfer_native_child_fees
from tests.billing.test_recovered_actor_fee_transfer import child, root, spend

pytestmark = pytest.mark.unit


def charged_family(owner=7):
    parent, account = root("invoice-root", owner)
    for index, rounds in enumerate((1, 2, 3)):
        actor = child(parent, f"actor-{index}")
        for _ in range(rounds):
            spend(actor)
        actor["status"] = "done"
        transfer_native_child_fees(parent, actor)
        account.close(actor)
    return parent


@pytest.mark.parametrize("reverse", [False, True])
def test_overlapping_round_numbers_and_foreign_grants_keep_exact_owned_charge(reverse):
    parent = charged_family()
    foreign = charged_family(owner=8)
    expected = native_task_cost(parent, model="alpha")
    grants = [object()]
    for wrong_owner, owned in zip(
        foreign["_nativeChildFeeGrants"], parent["_nativeChildFeeGrants"]
    ):
        grants.extend((wrong_owner, owned, owned))
    parent["_nativeChildFeeGrants"] = tuple(reversed(grants) if reverse else grants)

    result = native_task_cost(parent, model="alpha")

    assert result == expected
    assert result.micro == 6 * 7593
    assert result.snapshot["roundCount"] == 6
    assert parent["_executionBudget"].consumption_snapshot() == (61200, 0, True)


@pytest.mark.parametrize(
    "damage", ["foreign_owner", "removed_grant", "quote", "source_round"]
)
def test_successful_projection_does_not_cache_permission_for_changed_evidence(damage):
    parent = charged_family()
    assert native_task_cost(parent, model="alpha").micro == 6 * 7593
    if damage == "foreign_owner":
        parent["_nativeChildFeeGrants"] = charged_family(owner=8)[
            "_nativeChildFeeGrants"
        ]
    elif damage == "removed_grant":
        parent["_nativeChildFeeGrants"] = parent["_nativeChildFeeGrants"][:-1]
    elif damage == "quote":
        parent["apiRounds"][-1]["provider_id"] = "changed"
    else:
        parent["apiRounds"][-1]["sourceRound"] += 1

    with pytest.raises(ValueError, match="another executor"):
        native_task_cost(parent, model="alpha")
