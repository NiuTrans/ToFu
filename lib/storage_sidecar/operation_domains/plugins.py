"""Declarative plugin operation registrations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    'plugin.register': ops.OperationSpec('command', True, ops._plugin_register),
    'plugin.manifest.get': ops.OperationSpec(
        'query', False, ops._plugin_manifest_get),
}

__all__ = ['OPERATIONS']
