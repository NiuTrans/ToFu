"""Bounded no-shell CLI transport. No resident threads or startup probes."""
from __future__ import annotations
import os
import signal
import subprocess
import tempfile
import time
from .contracts import EnvironmentError

class CommandRunner:
    def __init__(self, executable="docker", *, timeout=600, output_limit=8388608, cancelled=lambda: False):
        self.executable, self.timeout = executable, max(1, min(float(timeout), 3600))
        self.output_limit = max(65536, min(int(output_limit), 67108864))
        self.cancelled = cancelled
    def run(self, args, *, input_bytes=b"", mutation=False):
        if os.name != "posix":
            raise EnvironmentError("transport_unsupported", "Default CLI transport requires POSIX; inject a platform-specific runner")
        if self.cancelled():
            raise EnvironmentError("cancelled", "Operation cancelled before dispatch")
        # Named 0600 files remain valid on FUSE until their scoped close.
        with tempfile.NamedTemporaryFile(prefix='tofu-provider-output-') as output, tempfile.NamedTemporaryFile(prefix='tofu-provider-input-') as source:
            source.write(input_bytes); source.seek(0)
            try:
                child = subprocess.Popen([self.executable, *args], stdin=source, stdout=output,
                                         stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as exc:
                raise EnvironmentError("provider_unavailable", "Provider executable is unavailable") from exc
            deadline = time.monotonic() + self.timeout
            failure = ""
            try:
                while child.poll() is None:
                    if self.cancelled(): failure = "cancelled"
                    elif time.monotonic() >= deadline: failure = "provider_timeout"
                    elif os.fstat(output.fileno()).st_size > self.output_limit: failure = "provider_output_limit"
                    if failure: break
                    time.sleep(0.05)
                if failure:
                    raise EnvironmentError(failure, "Provider command interrupted", uncertain=mutation)
                if os.fstat(output.fileno()).st_size > self.output_limit:
                    raise EnvironmentError("provider_output_limit", "Provider output exceeds bound", uncertain=mutation)
                output.seek(0)
                raw = output.read(self.output_limit)
                if child.returncode:
                    # Docker clients can fail after the daemon accepted a write.
                    raise EnvironmentError("provider_command_failed", "Provider command failed", uncertain=mutation)
                return raw.decode("utf-8", errors="replace").strip()
            finally:
                # Reclaim inherited pipes and descendants even when the client
                # exited successfully. Provider daemons are external to this group.
                if child.pid:
                    try: os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError: pass
                    try: child.wait(timeout=1)
                    except subprocess.TimeoutExpired: pass
                    try: os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                child.wait(timeout=5)
