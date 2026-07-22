"""lib/onnx_thread_guard.py — Cap onnxruntime's thread count at process start.

onnxruntime (pulled in transitively by ``pymupdf_layout`` — a hard dependency
of ``pymupdf4llm`` — as well as ``rapidocr-onnxruntime`` / ``cadtrans``) creates
``InferenceSession`` objects with ``intra_op_num_threads == 0`` by default. That
default makes it spawn **one worker thread per HOST cpu** and pin each one to a
physical core via ``pthread_setaffinity_np``.

On a cgroup/cpuset-restricted host (k8s pod, YARN/Hope container, ``taskset``-
launched job, or an exported deployment on a shared cluster) most of those host
cores are NOT in the process's allowed CPU set, so every pin fails with
``EINVAL`` (error code 22) and onnxruntime prints a wall of lines like::

    [E:onnxruntime:...] pthread_setaffinity_np failed for thread: 6922,
      index: 35, mask: {11, }, error code: 22 error msg: Invalid argument.
      Specify the number of threads explicitly so the affinity is not set.

Environment variables (``OMP_NUM_THREADS`` etc.) do NOT fix this: onnxruntime
reads the thread count from the ``SessionOptions`` object, not the environment.
The only reliable fix is to inject an explicit thread count into every
``SessionOptions`` — which means monkeypatching ``InferenceSession.__init__``.

CRITICAL — import ordering: the patch only helps if it is installed **before**
the first ``InferenceSession`` is constructed. That session is created deep in
the boot import chain (``tofu_search.fetch`` → ``pymupdf4llm`` →
``pymupdf_layout`` → onnxruntime), which runs long before any
``lib.pdf_parser`` module is imported. Historically the patch lived in
``lib.pdf_parser._common`` and so never ran on the server-boot path — the
affinity storm hit every deployment on a restricted host. This module lifts the
patch into a standalone, dependency-light callable that ``server.py`` installs
at the very top of boot; ``lib.pdf_parser._common`` now delegates here too so
worker processes and direct PDF-tool callers stay covered.

Idempotent: calling :func:`install_onnx_thread_guard` more than once is a no-op
after the first successful install. Safe to call when onnxruntime is not
installed (returns ``False``).

Environment:
    TOFU_ONNX_THREADS — explicit worker-thread cap. Defaults to
        ``min(8, allowed_cpu_count)``. Falls back to ``TOFU_DOCLING_THREADS``
        for backwards compatibility with the previous _common.py knob.
"""

import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['install_onnx_thread_guard', 'onnx_thread_count', 'allowed_cpu_count']

_installed = False


def allowed_cpu_count() -> int:
    """Return the number of CPUs this process is actually allowed to run on.

    On cgroup/cpuset-restricted hosts ``os.cpu_count()`` reports the HOST core
    count, but the kernel only lets the process schedule on a subset. Reading
    the affinity mask gives the true, permitted count.
    """
    try:
        return max(1, len(os.sched_getaffinity(0)))  # type: ignore[attr-defined]
    except (AttributeError, OSError) as e:
        # sched_getaffinity is Linux-only; on macOS/Windows fall back to the
        # reported cpu count (there is no cpuset restriction to trip over there).
        logger.debug('[onnx-guard] sched_getaffinity unavailable (%s): %s',
                     type(e).__name__, e)
        return max(1, os.cpu_count() or 1)


def onnx_thread_count() -> int:
    """Resolve the explicit onnxruntime thread cap.

    ``TOFU_ONNX_THREADS`` takes precedence, then the legacy
    ``TOFU_DOCLING_THREADS`` knob, else ``min(8, allowed_cpu_count())``.
    """
    for var in ('TOFU_ONNX_THREADS', 'TOFU_DOCLING_THREADS'):
        raw = os.environ.get(var, '').strip()
        if raw:
            try:
                n = int(raw)
                if n > 0:
                    return n
            except ValueError as e:
                logger.debug('[onnx-guard] invalid %s=%r: %s', var, raw, e)
    return min(8, allowed_cpu_count())


def install_onnx_thread_guard() -> bool:
    """Monkeypatch ``onnxruntime.InferenceSession`` to force an explicit thread
    count, suppressing the ``pthread_setaffinity_np`` EINVAL storm on
    cpuset-restricted hosts.

    Must run BEFORE the first ``InferenceSession`` is constructed. Idempotent
    and safe to call when onnxruntime is not installed.

    Returns:
        True if the guard is installed (now or on a prior call), False if
        onnxruntime is not importable.
    """
    global _installed
    if _installed:
        return True

    try:
        import onnxruntime as _ort
    except ImportError:
        # onnxruntime is an optional transitive dep — absent in minimal
        # installs. Nothing to guard.
        return False
    except Exception as e:
        # A broken onnxruntime build should not take down boot.
        logger.warning('[onnx-guard] onnxruntime import failed (%s) — '
                       'skipping thread guard: %s', type(e).__name__, e)
        return False

    thread_count = onnx_thread_count()
    _OrigSession = _ort.InferenceSession

    class _PatchedSession(_OrigSession):  # type: ignore[valid-type,misc]
        def __init__(self, *args, **kwargs):
            so = kwargs.get('sess_options')
            if so is None:
                so = _ort.SessionOptions()
                so.intra_op_num_threads = thread_count
                so.inter_op_num_threads = thread_count
                kwargs['sess_options'] = so
            else:
                # Only override the "spawn one per host cpu" default (0);
                # respect a caller that already chose a cap.
                if so.intra_op_num_threads == 0:
                    so.intra_op_num_threads = thread_count
                if so.inter_op_num_threads == 0:
                    so.inter_op_num_threads = thread_count
            super().__init__(*args, **kwargs)

    _ort.InferenceSession = _PatchedSession
    _installed = True
    logger.info('[onnx-guard] InferenceSession thread cap installed '
                '(threads=%d, allowed_cpus=%d)', thread_count,
                allowed_cpu_count())
    return True
