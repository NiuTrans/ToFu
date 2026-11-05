"""Validate frozen native fees and project settled children into their root.

native_task_cost checks complete account coverage and folds original credits.
transfer_native_child_fees and the two grants bind exact producer/payer evidence
under the shared ExecutionBudget and existing task content lock. No price lookup,
wallet write or new execution authority is created here.

Grants and invoice folding share this owner because each validates the other's
original evidence. CostBreakdown is the common result; native_settlement owns
terminal capsules and wallet recovery, and native_actor_fees exports grants.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass

from lib.billing.cost_types import CostBreakdown


def _fee_row_digest(row):
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(row, sort_keys=True, allow_nan=False).encode()
    ).digest()


def _transferred_child_row(row, parent_id):
    from copy import deepcopy

    result = deepcopy(row)
    result["sourceRound"] = row["round"]
    result["billableTaskId"] = row.get("billableTaskId", parent_id)
    return result


@dataclass(frozen=True, slots=True, init=False)
class NativeChildFeeGrant:
    """Process-local witness over already settled child rows; never a new ledger."""

    parent_id: str
    owner: int
    child_id: str
    execution_parent_id: str
    rows: object
    projected_rows: object
    prefix_digest: bytes
    tokens: int

    def __init__(self, parent, child, account):
        from lib.agent_core.execution_budget import ExecutionBudget

        if not isinstance(account, ExecutionBudget):
            raise ValueError("native child fees require the owned account")
        actual_tokens = account.settled_child_invoice_tokens(parent, child)
        rows = child.get("apiRounds")
        if not isinstance(rows, list):
            raise ValueError("native child quote rows missing")
        detached = dict(
            child,
            _nativePricingRequired=False,
            _nativeFrozenCostProjection=True,
            _executionBudget=None,
        )
        if rows:
            native_task_cost(
                detached, model=(child.get("config") or {}).get("model") or ""
            )
        quoted = sum(
            row["nativePrice"]["prompt_tokens"]
            + (row["cost"]["outputTokens"] if row["cost"] else 0)
            for row in rows
        )
        if quoted != actual_tokens:
            raise ValueError("native child quote coverage differs from settled usage")
        prefix = child.get("_checkpointApiRounds") or []
        if prefix:
            inherited = account.recovered_actor_prefix_tokens(
                child, owner=parent["_userId"]
            )
            prefix_tokens = sum(
                row["nativePrice"]["prompt_tokens"]
                + (row["cost"]["outputTokens"] if row["cost"] else 0)
                for row in prefix
            )
            if inherited is None or inherited != prefix_tokens:
                raise ValueError(
                    "native child fee prefix has no acknowledged account rebind"
                )
        for index, row in enumerate(rows):
            payer = row.get("billableTaskId", parent["id"])
            if (
                "sourceRound" in row
                or not isinstance(row.get("taskId"), str)
                or not row["taskId"]
                or row["taskId"] == parent["id"]
                or not isinstance(payer, str)
                or not payer
                or (
                    index < len(prefix)
                    and (
                        payer in {parent["id"], child["id"], row["taskId"]}
                        or "billableTaskId" not in row
                    )
                )
                or (
                    index >= len(prefix)
                    and (row["taskId"] != child["id"] or payer != parent["id"])
                )
            ):
                raise ValueError("native child fee producer or original payer differs")
        object.__setattr__(self, "parent_id", parent["id"])
        object.__setattr__(self, "owner", parent["_userId"])
        object.__setattr__(self, "child_id", child["id"])
        object.__setattr__(self, "execution_parent_id", child["_parentTaskId"])
        from types import MappingProxyType

        object.__setattr__(
            self,
            "rows",
            MappingProxyType({row["round"]: _fee_row_digest(row) for row in rows}),
        )
        object.__setattr__(
            self,
            "projected_rows",
            MappingProxyType(
                {
                    (row["taskId"], row["round"]): _fee_row_digest(
                        _transferred_child_row(row, parent["id"])
                    )
                    for row in rows
                }
            ),
        )
        object.__setattr__(self, "prefix_digest", _fee_row_digest(prefix))
        object.__setattr__(self, "tokens", actual_tokens)

    def source_rows(self, child):
        """Copy exact witnessed facts after completion; never revive the actor."""
        from copy import deepcopy

        rows = deepcopy(child.get("apiRounds"))
        prefix = deepcopy(child.get("_checkpointApiRounds") or [])
        if (
            child.get("id") != self.child_id
            or type(child.get("_userId")) is not int
            or child["_userId"] != self.owner
            or child.get("_parentTaskId") != self.execution_parent_id
            or getattr(child.get("_executionBudget"), "root_id", None) != self.parent_id
            or not isinstance(rows, list)
            or self.rows != {row["round"]: _fee_row_digest(row) for row in rows}
            or self.prefix_digest != _fee_row_digest(prefix)
        ):
            raise ValueError("Native actor fee evidence changed after transfer")
        return rows

    def allows(self, parent, row):
        if (
            type(parent.get("_userId")) is not int
            or parent.get("id") != self.parent_id
            or parent.get("_userId") != self.owner
        ):
            return False
        source_round = row.get("sourceRound")
        if type(source_round) is not int or source_round < 1:
            return False
        restored = dict(row, round=source_round)
        return self.projected_rows.get(
            (row.get("taskId"), source_round)
        ) == _fee_row_digest(restored)


@dataclass(frozen=True, slots=True, init=False)
class NativeRecoveredChildFeeGrant:
    """Exact retained child rows admitted after the owned account is restored."""

    parent_id: str
    owner: int
    rows: object

    def __init__(self, parent, rows, account_state):
        from types import MappingProxyType
        from lib.agent_core.execution_budget import ExecutionBudget

        account = parent.get("_executionBudget")
        if (
            not isinstance(account, ExecutionBudget)
            or not account.authorizes_model_owner(parent, parent.get("_userId"))
            or parent.get("_executionBudget") is not account
        ):
            raise ValueError("recovered child fees require the restored owned account")
        account.validate_restored_shared_account(
            owner=parent["_userId"], task=parent, snapshot=account_state
        )
        accepted = {}
        for row in rows:
            if row.get("billableTaskId") != parent["id"]:
                continue
            original = dict(row, round=row["sourceRound"])
            original.pop("sourceRound")
            original.pop("billableTaskId")
            native_task_cost(
                dict(
                    id=row["taskId"],
                    apiRounds=[original],
                    _nativeFrozenCostProjection=True,
                ),
                model=row["model"],
            )
            key = (row["taskId"], row["sourceRound"])
            if key in accepted:
                raise ValueError("recovered child fee duplicated")
            accepted[key] = _fee_row_digest(row)
        object.__setattr__(self, "parent_id", parent["id"])
        object.__setattr__(self, "owner", parent["_userId"])
        object.__setattr__(self, "rows", MappingProxyType(accepted))

    def allows(self, parent, row):
        if (
            parent.get("id") != self.parent_id
            or type(parent.get("_userId")) is not int
            or parent["_userId"] != self.owner
        ):
            return False
        key = (row.get("taskId"), row.get("sourceRound"))
        return self.rows.get(key) == _fee_row_digest(row)


def transfer_native_child_fees(parent, child):
    """Append original quotes once to the parent's existing apiRounds projection."""
    from lib.cost import compute_api_rounds_cost

    account = parent.get("_executionBudget")
    if account is None:
        raise ValueError("native child fees require the shared account")
    from lib.agent_core.execution_budget import ExecutionBudget

    if not isinstance(account, ExecutionBudget):
        raise ValueError("native child fees require the owned account")
    with account.child_invoice_projection(parent, child):
        with parent.get("content_lock") or nullcontext():
            grants = parent.get("_nativeChildFeeGrants", ())
            previous = next(
                (
                    value
                    for value in grants
                    if isinstance(value, NativeChildFeeGrant)
                    and value.child_id == child["id"]
                ),
                None,
            )
            if previous is not None:
                rows = child.get("apiRounds")
                if (
                    type(parent.get("_userId")) is not int
                    or type(child.get("_userId")) is not int
                    or parent.get("id") != previous.parent_id
                    or parent.get("_userId") != previous.owner
                    or child.get("_userId") != previous.owner
                    or child.get("_parentTaskId") != previous.execution_parent_id
                    or child.get("_executionBudget") is not account
                    or not isinstance(rows, list)
                    or previous.rows
                    != {row["round"]: _fee_row_digest(row) for row in rows}
                    or previous.prefix_digest
                    != _fee_row_digest(child.get("_checkpointApiRounds") or [])
                ):
                    raise ValueError("native child fee retry changed its candidate")
                accepted = [
                    row
                    for row in parent.get("apiRounds", [])
                    if (row.get("taskId"), row.get("sourceRound"))
                    in previous.projected_rows
                ]
                if len(accepted) != len(previous.rows) or not all(
                    previous.allows(parent, row) for row in accepted
                ):
                    raise ValueError(
                        "native parent lost its transferred child fee projection"
                    )
                child["_nativeChildFeeGrant"] = previous
                return
            grant = NativeChildFeeGrant(parent, child, account)
            rounds = parent.get("apiRounds") or []
            if not isinstance(rounds, list):
                raise ValueError("native parent quote rows invalid")
            updated = [*rounds]
            for original in child["apiRounds"]:
                row = _transferred_child_row(original, parent["id"])
                row["round"] = (updated[-1]["round"] if updated else 0) + 1
                updated.append(row)
            cost = compute_api_rounds_cost(updated, use_frozen_costs=True)
            parent["apiRounds"] = updated
            parent["cost"] = cost
            parent["_nativeChildFeeGrants"] = (*grants, grant)
            child["_nativeChildFeeGrant"] = grant


