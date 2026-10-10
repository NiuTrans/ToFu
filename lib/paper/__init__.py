"""Paper-domain namespace; concrete modules own every public contract.

Responsibility
--------------
This package groups paper-reading capabilities without importing or re-exporting
their implementations.  Keeping package import side-effect free prevents a
route, test, or worker from accidentally loading every paper engine and makes
the dependency owner visible at each import site.

Entry points
------------
* ``artifact_repository`` and ``library_repository`` own persisted artifacts.
* ``*_runtime`` modules own task creation, lookup, replay, and cleanup.
* ``*_engine`` modules own background execution.
* ``review``, ``images``, ``arxiv``, and ``qa_context`` own pure domain logic.
* ``paper_identity`` (one level above this package) owns paths and paper hashes.

Import concrete owners directly.  Adding package-level compatibility exports is
an architecture regression because it hides ownership and creates eager import
cycles.
"""

__all__: tuple[str, ...] = ()
