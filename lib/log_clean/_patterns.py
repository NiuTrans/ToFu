"""lib/log_clean/_patterns.py — Compiled regex tables & constants.

All module-level regex patterns and threshold constants used by the
log-noise detection passes live here. Grouped by the pass that consumes
them (see the pass ordering in ``_detect.detect_log_noise``).

These are pure data (compiled patterns) — no I/O, no state. They travel
with the collapse/helper functions that reference them.
"""

from __future__ import annotations

import re

from lib.log import get_logger

logger = get_logger(__name__)


# ── Pass 1: per-line log prefixes ──────────────────────────────────
# Each entry is ``(compiled_regex, label)``. Order matters — the
# Worker-with-full-format pattern must come before the bare
# ``(WorkerName pid=NNN)`` catch-all.

_LOG_PREFIX_PATTERNS = [
    # Ray/vLLM worker: (Worker_XXX pid=NNN) LEVEL MM-DD HH:MM:SS [path:line]
    (re.compile(
        r'^\([\w_]+ pid=\d+\)\s+(?:ERROR|WARNING|INFO|DEBUG)\s+'
        r'\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+\[[^\]]+\]\s*'),
     'Worker日志前缀'),
    # Bare Ray worker tag — strip ONLY the tag (no trailing \s*) so
    # traceback indentation is preserved.
    (re.compile(r'^\([\w_]+ pid=\d+\) ?'), 'Worker前缀'),
    # Standard Python logging: LEVEL YYYY-MM-DD HH:MM:SS,NNN module
    (re.compile(
        r'^(?:ERROR|WARNING|INFO|DEBUG)\s+\d{4}-\d{2}-\d{2}\s+'
        r'\d{2}:\d{2}:\d{2}[.,]\d+\s+[\w.]+\s*'),
     'Python日志前缀'),
    # Bracketed timestamp: [YYYY-MM-DD HH:MM:SS] LEVEL
    (re.compile(
        r'^\[\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]?\d*\]\s*'
        r'(?:ERROR|WARNING|INFO|DEBUG|CRITICAL)?\s*'),
     '时间戳前缀'),
    # Dash-separated: YYYY-MM-DD HH:MM:SS,NNN - name - LEVEL -
    (re.compile(
        r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[.,]\d+\s+-\s+'
        r'[\w.]+\s+-\s+\w+\s+-\s*'),
     '日志前缀'),
    # Go-style: I0302 01:26:07.123456 file.go:123]
    (re.compile(r'^[IWEF]\d{4}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\S+\]\s*'),
     'Go日志前缀'),
    # Task ID prefix
    (re.compile(r'^\[Task\s+[0-9a-f]+\]\s*'), 'Task ID前缀'),
    # Flask/Werkzeug access log prefix
    (re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}\s+-\s+\S+\s+\[.*?\]\s*'),
     'HTTP日志前缀'),
    # Docker/K8s ISO timestamp prefix
    (re.compile(
        r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\s+'),
     'ISO时间戳前缀'),
    # systemd/journald
    (re.compile(
        r'^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+'
        r'\S+(?:\[\d+\])?:\s*'),
     'syslog前缀'),
]

# Whole-line noise patterns (removed in Pass 0.5).
_NOISE_LINE_PATTERNS = [
    (re.compile(
        r'^\d{1,3}(?:\.\d{1,3}){3}\s+-\s+\S+\s+\[.*?\]\s+'
        r'"[A-Z]+\s+\S+\s+HTTP/[\d.]+"\s+[23]\d{2}\s+[\d-]+\s*$'),
     'HTTP成功请求'),
]

_POINTER_LINE_RE = re.compile(r'^\s*[\^~]+\s*$')

# ── Pass 3: shorten long absolute paths ──
_LONG_PATH_RE = re.compile(
    r'(?:/[\w._-]+){4,}/([\w._-]+/[\w._-]+(?:\.[\w]+)?)')

# ── Pass 3.3: tqdm progress bars ──
_TQDM_BAR_RE = re.compile(
    r'(\d+)%\|[^|]*\|\s*[\d.]+[kKMGT]?\s*/\s*[\d.]+[kKMGT]?')
_TQDM_RATE_TAIL_RE = re.compile(r'/(?:s|it)\]\s*$')

# ── Pass 3.5: similarity fingerprint ──
_HEX_ADDR_RE = re.compile(r'0x[0-9a-fA-F]+')
_UUID_RE = re.compile(
    r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.IGNORECASE)
_IP_RE = re.compile(r'\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?')
_DEVICE_ID_RE = re.compile(
    r'\b(?:cuda|gpu|device|worker|rank)\s*[:_]?\s*\d+', re.IGNORECASE)
_LONG_DIGIT_RE = re.compile(r'\b\d{6,}\b')

_DEVICE_ID_PATTERNS = [
    re.compile(r'\bcuda:(\d+)'),
    re.compile(r'\bWorker\s*(\d+)', re.IGNORECASE),
    re.compile(r'\bGPU\s*[:_]?\s*(\d+)', re.IGNORECASE),
    re.compile(r'\brank\s*[:_]?\s*(\d+)', re.IGNORECASE),
    re.compile(r'\bdevice\s*[:_]?\s*(\d+)', re.IGNORECASE),
]

_WORKER_TAG_RE = re.compile(r'^\([\w_]+ pid=\d+\)')
