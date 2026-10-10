"""routes/api_v4/ — dual-mounted API v4 product surface.

Complements ``.bootstrap`` (the always-on negotiation contract: ``/api/v4/meta``
+ ``/api/v4/openapi.json``). This package projects EVERY v1 domain blueprint
onto ``/api/v4`` via :func:`lib.api_v4.copy_blueprint_to_v4`, rewriting each
rule's ``/api/v1`` path prefix and attaching the ``{data, meta}`` success
envelope / RFC 7807 error contract at the boundary. Domain handlers are shared
with v1 — only the wire path and envelope differ, never behaviour.

Registration is gated by ``API_V4_ENABLED`` (default off) at assembly time and
mounted by :func:`routes.register_all`. The projection enumerates
``routes.api_v1.ALL_V1_BLUEPRINTS`` so a new v1 domain is mirrored onto v4
automatically — there is no hand-maintained batch list to drift. See
``docs/contracts/API_V4_MIGRATION.md``.
"""

from __future__ import annotations

from typing import Any

from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
from routes.api_v1 import ALL_V1_BLUEPRINTS

from .bootstrap import api_v4_bp


def _project(source: Any) -> Any:
    return attach_v4_envelope(
        copy_blueprint_to_v4(source, name=f'{source.name}_v4'))


#: Every public v1 domain, mirrored onto ``/api/v4`` in declaration order.
#: Totality is enforced by ``tests/api/test_api_v4_product.py`` (every v1 rule must
#: have a v4 twin), so this list can never silently drift from the v1 surface.
API_V4_PRODUCT_BLUEPRINTS = tuple(_project(bp) for bp in ALL_V1_BLUEPRINTS)

__all__ = ['API_V4_PRODUCT_BLUEPRINTS', 'api_v4_bp']