def _grants_by_source(grants):
    """Find candidate witnesses without hashing unrelated producers' rows.

    An index hit grants no authority: allows() still checks the owner and exact
    evidence. Keep all candidates for a source, including recovered witnesses;
    an unrelated or stale candidate must not hide a valid one.
    """
    indexed = {}
    for grant in grants:
        if isinstance(grant, NativeChildFeeGrant):
            sources = grant.projected_rows
        elif isinstance(grant, NativeRecoveredChildFeeGrant):
            sources = grant.rows
        else:
            continue
        for source in sources:
            indexed.setdefault(source, []).append(grant)
    return indexed


def native_task_cost(task: dict, *, model: str) -> CostBreakdown:
    """Fold settled Rust credits for the existing wallet; never reprice them."""
    if (
        task.get("_nativePricingRequired") is not True
        and task.get("_nativeFrozenCostProjection") is not True
    ):
        raise ValueError("native wallet cost requires frozen task projection")
    budget = task.get("_executionBudget")
    if task.get("_nativePricingRequired") is True and budget is None:
        raise ValueError("native wallet cost requires the owned financial account")
    account = None if budget is None else budget.consumption_snapshot()
    if account is not None and account[2] is not True:
        raise ValueError("native wallet cost has unknown financial usage")
    rounds = task.get("apiRounds", [])
    if not isinstance(rounds, list) or (
        not rounds and (account is None or account[0] != 0)
    ):
        raise ValueError("native wallet cost requires settled round evidence")
    prefix = task.get("_checkpointApiRounds")
    prefix = [] if prefix is None else prefix
    if not isinstance(prefix, list) or len(prefix) > len(rounds):
        raise ValueError(
            "native wallet checkpoint prefix differs from accepted round evidence"
        )
    task_id = task.get("id")
    keys = {"input", "output", "cache_read", "cache_write", "base", "margin", "total"}
    totals = dict.fromkeys(keys, 0)
    previous = 0
    quoted_tokens = 0
    billable_rounds = 0
    transferred = set()
    grants_by_source = None
    for position, row in enumerate(rounds):
        if (
            not isinstance(row, dict)
            or type(row.get("round")) is not int
            or row["round"] <= previous
            or "cost" not in row
            or not isinstance(row.get("nativePrice"), dict)
        ):
            raise ValueError("native wallet round evidence invalid")
        credits = row.get("nativeCredits")
        if (
            not isinstance(credits, dict)
            or set(credits) != keys
            or any(
                type(value) is not int or not 0 <= value <= 9223372036854775807
                for value in credits.values()
            )
            or credits["base"]
            != sum(
                credits[key] for key in ("input", "output", "cache_read", "cache_write")
            )
            or credits["total"] != credits["base"] + credits["margin"]
        ):
            raise ValueError("native wallet credits invalid")
        previous = row["round"]
        price, amounts = row["nativePrice"], row["cost"]
        prompt = price.get("prompt_tokens")
        if type(prompt) is not int or prompt < 0:
            raise ValueError("native wallet prompt evidence invalid")
        if amounts is None:
            if prompt or any(credits.values()):
                raise ValueError("native empty quote cannot carry a charge")
        elif (
            not isinstance(amounts, dict)
            or amounts.get("pricingSource") != "native_resolved_price"
            or amounts.get("totalInputTokens") != prompt
            or type(amounts.get("outputTokens")) is not int
            or amounts["outputTokens"] < 0
        ):
            raise ValueError("native wallet amount evidence invalid")
        quoted_tokens += prompt + (0 if amounts is None else amounts["outputTokens"])
        if position < len(prefix):
            if row != prefix[position]:
                raise ValueError(
                    "native wallet checkpoint prefix differs from accepted round evidence"
                )
            source = row.get("taskId")
            if not isinstance(source, str) or not source or source == task_id:
                raise ValueError(
                    "native wallet resume requires distinct recorded executor provenance"
                )
            # Old execution still consumes the shared token budget. Its wallet
            # debt belongs to its original task id, including lost-ACK recovery.
            continue
        if row.get("taskId", task_id) != task_id:
            if (
                not isinstance(row.get("taskId"), str)
                or type(row.get("sourceRound")) is not int
            ):
                raise ValueError("native child quote provenance invalid")
            key = (row["taskId"], row["sourceRound"])
            if key in transferred:
                raise ValueError(
                    "native wallet fresh quote belongs to another executor"
                )
            if grants_by_source is None:
                grants_by_source = _grants_by_source(
                    task.get("_nativeChildFeeGrants", ())
                )
            if not any(
                grant.allows(task, row) for grant in grants_by_source.get(key, ())
            ):
                raise ValueError(
                    "native wallet fresh quote belongs to another executor"
                )
            transferred.add(key)
            if row.get("billableTaskId") != task_id:
                # An exact witnessed historical row still consumes tokens, but
                # its debt remains with the original root across actor handoffs.
                continue
        billable_rounds += 1
        for key in keys:
            totals[key] += credits[key]
            if totals[key] > 9223372036854775807:
                raise ValueError("native wallet total exceeds storage integer range")
    if account is not None and account[0] != quoted_tokens:
        raise ValueError("native wallet quotes do not cover settled financial usage")
    components = {
        key: totals[key] for key in ("input", "output", "cache_read", "cache_write")
    }
    snapshot = {
        "source": "native_resolved_price",
        "roundCount": billable_rounds,
        "componentsMicro": dict(
            components,
            baseMicro=totals["base"],
            marginMicro=totals["margin"],
            totalMicro=totals["total"],
        ),
    }
    return CostBreakdown(
        totals["total"], totals["base"], totals["margin"], model, components, snapshot
    )
