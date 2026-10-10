#!/usr/bin/env python3
"""Current canonical Python model-account trace gate; no stores/network/deps.

Executes unchanged account/lease bodies with the canonical limits validator and
usage observer. App imports are excluded; bounded fixture traces never wait on
pending reservations. Native tests exercise async waiting and physical HTTP.
"""
import ast
import builtins
import json
from pathlib import Path
import threading
import time
import types
from check_rust_usage_oracle import canonical_observer
ROOT = Path(__file__).resolve().parents[1]


def canonical_account():
    def declaration(path, name):
        nodes = ast.parse((ROOT / path).read_text()).body
        return next(n for n in nodes if
                    (isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == name) or
                    (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name))
    validator = {}
    node = declaration('lib/agent_core/execution_limits.py', 'validate_execution_limits')
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'canonical_limits', 'exec'), validator)
    namespace = types.SimpleNamespace(validate_execution_limits=validator['validate_execution_limits'])
    def imports(name, *args, **kwargs):
        if name == 'lib.agent_core.execution_limits':
            return namespace
        return builtins.__import__(name, *args, **kwargs)
    scope = {'threading': threading, 'time': time, '__builtins__': dict(vars(builtins), __import__=imports),
             'observed_usage_totals': canonical_observer()}
    nodes = [declaration('lib/agent_core/execution_budget.py', name) for name in
             ('tokens', 'ExecutionBudget', 'ModelBudgetExceeded', 'ModelBudgetLease')]
    nodes.insert(0, declaration('lib/agent_core/terminal_delivery.py', 'TERMINAL_STATUSES'))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'canonical_model_account', 'exec'), scope)
    return scope['ExecutionBudget'], scope['ModelBudgetExceeded']


def traces():
    account_class, refused = canonical_account()
    rows = []
    for cap in (None, 0, 10, 50, 100):
        for input_tokens, output in ((0, 0), (1, 1), (10, 30), (20, 100), (50, 1)):
            for dispatched, unsent, usage in ((False, True, None), (True, True, {}),
                                             (True, False, {'prompt_tokens': 10, 'completion_tokens': 5}),
                                             (False, False, {'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 60})):
                task = {'id': 'root', 'usage': {}, 'config': {'executionLimits':
                        {'max_tokens': cap, 'max_tool_calls': None, 'deadline': None}}}
                account = account_class(task)
                row = dict(cap=cap, input=input_tokens, output=output, dispatched=dispatched, unsent=unsent, usage=usage)
                try:
                    lease = account.reserve_model(task, input_tokens=input_tokens, max_output_tokens=output)
                except refused as error:
                    row['refused'] = error.reason
                else:
                    row['admitted_output'] = lease.max_output_tokens
                    row['ordinal'] = lease.ordinal
                    if dispatched:
                        lease.mark_dispatched()
                    lease.settle(usage, unsent=unsent)
                    lease.settle(usage, unsent=unsent)
                total, _, complete = account.consumption_snapshot()
                row.update(tokens=total, complete=complete, pending=len(account._model_leases))
                rows.append(row)
    return rows


def main():
    path = ROOT / 'packages/tofu-runtime/tests/fixtures/python_model_account.json'
    if path.stat().st_size > 1024 * 1024:
        raise SystemExit('native account oracle exceeds 1 MiB bound')
    if json.loads(path.read_text()) != traces():
        raise SystemExit('native model-account oracle differs from current canonical source')
    print('100 current canonical model-account traces match')


if __name__ == '__main__':
    main()
