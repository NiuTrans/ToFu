"""Project pack manifests onto the browser adapter registry.

Imported lazily by lib/browser/adapters.py at sync time: this module imports
the adapter contract, so a module-level import from adapters would cycle.
"""

from __future__ import annotations

from lib.browser.adapters import AdapterCommand, SiteAdapter

from ._interpreter import operation_capabilities, operation_handler
from ._manifest import PackManifest


def pack_to_adapter(pack: PackManifest) -> SiteAdapter:
    commands = tuple(
        AdapterCommand(
            operation.name,
            operation.description,
            access=operation.access,
            input_schema=operation.input_schema,
            output_schema=operation.output_schema,
            required_capabilities=operation_capabilities(operation),
            min_interval_s=operation.min_interval_s,
            cooldown_s=operation.cooldown_s,
            session=operation.session,
            window_mode=operation.window_mode,
            timeout=operation.timeout,
            handler=operation_handler(pack, operation),
        )
        for operation in pack.operations
    )
    return SiteAdapter(
        id=pack.id,
        name=pack.name,
        domains=pack.domains,
        aliases=pack.aliases,
        commands=commands,
        login_url=pack.auth.login_url if pack.auth else '',
        risk_notice=pack.auth.risk_notice if pack.auth else '',
        builtin=False,
        version=pack.version,
        routing_hint=pack.routing_hint,
    )
