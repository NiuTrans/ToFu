"""Canonical resource and API budgets for VLM PDF transcription.

The launch-time resource manifest owns zero-configuration defaults.  This
module only applies domain hard ceilings and exposes self-describing accessors
to the task registry and synchronous parser.
"""

from __future__ import annotations

import os

from runtime_guards import resolve_resource_budget


def vlm_task_workers() -> int:
    return resolve_resource_budget(
        'TOFU_PDF_VLM_TASK_WORKERS', minimum=1, maximum=16)


def vlm_queue_capacity() -> int:
    return resolve_resource_budget(
        'TOFU_PDF_VLM_QUEUE_CAPACITY', minimum=1, maximum=256)


def vlm_worker_idle_seconds() -> float:
    if os.environ.get('TOFU_PDF_VLM_WORKER_IDLE_SECONDS', '').strip() == '0':
        return 0.0
    return float(resolve_resource_budget(
        'TOFU_PDF_VLM_WORKER_IDLE_SECONDS', maximum=86_400))


def vlm_call_workers() -> int:
    return resolve_resource_budget(
        'TOFU_PDF_VLM_CALL_WORKERS', minimum=1, maximum=16)


def vlm_max_pages() -> int:
    return resolve_resource_budget(
        'TOFU_PDF_VLM_MAX_PAGES', minimum=1, maximum=2048)


def vlm_task_timeout_seconds() -> int:
    return resolve_resource_budget(
        'TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS', minimum=60, maximum=86_400)


def vlm_max_429_attempts() -> int:
    return resolve_resource_budget(
        'TOFU_PDF_VLM_MAX_429_ATTEMPTS', minimum=1, maximum=64)


__all__ = [
    'vlm_call_workers',
    'vlm_max_429_attempts',
    'vlm_max_pages',
    'vlm_queue_capacity',
    'vlm_task_timeout_seconds',
    'vlm_task_workers',
    'vlm_worker_idle_seconds',
]
