"""Canonical Typed-I/O diagnostic codes shared with Studio edits."""

from __future__ import annotations


IO_SIDE_MAX_PORTS = 'io.side.max_ports'
IO_PORT_MISSING = 'io.port.missing'
IO_PORT_NAME_REQUIRED = 'io.port.name.required'
IO_PORT_NAME_DUPLICATE = 'io.port.name.duplicate'
IO_PRESET_MISSING = 'io.preset.missing'


_CLIENT_FAILURE_CODES = {
    'maxPorts': IO_SIDE_MAX_PORTS,
    'missingPort': IO_PORT_MISSING,
    'missingPortName': IO_PORT_NAME_REQUIRED,
    'duplicatePortName': IO_PORT_NAME_DUPLICATE,
    'missingPreset': IO_PRESET_MISSING,
}


def io_client_failure_codes() -> dict[str, str]:
    """Return detached codes for failures Studio can reject before saving."""
    return dict(_CLIENT_FAILURE_CODES)


__all__ = [
    'IO_PORT_MISSING',
    'IO_PORT_NAME_DUPLICATE',
    'IO_PORT_NAME_REQUIRED',
    'IO_PRESET_MISSING',
    'IO_SIDE_MAX_PORTS',
    'io_client_failure_codes',
]
