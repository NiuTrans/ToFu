"""Fresh-code control boundary for approved application restarts.

Entry points: run_application_restart_control (serving worker), main (child).
Dependencies: serverctl preparation and the shared Supervisor protocol. The
child never opens application storage or decides approval/task policy. The
existing bounded command runner owns output, deadlines and process teardown.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
from pathlib import Path
import sys

from .native_engine import _run

RESULT_PREFIX = 'TOFU_RESTART_RESULT '
# Native lock/build budget (1200s), frontend lock/build (660s), plus overhead.
PREPARATION_TIMEOUT_SECONDS = 2010
DELEGATION_TIMEOUT_SECONDS = 45
_MANAGED_ENGINE_ENVIRONMENT = ('TOFUDB_EXPECTED_SHA256', 'TOFUDB_BIN')


def run_application_restart_control(
    project_path: str, *, stage: str, reason: str = 'update',
    requested_at: float | None = None,
) -> dict:
    """Use the target checkout's code, relay progress and require a receipt.

    Preparation completes before approval consumption/shutdown. Delegation
    completes only after the manager acknowledges its deferred replacement;
    the initiating response can flush before the worker is stopped.
    """
    if stage not in ('prepare', 'delegate'):
        raise ValueError(f'Unknown application restart stage: {stage}')
    project = Path(project_path).resolve()
    command = [sys.executable, '-m', 'serverctl_pkg.application_restart',
               '--stage', stage, '--reason', reason]
    if requested_at is not None:
        command.extend(['--requested-at', str(requested_at)])
    output = _run(
        command, cwd=project, environment=dict(os.environ),
        timeout=(PREPARATION_TIMEOUT_SECONDS if stage == 'prepare'
                 else DELEGATION_TIMEOUT_SECONDS),
        progress='Preparing application restart' if stage == 'prepare' else None,
        termination_grace=40, failure_subject=f'Application restart {stage}',
    )
    # The runner retains the tail, so the final bounded receipt survives logs.
    lines = output.splitlines()
    if not lines or not lines[-1].startswith(RESULT_PREFIX):
        raise RuntimeError(f'Application restart {stage} returned no receipt.')
    try:
        result = json.loads(lines[-1][len(RESULT_PREFIX):])
    except ValueError as exc:
        raise RuntimeError('Application restart returned an invalid receipt.') from exc
    if not isinstance(result, dict) or result.get('ok') is not True:
        raise RuntimeError(f'Application restart {stage} was not acknowledged.')
    # Carry only managed engine changes; never overwrite a concurrently changed
    # explicit override or copy arbitrary child environment/credentials back.
    changes = result.get('environmentChanges', {})
    if not isinstance(changes, dict):
        raise RuntimeError('Application restart returned invalid environment changes.')
    for key, change in changes.items():
        if (key in _MANAGED_ENGINE_ENVIRONMENT and isinstance(change, dict)
                and isinstance(change.get('after'), str)
                and os.environ.get(key) == change.get('before')):
            os.environ[key] = change['after']
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True, choices=('prepare', 'delegate'))
    parser.add_argument('--reason', default='update')
    parser.add_argument('--requested-at', type=float)
    args = parser.parse_args(argv)
    project = str(Path.cwd().resolve())
    try:
        if args.stage == 'prepare':
            from serverctl import (
                prepare_native_engine_artifact, prepare_source_frontend_artifact,
            )
            from .native_engine import _report_stage
            before = {key: os.environ.get(key) for key in _MANAGED_ENGINE_ENVIRONMENT}
            error = prepare_native_engine_artifact('in-app restart', project=project)
            if error:
                raise RuntimeError(error)
            from lib.process_roles import CAPABILITY_FRONTEND, process_role_has
            owns_frontend = process_role_has(
                os.environ.get('TOFU_PROCESS_ROLE') or 'all', CAPABILITY_FRONTEND)
            if owns_frontend:
                _report_stage('frontend', 'Preparing frontend resources.')
                error = prepare_source_frontend_artifact(
                    'in-app restart', capture_output=True)
                if error:
                    raise RuntimeError(error)
            result = {'ok': True, 'environmentChanges': {
                key: {'before': before[key], 'after': os.environ[key]}
                for key in _MANAGED_ENGINE_ENVIRONMENT
                if os.environ.get(key) is not None and os.environ.get(key) != before[key]
            }}
        else:
            from supervisor_protocol import request_deferred_worker_restart
            result = request_deferred_worker_restart(
                project, source=f'application-{args.reason}',
                environment=os.environ, requested_at=args.requested_at)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    print(RESULT_PREFIX + json.dumps(result), flush=True)
    return 0


if __name__ == '__main__':
    if os.name != 'nt':
        def interrupted(_signal, _frame):
            raise RuntimeError('Application restart control was cancelled.')
        signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
