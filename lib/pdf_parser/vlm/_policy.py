"""Canonical resource and API budgets for VLM PDF transcription.

The launch-time resource manifest owns zero-configuration defaults.  This
module exposes worker idle hints and explicit time/attempt policies
to the task registry and synchronous parser.
"""

from __future__ import annotations

import os
import sys

from runtime_guards import resolve_resource_budget


def vlm_task_workers() -> int:
    return resolve_resource_budget(
        'TOFU_PDF_VLM_TASK_WORKERS', minimum=1, maximum=sys.maxsize)


def vlm_worker_idle_seconds() -> float:
    if os.environ.get('TOFU_PDF_VLM_WORKER_IDLE_SECONDS', '').strip() == '0':
        return 0.0
    return float(resolve_resource_budget(
        'TOFU_PDF_VLM_WORKER_IDLE_SECONDS', maximum=86_400))


def vlm_call_workers() -> int:
    return resolve_resource_budget(
        'TOFU_PDF_VLM_CALL_WORKERS', minimum=1, maximum=sys.maxsize)


def vlm_task_timeout_seconds() -> int:
    return resolve_resource_budget(
        'TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS', minimum=60, maximum=86_400)


def vlm_max_429_attempts() -> int:
    return resolve_resource_budget(
        'TOFU_PDF_VLM_MAX_429_ATTEMPTS', minimum=1, maximum=64)


__all__ = [
    'vlm_call_workers',
    'vlm_max_429_attempts',
    'vlm_task_timeout_seconds',
    'vlm_task_workers',
    'vlm_worker_idle_seconds',
]
