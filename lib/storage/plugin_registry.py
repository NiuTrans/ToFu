"""Declarative plugin-storage discovery (the replacement for callbacks)."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.metadata import entry_points
import uuid

from lib.storage.client import StorageClient
from lib.storage.manifest import ManifestError, validate_manifest


ENTRY_POINT_GROUP = 'tofu.storage'


def discover_plugin_manifests(*, entries=None) -> list[dict]:
    """Load immutable manifest objects; executable registrars are rejected."""
    if entries is None:
        entries = entry_points(group=ENTRY_POINT_GROUP)
    manifests = []
    for entry in entries:
        value = entry.load()
        if callable(value) or not isinstance(value, Mapping):
            raise ManifestError(
                f'{getattr(entry, "name", "plugin")}: storage entry point '
                'must expose a manifest object, not a callback')
        manifests.append(validate_manifest(value))
    return manifests


def register_plugin_manifests(
    client: StorageClient,
    manifests: list[Mapping],
    *,
    command_prefix: str | None = None,
) -> list[dict]:
    prefix = command_prefix or uuid.uuid4().hex
    results = []
    for manifest in manifests:
        validated = validate_manifest(manifest)
        command_id = (
            f'plugin-manifest:{prefix}:{validated["namespace"]}:'
            f'{validated["version"]}'
        )
        results.append(client.command(
            'plugin.register', {'manifest': validated}, command_id,
            priority='maintenance', deadline=30,
        ))
    return results


__all__ = [
    'ENTRY_POINT_GROUP', 'discover_plugin_manifests',
    'register_plugin_manifests',
]
