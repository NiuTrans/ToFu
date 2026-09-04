"""Standalone control plane for the canonical model-routing v2 aggregate.

The module intentionally has no provider-shaped draft or discovery state. A
save validates the complete access aggregate and its independent Credential
secret map, atomically persists it, and hot-applies it to future Agent runs.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Mapping

from tofu_agent.models import AgentConfigurationError, ModelRoutingConfig
from tofu_agent.provider_store import ModelRoutingSettingsStore


class ModelRoutingConfigurationLocked(AgentConfigurationError):
    """The current aggregate belongs to environment/CLI authority."""


class ModelRoutingSetupService:
    """Manage one complete v2 access envelope for the standalone runtime."""

    def __init__(
        self,
        runtime,
        store: ModelRoutingSettingsStore,
        *,
        source: str = 'none',
        editable: bool = True,
        load_error: str = '',
    ) -> None:
        self.runtime = runtime
        self.store = store
        self.source = str(source or 'none')
        self.editable = bool(editable)
        self.load_error = str(load_error or '')
        self._mutation_lock = threading.RLock()

    @property
    def model_routing(self) -> ModelRoutingConfig | None:
        return getattr(self.runtime, 'model_routing', None)

    def snapshot(self) -> dict[str, Any]:
        access = self.model_routing
        return {
            'configured': access is not None,
            'ready': bool(access and getattr(self.runtime, 'default_model', None)),
            'editable': self.editable,
            'source': self.source,
            'load_error': self.load_error,
            'model_routing': access.public_dict() if access is not None else None,
            'storage': {
                'kind': 'encrypted-file',
                'secret_values_returned': False,
                'contract_version': 'tofu.model-routing/v2',
            },
        }

    def _require_editable(self) -> None:
        if not self.editable:
            raise ModelRoutingConfigurationLocked(
                'the active model-routing aggregate is owned by the '
                'environment or command line; remove that override and restart')

    @staticmethod
    def _config(payload: Mapping[str, Any]) -> ModelRoutingConfig:
        if not isinstance(payload, Mapping):
            raise AgentConfigurationError('model-routing payload must be an object')
        return ModelRoutingConfig.from_mapping(payload)

    def test_connection(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_s: float = 20.0,
    ) -> dict[str, Any]:
        """Probe the computed primary Deployment without retaining the config."""
        access = self._config(payload)
        from lib.model_routing import (
            InMemoryModelRoutingRepository, OwnerBoundary,
            dispose_routed_slot_group, mint_routed_slot_group,
            parse_native_model_selection,
        )
        repository = InMemoryModelRoutingRepository()
        boundary = OwnerBoundary.create(
            self.runtime.principal.require_owner(
                context='ModelRoutingSetupService.test_connection'))
        repository.compare_and_swap(
            boundary, access.document, expected_revision=0)
        for reference, secret in access.credential_secrets.items():
            repository.put_secret(
                boundary, secret, secret_reference=reference)
        selection = parse_native_model_selection({
            'model': access.model,
            'routing': access.routing,
        })
        group = mint_routed_slot_group(
            repository, boundary, selection, owner_tag='tofu-agent:probe')
        try:
            slot = group.primary.slot
            if slot.oauth:
                raise AgentConfigurationError(
                    'OAuth Deployment probes require the full app token lifecycle')
            from lib.byo_egress import validate_egress_url
            validate_egress_url(slot.base_url)
            from lib.provider_probe import probe_one_cell
            started = time.monotonic()
            verdict, _detail = probe_one_cell(
                slot.base_url,
                slot.api_key,
                slot.model,
                dict(slot.extra_headers),
                timeout_s,
                protocol=slot.protocol,
            )
            return {
                'ok': verdict == 'ok',
                'verdict': verdict,
                'provider_id': slot.routing_provider_id,
                'deployment_id': slot.route_deployment_id,
                'latency_ms': max(
                    0, round((time.monotonic() - started) * 1000)),
            }
        finally:
            dispose_routed_slot_group(group)

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._mutation_lock:
            self._require_editable()
            access = self._config(payload)
            self.store.save(access)
            self.runtime.configure_model_routing(access, source='saved')
            self.source = 'saved'
            self.load_error = ''
            return self.snapshot()

    def delete(self) -> dict[str, Any]:
        with self._mutation_lock:
            self._require_editable()
            self.store.delete()
            self.runtime.configure_model_routing(None, source='none')
            self.source = 'none'
            self.load_error = ''
            return self.snapshot()


__all__ = [
    'ModelRoutingConfigurationLocked',
    'ModelRoutingSetupService',
]
