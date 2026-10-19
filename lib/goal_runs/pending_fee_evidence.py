"""Read complete Goal actor fee witnesses before atomic source retirement.

Entry preflight_pending_fees: bind exact completed receipts and pending native
versions to the inspected launch/cut. Return a wallet capsule or cost-only proof;
no writes, financial grants, leases or executor admission occur.
Dependencies: pending_recovery, invocation_recovery and billing fee projections.
"""
from copy import deepcopy
from dataclasses import dataclass

from lib.orchestration.execution_journal import RecoveryRefused


@dataclass(frozen=True)
class PendingGoalFees:
    source_run_id: str
    source_launch_id: str
    account_frontier: dict
    actors: dict
    invoice: dict


def preflight_pending_fees(evidence, pending, *, storage, wallet_user_id, reserved_micro):
    from lib.billing.native_actor_fees import from_checkpoint
    from lib.billing.native_aggregate_fees import project
    from lib.orchestration.invocation_recovery import completion_binding, read_registered_actor_fees
    metadata, journal = evidence.metadata, evidence.journal
    try:
        if (pending.source_run_id != metadata['run_id'] or pending.source_launch_id != metadata['launch_id']
                or pending.account_frontier != evidence.account_frontier
                or set(pending.actors) != set(journal.pending_agents) or journal.parent_run_id):
            raise RecoveryRefused('Pending Goal fee evidence changed its launch or account cut')
        actors = {}
        for key, observed in pending.actors.items():
            admission = observed['admission']
            if (admission != journal.pending_agents[key]
                    or any(type(version) is not int or version < 1 for version in (
                        observed['registration_version'], observed['frontier']['version']))):
                raise RecoveryRefused('Pending Goal fee evidence changed its admission')
            binding = completion_binding(admission)
            if (binding['identity']['user_id'] != metadata['owner_user_id']
                    or binding['identity']['conversation_id'] != metadata['conversation_id']
                    or binding['identity']['parent_task_id'] != metadata['task_id']
                    or binding['account_root_task_id'] != metadata['task_id']):
                raise RecoveryRefused('Pending Goal fee evidence has foreign actor origins')
            fees = from_checkpoint(observed['frontier']['snapshot'], owner_user_id=metadata['owner_user_id'],
                root_task_id=metadata['task_id'], actor_task_id=admission['invocation_id'],
                tenant_id=str(metadata.get('tenant_id') or ''))
            actors[key] = dict(admission=admission, registration_version=observed['registration_version'],
                checkpoint_version=observed['frontier']['version'], evidence=fees)
        for key, receipt in journal.records.items():
            if receipt.get('kind') != 'agent':
                continue
            if key in actors:
                raise RecoveryRefused('Goal fee inventory duplicates an unfinished effect')
            admission = dict(type='execution_effect_started', key=key, **{
                field: receipt[field] for field in ('kind', 'input_hash', 'invocation_id', 'invocation_completion')})
            binding = completion_binding(admission)
            parent = binding['identity']['parent_task_id']
            if binding.get('account_root_task_id') != parent:
                raise RecoveryRefused('Goal fee evidence requires a direct registered actor')
            fees = read_registered_actor_fees(storage, admission, owner_user_id=metadata['owner_user_id'],
                conversation_id=metadata['conversation_id'], parent_task_id=parent, expected_receipt=receipt)
            if fees is None:
                raise RecoveryRefused('Completed Goal actor has no original native fee evidence')
            actors[key] = dict(admission=admission, **fees)
        invoice = project(pending.account_frontier, [row['evidence'] for row in actors.values()],
            owner_user_id=metadata['owner_user_id'], root_task_id=metadata['task_id'],
            wallet_user_id=wallet_user_id, reserved_micro=reserved_micro, model=metadata['launch']['model'])
        return PendingGoalFees(metadata['run_id'], metadata['launch_id'], deepcopy(pending.account_frontier),
            deepcopy(actors), invoice)
    except RecoveryRefused:
        raise
    except Exception as exc:
        raise RecoveryRefused('Goal aggregate native fees could not be verified') from exc
