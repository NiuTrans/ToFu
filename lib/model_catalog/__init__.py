"""One-way legacy model identity helpers for model-routing v2 migration.

The former model-catalog v1 authority, third-party enrichment feeds, and
server-config projection paths have been removed. Only the pure
``_creator_identity`` module remains so migration can attribute decorated
legacy model IDs without network or runtime state.
"""

from . import _creator_identity


__all__ = ['_creator_identity']
