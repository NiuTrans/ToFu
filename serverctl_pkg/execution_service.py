"""Execution policy on the shared manager-bound resident-process owner.

The manager supplies the prepared execution generation and storage endpoint.
Task claims remain in storage; this private record proves only process lifetime.
"""
import json
from pathlib import Path
import tempfile

from serverctl_pkg.resident_service import ResidentService
from serverctl_pkg.restart_budget import NATIVE_SERVICE_PREPARATION_SECONDS

HOST_MODULE = 'serverctl_pkg.execution_host'
RECORD_FORMAT = 'tofu.execution-service/v1'


class ExecutionService(ResidentService):
    service_name = 'execution'
    state_name = 'execution-service'
    record_format = RECORD_FORMAT
    host_module = HOST_MODULE

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
