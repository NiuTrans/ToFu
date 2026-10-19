"""Shared reconciliation of Tofu's public billed rounds and accounting witness.

Successful and failed projections reuse these checks. Per-call API rounds are
charged once; compaction summaries are cross-checks, not additional calls.
Counter normalization and provider format semantics live in benchmark_contract.
"""

from lib.benchmark_contract import BenchmarkContractError, canonical_benchmark_usage


def partition_billed_rounds(rows: list) -> tuple[list, list]:
    if not isinstance(rows, list):
        raise BenchmarkContractError('runtime evidence lacks API rounds')
    main, compaction = [], []
    for row in rows:
        if not isinstance(row, dict):
            raise BenchmarkContractError('billed API round is not an object')
        number = row.get('round')
        if type(number) is int and number > 0:
            if row.get('kind') == 'compaction':
                raise BenchmarkContractError('compaction round has an agent round ID')
            main.append(row)
        elif number == f'compaction-{len(compaction) + 1}':
            compaction.append(row)
        else:
            raise BenchmarkContractError('billed compaction rounds are not sequential compaction-<k>')
    return main, compaction


def validate_billed_model(row: dict, logical_model: str, physical_model: str) -> None:
    if row.get('model') not in (None, '', logical_model, physical_model):
        raise BenchmarkContractError('billed API call used another model')
    dispatch = row['usage'].get('_dispatch')
    if isinstance(dispatch, dict) and dispatch.get('model') not in (None, '', physical_model):
        raise BenchmarkContractError('billed API call used a different physical model')


def reconcile_compaction_usage(raw: dict | None, rounds: list, *,
                               logical_model: str, physical_model: str) -> tuple[dict, int]:
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise BenchmarkContractError('compactionUsage must be an object')
    calls = raw.get('n_calls', 0)
    if type(calls) is not int or calls < 0:
        raise BenchmarkContractError('compactionUsage.n_calls must be a non-negative integer')
    if calls != len(rounds):
        raise BenchmarkContractError('billed compaction rounds differ from compactionUsage.n_calls')
    summary = canonical_benchmark_usage(raw if calls else {
        'prompt_tokens': 0, 'completion_tokens': 0, **raw})
    if not calls and (any(summary.values()) or raw.get('total_tokens', 0)):
        raise BenchmarkContractError('compaction usage has tokens but no model calls')
    details = raw.get('calls')
    if 'calls' in raw and (not isinstance(details, list) or len(details) != calls):
        raise BenchmarkContractError('compactionUsage.calls differs from billed calls')
    total = {key: 0 for key in summary}
    for index, row in enumerate(rounds):
        usage = canonical_benchmark_usage(row.get('usage'))
        validate_billed_model(row, logical_model, physical_model)
        for key in total:
            total[key] += usage[key]
        if 'calls' in raw:
            call = details[index]
            if not isinstance(call, dict) or canonical_benchmark_usage(call.get('usage')) != usage:
                raise BenchmarkContractError('compaction call usage differs from billed API round')
            validate_billed_model(call, logical_model, physical_model)
    if total != summary:
        raise BenchmarkContractError('compaction summary usage differs from billed API rounds')
    return summary, calls


def validate_usage_accounting(evidence: dict, total_tokens: int) -> None:
    """Reject known incompleteness; absent/unobserved is not a completeness proof."""
    accounting = evidence.get('usageAccounting')
    if accounting is None:
        return
    if not isinstance(accounting, dict):
        raise BenchmarkContractError('usageAccounting must be an object')
    if not {'complete', 'observedTokens', 'scope', 'rootTaskId'} <= accounting.keys():
        raise BenchmarkContractError('usageAccounting lacks its required observation fields')
    complete = accounting.get('complete')
    if complete is False:
        raise BenchmarkContractError('runtime usage accounting is incomplete or still pending')
    if complete is None:
        if accounting['observedTokens'] is not None or accounting['scope'] != 'unobserved' \
                or accounting['rootTaskId'] != '':
            raise BenchmarkContractError('unobserved usage accounting cannot claim measured tokens')
        return
    if complete is not True or accounting.get('scope') != 'execution_tree' \
            or not isinstance(accounting.get('rootTaskId'), str) or not accounting['rootTaskId'] \
            or accounting.get('rootTaskId') != evidence.get('taskId'):
        raise BenchmarkContractError('runtime usage accounting lacks its execution tree witness')
    observed = accounting.get('observedTokens')
    if type(observed) is not int or observed < 0 or observed != total_tokens:
        raise BenchmarkContractError('runtime billed usage differs from the execution account')
