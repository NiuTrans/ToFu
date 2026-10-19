"""Launch-derived webhook file/worker reuse hints.

Hints size reuse, never subscription, delivery, retry or event admission.
Remote transient failures retain deliveries for backoff until success, permanent
refusal, explicit revocation or shutdown. No second host probe is performed.
"""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
import os
from runtime_guards import resolve_resource_budget


@dataclass(frozen=True, slots=True)
class WebhookBudget:
    spool_segment_bytes: int
    worker_reuse_target: int
    subscription_cache_seconds: float


def resolve_webhook_budget(environment: Mapping[str, str] | None = None) -> WebhookBudget:
    environment = os.environ if environment is None else environment
    registry = resolve_resource_budget('TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY',
                                      environment, minimum=16, maximum=4096)
    workers = resolve_resource_budget('TOFU_AGENT_WORKERS', environment,
                                     minimum=1, maximum=65536)
    return WebhookBudget(spool_segment_bytes=registry * 64 * 1024,
                         worker_reuse_target=workers,
                         subscription_cache_seconds=1.0)
