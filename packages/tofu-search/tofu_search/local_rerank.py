"""Optional local relevance scores from a request-owned, disposable CPU worker.

LocalReranker holds configuration and any explicit reservations, never a loaded
model; host CPU and memory never gate, refuse or stop a call. Each score call
owns its child, bounded output, cancellation and joined process-group
retirement. The shared resource probe and group owner are
generated from their application owners; no host import or network is needed.
"""

from __future__ import annotations

import json
import math
import os
import selectors
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import CancelledError
from pathlib import Path

from tofu_search.generated_local_models import local_model_artifacts
from tofu_search.generated_process_groups import retire_process_group
from tofu_search.generated_resources import probe_resources


class LocalRerankError(RuntimeError):
    """An optional scoring attempt failed without producing usable scores."""

    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind


class LocalRerankCleanupError(RuntimeError):
    """Cleanup remains owned; close() must retry before capacity is released."""


def _positive_integer(value, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _resident_bytes(pid: int) -> int | None:
    """Observe this owned child's Linux resident pages, without a host scan."""
    try:
        return int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


class LocalReranker:
    """Score with an already downloaded, pinned Qwen snapshot; never download.

    Reuse one instance across related concurrent searches so explicit
    max_parallel and max_rss_mb reservations are shared. Only default threads
    follow one CPU observation; without explicit limits every call starts its
    own worker. The current qualified execution path is Linux CPU BF16. No
    worker survives score().
    """

    def __init__(
        self,
        model_dir: str | Path,
        *,
        artifact_id: str = "qwen3-reranker-0.6b-bf16",
        python_executable: str | None = None,
        threads: int | None = None,
        max_parallel: int | None = None,
        max_rss_mb: int | None = None,
        max_tokens: int = 1024,
    ):
        artifacts = {row["artifact_id"]: row for row in local_model_artifacts()["artifacts"]}
        if artifact_id not in artifacts:
            raise ValueError("Unknown admitted local model artifact")
        self._artifact = artifacts[artifact_id]
        self.model_dir = Path(model_dir).expanduser().resolve()
        self.python_executable = python_executable or sys.executable
        self.max_tokens = _positive_integer(max_tokens, "max_tokens")
        if self.max_tokens > self._artifact["model"]["context_window"]:
            raise ValueError("max_tokens exceeds the admitted model context")
        self.capacity = probe_resources(self.model_dir)
        # The qualified CPU study favored a quarter of visible CPUs per call.
        # Threads size one call; they never limit how many calls run.
        self.threads = (
            _positive_integer(threads, "threads")
            if threads is not None
            else max(1, self.capacity.effective_cpu_count // 4)
        )
        weight_bytes = sum(
            row["bytes"]
            for row in self._artifact["source"]["files"]
            if row["name"].endswith(".safetensors")
        )
        self._minimum_memory = weight_bytes * 3
        self.max_parallel = (
            _positive_integer(max_parallel, "max_parallel") if max_parallel is not None else None
        )
        self.max_rss_bytes = (
            _positive_integer(max_rss_mb, "max_rss_mb") * 1024 * 1024
            if max_rss_mb is not None
            else None
        )
        self._slots = (
            threading.BoundedSemaphore(self.max_parallel) if self.max_parallel is not None else None
        )
        self._closed = threading.Event()
        self._state = threading.Condition()
        self._cleanup_lock = threading.Lock()
        self._active_calls = 0
        self._join_debt: list[subprocess.Popen] = []

    def _command(self) -> list[str]:
        return [
            self.python_executable,
            "-u",
            str(Path(__file__).with_name("_local_rerank_worker.py")),
            str(os.getpid()),
        ]

    def score(
        self,
        query: str,
        documents: list[str],
        *,
        timeout: float = 30.0,
        cancel_event=None,
        diag: dict | None = None,
        group_sizes: list[int] | None = None,
    ) -> list[float]:
        """Return one finite score per document, or raise after retiring the child.

        timeout includes reservation, verification, imports, loading and scoring.
        Retirement has its own finite grace/escalation interval. Cancellation
        raises CancelledError; missing dependencies/weights and resource failures
        raise LocalRerankError. Callers own any lexical fallback decision.
        """
        with self._state:
            if self._closed.is_set():
                raise CancelledError("Local reranker is closed")
            if self._join_debt:
                raise LocalRerankCleanupError("Call close() to settle outstanding worker cleanup")
            self._active_calls += 1
        try:
            return self._score(
                query,
                documents,
                timeout=timeout,
                cancel_event=cancel_event,
                diag=diag,
                group_sizes=group_sizes,
            )
        finally:
            with self._state:
                self._active_calls -= 1
                self._state.notify_all()

    def close(self, *, timeout: float = 5.0) -> None:
        """Fence future calls, cancel/join accepted calls and retry cleanup debt.

        An observation timeout or retirement failure raises without claiming
        completion. The closed instance retains every unsettled process and
        reservation so a later close() can retry; no background reaper is used.
        """
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("close timeout must be finite and positive")
        deadline = time.monotonic() + timeout
        self._closed.set()
        with self._state:
            while self._active_calls:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Local ranking calls have not yet joined")
                self._state.wait(remaining)
        if not self._cleanup_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError("Local ranking cleanup is still in progress")
        try:
            for process in list(self._join_debt):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Local ranking cleanup has not yet joined")
                try:
                    retire_process_group(
                        process.pid,
                        wait_leader=process.wait,
                        grace_seconds=min(0.2, remaining / 2),
                        kill_seconds=min(2.0, remaining / 2),
                    )
                except Exception as error:
                    raise LocalRerankCleanupError(
                        "Local worker cleanup remains unsettled"
                    ) from error
                with self._state:
                    self._join_debt.remove(process)
                if self._slots is not None:
                    self._slots.release()
        finally:
            self._cleanup_lock.release()

    def __enter__(self):
        if self._closed.is_set():
            raise CancelledError("Local reranker is closed")
        return self

    def __exit__(self, *_exception):
        self.close()

    def _score(
        self,
        query: str,
        documents: list[str],
        *,
        timeout: float,
        cancel_event,
        diag: dict | None,
        group_sizes: list[int] | None,
    ) -> list[float]:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be finite and positive")
        if (
            not isinstance(query, str)
            or not isinstance(documents, list)
            or any(not isinstance(d, str) for d in documents)
        ):
            raise TypeError("query and documents must be text")
        if group_sizes is None:
            group_sizes = [len(documents)]
        if (
            not isinstance(group_sizes, list)
            or any(type(n) is not int or n < 0 for n in group_sizes)
            or sum(group_sizes) != len(documents)
        ):
            raise ValueError("group_sizes must partition the candidate documents")
        if not documents:
            return []
        if not sys.platform.startswith("linux"):
            raise LocalRerankError(
                "unsupported_platform", "Local CPU reranking currently requires Linux"
            )
        if self.max_rss_bytes is not None and self.max_rss_bytes < self._minimum_memory:
            raise LocalRerankError(
                "insufficient_memory",
                "Explicit max_rss_mb is below the model working-set allowance",
            )
        started = time.monotonic()
        deadline = started + float(timeout)

        def remaining():
            if self._closed.is_set() or (cancel_event is not None and cancel_event.is_set()):
                raise CancelledError("Local reranking cancelled")
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("Local reranking deadline exceeded")
            return left

        remaining()
        payload = json.dumps(
            {
                "artifact": self._artifact,
                "model_dir": str(self.model_dir),
                "query": query,
                "documents": documents,
                "threads": self.threads,
                "max_tokens": self.max_tokens,
                "group_sizes": group_sizes,
            },
            ensure_ascii=False,
        ).encode()
        if self.max_rss_bytes is not None and len(payload) > self.max_rss_bytes // 16:
            raise LocalRerankError(
                "input_limit", "Ranking input exceeds its explicit memory allowance"
            )
        if self._slots is not None:
            while not self._slots.acquire(timeout=min(0.05, remaining())):
                pass
        process = None
        output = bytearray()
        output_limit = 16_384 + len(documents) * 32
        peak_bytes = 0
        try:
            remaining()
            environment = dict(os.environ)
            environment.update(
                HF_HUB_OFFLINE="1",
                TRANSFORMERS_OFFLINE="1",
                HF_HUB_DISABLE_TELEMETRY="1",
                TOKENIZERS_PARALLELISM="false",
                OMP_NUM_THREADS=str(self.threads),
                MKL_NUM_THREADS=str(self.threads),
            )
            # A private anonymous input file avoids a blocking stdin writer or
            # a second communication thread while large candidate pools load.
            with tempfile.TemporaryFile() as input_file, selectors.DefaultSelector() as selector:
                input_file.write(payload)
                input_file.seek(0)
                remaining()
                try:
                    process = subprocess.Popen(
                        self._command(),
                        stdin=input_file,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        env=environment,
                        start_new_session=True,
                    )
                except OSError as error:
                    raise LocalRerankError(
                        "worker_start_failed",
                        "Cannot start the configured local ranking interpreter",
                    ) from error
                assert process.stdout is not None
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ)
                eof = False
                while not (eof and process.poll() is not None):
                    wait = min(0.05, remaining())
                    rss = _resident_bytes(process.pid)
                    peak_bytes = max(peak_bytes, rss or 0)
                    if (
                        rss is not None
                        and self.max_rss_bytes is not None
                        and rss > self.max_rss_bytes
                    ):
                        raise LocalRerankError(
                            "memory_limit", "Local ranking exceeded its resident-memory allowance"
                        )
                    for key, _events in selector.select(wait):
                        chunk = os.read(
                            key.fileobj.fileno(), min(65536, output_limit + 1 - len(output))
                        )
                        if not chunk:
                            selector.unregister(key.fileobj)
                            eof = True
                        else:
                            output.extend(chunk)
                            if len(output) > output_limit:
                                raise LocalRerankError(
                                    "output_limit", "Local ranking exceeded its response allowance"
                                )
                remaining()  # Stop/deadline wins over a concurrent completion.
                try:
                    response = json.loads(output)
                except (ValueError, UnicodeError) as error:
                    raise LocalRerankError(
                        "invalid_response", "Local ranking returned no valid response"
                    ) from error
                if (
                    not isinstance(response, dict)
                    or response.get("ok") is not True
                    or process.returncode != 0
                ):
                    kind = (
                        response.get("kind", "worker_failed")
                        if isinstance(response, dict)
                        else "invalid_response"
                    )
                    raise LocalRerankError(
                        str(kind)[:80],
                        "Local ranking worker failed; verify the pinned snapshot and optional dependencies",
                    )
                scores = response.get("scores")
                if (
                    not isinstance(scores, list)
                    or len(scores) != len(documents)
                    or any(
                        type(s) not in (int, float) or not math.isfinite(s) or not 0 <= s <= 1
                        for s in scores
                    )
                ):
                    raise LocalRerankError(
                        "invalid_scores", "Local ranking returned invalid scores"
                    )
                if diag is not None:
                    diag.update(
                        artifact_id=self._artifact["artifact_id"],
                        revision=self._artifact["source"]["revision"],
                        threads=self.threads,
                        observed_peak_rss_bytes=peak_bytes,
                        worker=response.get("metrics", {}),
                    )
                accepted_scores = [float(score) for score in scores]
        except TimeoutError:
            raise
        except OSError as error:
            raise LocalRerankError(
                "worker_io_failed", "Local ranking input/output is unavailable"
            ) from error
        finally:
            retired = process is None
            try:
                if process is not None:
                    # Even a successful leader can leave inherited writers.
                    # The common owner proves group quiescence and reaps it.
                    retire_process_group(
                        process.pid, wait_leader=process.wait, grace_seconds=0.2, kill_seconds=2.0
                    )
                    retired = True
            except Exception as error:
                raise LocalRerankCleanupError(
                    "Local worker cleanup remains unsettled; call close() to retry"
                ) from error
            finally:
                if process is not None and process.stdout is not None:
                    process.stdout.close()
                if retired:
                    if self._slots is not None:
                        self._slots.release()
                else:
                    with self._state:
                        self._join_debt.append(process)
                if diag is not None:
                    diag["elapsed_seconds"] = time.monotonic() - started
                    diag["retired"] = retired
        remaining()
        return accepted_scores
