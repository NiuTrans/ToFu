"""Execution policy on the shared manager-bound resident-process owner.

The manager supplies the prepared execution generation and storage endpoint.
Task claims remain in storage; this private record proves only process lifetime.
"""
import json
from pathlib import Path
import tempfile
import sys

from serverctl_pkg.resident_service import ResidentService
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
