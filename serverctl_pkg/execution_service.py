"""Execution policy on the shared manager-bound resident-process owner.

The manager supplies the prepared generation, storage endpoint and shared
failure budget. This owner reconciles execution lifetime and projects readiness;
task claims remain in storage and the manager retains the one recovery record.
"""
import json
from pathlib import Path
import tempfile
import sys

from serverctl_pkg.resident_service import ResidentService, ServiceOwnerDraining
from serverctl_pkg.execution_plan import SOURCE_CHANGED_BEFORE_BOOT, source_only_change
from serverctl_pkg.restart_budget import NATIVE_SERVICE_PREPARATION_SECONDS, NATIVE_SERVICE_STOP_SECONDS

HOST_MODULE = 'serverctl_pkg.execution_host'
RECORD_FORMAT = 'tofu.execution-service/v1'


class ExecutionService(ResidentService):
    service_name = 'execution'
    state_name = 'execution-service'
    record_format = RECORD_FORMAT
    host_module = HOST_MODULE

    def stop(self, timeout: float = NATIVE_SERVICE_STOP_SECONDS, *,
             retire_failed_close: bool = False) -> str:
        return super().stop(timeout, retire_failed_close=retire_failed_close)

    def enabled(self, environment: dict[str, str]) -> bool:
        """Managed personal composition; explicit topology retains its owner."""
        return (sys.platform.startswith('linux')
                and (self.project / 'contracts/worker_job_lifecycle_v1.json').is_file()
                and (environment.get('TOFU_DEPLOYMENT_MODE') or 'personal').strip().lower() == 'personal'
                and (environment.get('TOFU_PROCESS_ROLE') or 'all').strip().lower() == 'all'
                and environment.get('TOFUDB_TRANSPORT', '').strip().lower() != 'embedded')

    def prepare(self, environment: dict[str, str]) -> dict:
        from serverctl_pkg.native_engine import _run
        with tempfile.TemporaryDirectory(prefix='tofu-execution-plan-') as directory:
            result = Path(directory) / 'plan.json'
            _run([self.python, '-m', HOST_MODULE, '--plan', '--plan-output', str(result)],
                 cwd=self.project, environment=self.process_environment(environment),
                 timeout=NATIVE_SERVICE_PREPARATION_SECONDS,
                 failure_subject='Execution service preparation')
            return json.loads(result.read_text(encoding='utf-8'))

    def process_environment(self, environment: dict[str, str]) -> dict[str, str]:
        env = dict(environment)
        env['TOFU_PROCESS_ROLE'] = 'execution'
        return env

    def readiness_error(self, record: dict, recovery: dict) -> str:
        """A live API is ready only while its recorded executor is ready."""
        if not record or (record.get('state') == 'ready' and self.host_matches(record)):
            return ''
        return str(recovery.get('error') or record.get('error')
                   or 'execution service is not ready')

    def recover(self, *, state: dict, max_failures: int, clock, account_failure,
                retry_delay, observe, environment_factory, validate_source_refresh) -> bool:
        """Recover owned execution without retiring the live API or replaying work."""
        record = self.record()
        if not record:
            return True  # Explicit/legacy topology has its own execution owner.
        if self.host_matches(record):
            if record.get('state') == 'ready':
                state['executionRecovery'] = {}
                return True
            observe('degraded', str(record.get('error') or 'execution service is starting'))
            return False
        recovery = state.get('executionRecovery') or {}
        if recovery.get('serviceId') != record.get('serviceId'):
            now, recent, consecutive = account_failure()
            exhausted = recent >= max_failures
            recovery = {
                'serviceId': record.get('serviceId'),
                'retryAt': now + retry_delay(consecutive, recent),
                'requiresRestart': exhausted,
                'error': ('execution recovery paused; explicit restart required' if exhausted
                          else 'execution service exited; waiting for recovery'),
            }
            state['executionRecovery'] = recovery
            state['lastFailureReason'] = recovery['error']
            state['lastExitCause'] = 'execution_exit'
        if recovery.get('requiresRestart') or clock() < recovery['retryAt']:
            observe('crashloop' if recovery.get('requiresRestart') else 'degraded',
                               recovery['error'])
            return False
        try:
            environment = environment_factory()
            plan = self.prepare(environment)
            original = record.get('plan') or {}
            unentered_source_change = (
                record.get('state') == 'failed'
                and record.get('failureKind') == SOURCE_CHANGED_BEFORE_BOOT
                and source_only_change(original, plan))
            if original != plan and not unentered_source_change:
                raise RuntimeError('execution configuration or code changed; explicit restart required')
            if unentered_source_change:
                validate_source_refresh()
            self.stop()
            self.ensure(plan, environment)
        except ServiceOwnerDraining as exc:
            # A killed host's cmdline vanishes before its descriptors close, so
            # its lease can outlive the departure briefly. That is the same
            # failure still exiting, retried within the host stop budget.
            draining_since = recovery.setdefault('drainingSince', clock())
            if clock() - draining_since < NATIVE_SERVICE_STOP_SECONDS:
                observe('degraded', f'execution service is still exiting: {exc}')
                return False
            recovery.update(requiresRestart=True, error=f'execution recovery failed: {exc}')
            observe('degraded', recovery['error'])
            return False
        except Exception as exc:
            recovery.update(requiresRestart=True, error=f'execution recovery failed: {exc}')
            observe('degraded', recovery['error'])
            return False
        observe('degraded', 'execution service is starting')
        return False
