"""lib/pdf_parser/_common.py — Shared constants and initialization for PDF parsing."""

import os
import threading

from lib.log import get_logger

logger = get_logger(__name__)


# ── ORT / OpenMP thread caps (must be set BEFORE ``import docling``) ──
# Docling pulls in onnxruntime, which by default spawns one worker per
# host CPU and pins each one to a physical core via
# ``pthread_setaffinity_np``. On cgroup/cpuset-restricted hosts (k8s
# pods, YARN/Hope containers, ``taskset``-launched jobs) most of those
# cores are NOT in the process's allowed set → ``EINVAL`` storm in
# stderr. Specifying the thread count explicitly skips the affinity
# loop. The full per-converter cap also goes through Docling's
# ``AcceleratorOptions(num_threads=...)`` in ``lib/pdf_parser/docling.py``;
# this is the belt-and-braces early hint.
def _allowed_cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))  # type: ignore[attr-defined]
    except (AttributeError, OSError) as _e_audit:
        logger.debug('[_common] _allowed_cpu_count caught %s: %s', type(_e_audit).__name__, _e_audit)
        return max(1, os.cpu_count() or 1)


_thread_hint = os.environ.get('TOFU_DOCLING_THREADS', '').strip()
if not _thread_hint:
    _thread_hint = str(min(8, _allowed_cpu_count()))
_thread_count = int(_thread_hint)
for _var in ('OMP_NUM_THREADS', 'ORT_NUM_THREADS',
             'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS'):
    os.environ.setdefault(_var, _thread_hint)

# ── Monkeypatch onnxruntime.InferenceSession (if installed) ──
# pymupdf.layout creates InferenceSession objects without specifying
# intra_op_num_threads. The default (0) makes onnxruntime spawn one
# thread per *host* CPU and call pthread_setaffinity_np on each.
# In cgroup-restricted containers most of those CPUs are inaccessible
# → a wall of EINVAL errors on stderr. Env vars don't help because
# onnxruntime only reads intra_op_num_threads from the SessionOptions
# object, not from the environment.  Monkeypatching the constructor
# to inject an explicit thread count is the only reliable fix.
try:
    import onnxruntime as _ort

    _OrigSession = _ort.InferenceSession

    class _PatchedSession(_OrigSession):
        def __init__(self, *args, **kwargs):
            if 'sess_options' not in kwargs or kwargs['sess_options'] is None:
                _so = _ort.SessionOptions()
                _so.intra_op_num_threads = _thread_count
                _so.inter_op_num_threads = _thread_count
                kwargs['sess_options'] = _so
            else:
                _so = kwargs['sess_options']
                if _so.intra_op_num_threads == 0:
                    _so.intra_op_num_threads = _thread_count
                if _so.inter_op_num_threads == 0:
                    _so.inter_op_num_threads = _thread_count
            super().__init__(*args, **kwargs)

    _ort.InferenceSession = _PatchedSession
except ImportError:
    pass

__all__ = ['MAX_PDF_BYTES', 'HAS_PYMUPDF4LLM', 'HAS_PYMUPDF', 'HAS_DOCLING', 'PYMUPDF_LOCK']

# PyMuPDF's C library (MuPDF) is NOT thread-safe. The official docs state:
# "PyMuPDF does not support running on multiple threads - doing so may cause
# incorrect behaviour or even crash Python itself."
# All pymupdf operations MUST be serialized behind this lock.
PYMUPDF_LOCK = threading.Lock()

MAX_PDF_BYTES = 200 * 1024 * 1024  # 200 MB safety limit

# ─── PyMuPDF (core PDF engine) ───
try:
    import pymupdf
    HAS_PYMUPDF = True
    # Suppress noisy MuPDF C-library warnings — they are harmless;
    # MuPDF recovers gracefully.
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
except ImportError as e:
    pymupdf = None  # type: ignore[assignment]
    HAS_PYMUPDF = False
    logger.warning('[PDF] pymupdf not installed — PDF parsing disabled: %s', e)

# ─── pymupdf4llm (preferred for table/header-aware extraction) ───
try:
    import pymupdf4llm  # noqa: F401
    HAS_PYMUPDF4LLM = True
except ImportError as e:
    pymupdf4llm = None  # type: ignore[assignment]
    HAS_PYMUPDF4LLM = False
    logger.warning('[PDF] pymupdf4llm not installed — Markdown PDF extraction disabled: %s', e)

# ─── Docling (optional, OPT-IN — used by mode='structured') ───
# Heavy dep (~2 GB with torch). NOT auto-installed; user opts in via
# `pip install docling` or `install.sh --with-docling`. When present,
# `extract_pdf_text(..., mode='structured')` uses Docling's layout-aware
# pipeline (TableFormer + an internal equation model) for noticeably
# better tables/formulas vs. pymupdf4llm. Silent at import-time when
# missing — the structured-mode call site emits a single info-level
# hint on first use so users know how to enable it.
try:
    import docling  # noqa: F401
    HAS_DOCLING = True
except ImportError:
    docling = None  # type: ignore[assignment]
    HAS_DOCLING = False
    # Intentionally silent on import — Docling is opt-in. We don't want
    # to spam the log on every server start when the user never asked
    # for the structured mode in the first place.
