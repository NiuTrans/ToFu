"""Paper-report engine namespace.

Import ``lib.paper.report_engine.worker`` for execution and the focused sibling
modules for metadata or post-report hooks.  This package performs no eager
imports so dependency ownership remains visible and import cycles stay local.
"""

__all__: tuple[str, ...] = ()
