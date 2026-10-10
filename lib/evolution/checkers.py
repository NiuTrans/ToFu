"""Criteria checkers: turn a goal's completion criteria into a verdict.

Responsibility: evaluate machine-checkable criteria and produce the verdict
an episode settlement requires. Entry point: ``evaluate_goal``. Dependencies:
bounded subprocess for ``test_command``; no model calls (``judge_panel``
criteria stay human until a model-backed checker attaches through the task
engine).

A criterion whose kind has no automatic checker, or whose params are invalid,
yields ``unknown`` — and any unknown criterion makes the whole verdict None,
so the episode waits for a human instead of guessing. ``test_command`` runs
the owner's own declared command with shell-free argv, a hard timeout, and a
captured-output cap; it never retries. Commands are owner-authored criteria
and retain their declared side effects; shell-free execution is not a sandbox.
"""
from __future__ import annotations

import shlex
import math
import time

from lib.identity import PrincipalContext, require_user_id
from lib.scheduler.process_runner import run_bounded_process

MAX_COMMAND_TIMEOUT_S = 300
MAX_OUTPUT_CHARS = 4000

_AUTOMATIC_KINDS = ('test_command',)


def _run_test_command(params: dict, *, user_id, cancel_event=None) -> tuple[str, str]:
    command = str(params.get('command') or '').strip()
    if not command:
        return 'unknown', ''
    try:
        argv = shlex.split(command)
    except ValueError:
        return 'unknown', ''
    if not argv:
        return 'unknown', ''
    try:
        timeout = float(params.get('timeout_s', 120))
        if not math.isfinite(timeout) or timeout <= 0:
            return 'unknown', ''
        timeout = min(timeout, MAX_COMMAND_TIMEOUT_S)
        owner = require_user_id(user_id, context='evolution criterion owner')
    except (TypeError, ValueError, OverflowError):
        return 'unknown', ''
    completed = run_bounded_process(
        argv, max_runtime=timeout, job_id='evolution-criterion',
        job_type='evolution-check', cancel_event=cancel_event,
        principal=PrincipalContext.user(subject_id=f'evolution-check:{owner}',
            owner_user_id=owner, scopes={'evolution:check'}))
    if completed.timed_out or completed.cancelled or completed.error:
        return 'unknown', ''
    outcome = 'pass' if completed.returncode == 0 else 'fail'
    evidence = ((completed.stdout or '') + (completed.stderr or ''))[-MAX_OUTPUT_CHARS:]
    return outcome, evidence


def evaluate_goal(goal: dict, *, runner=None, user_id=None, cancel_event=None) -> dict | None:
    """Evaluate every criterion; return a verdict or None when human input is
    required. ``runner`` is the injectable seam for tests and future kinds."""
    criteria = (goal or {}).get('criteria') or []
    if not criteria:
        return None
    results = []
    deadline = time.monotonic() + MAX_COMMAND_TIMEOUT_S
    for criterion in criteria[:8]:
        kind = criterion.get('kind')
        params = criterion.get('params') or {}
        if kind == 'test_command':
            if cancel_event is not None and cancel_event.is_set():
                return None
            remaining = deadline - time.monotonic()
            if remaining < 1:
                return None
            if runner is not None:
                outcome, evidence = runner(params)
            else:
                try:
                    requested = float(params.get('timeout_s', 120))
                except (TypeError, ValueError, OverflowError):
                    return None
                if not math.isfinite(requested) or requested <= 0:
                    return None
                bounded_params = dict(params, timeout_s=min(requested, remaining))
                outcome, evidence = _run_test_command(
                    bounded_params, user_id=user_id, cancel_event=cancel_event)
            results.append({'criterion_id': criterion.get('criterion_id') or '',
                            'outcome': outcome,
                            'evidence_ref': evidence[:500] if evidence else ''})
        else:
            results.append({'criterion_id': criterion.get('criterion_id') or '',
                            'outcome': 'unknown'})
    if any(r['outcome'] == 'unknown' for r in results):
        return None
    passes = sum(1 for r in results if r['outcome'] == 'pass')
    if passes == len(results):
        outcome = 'success'
    elif passes:
        outcome = 'partial'
    else:
        outcome = 'failure'
    return {'outcome': outcome, 'assessed_by': 'checker',
            'criterion_results': results}
