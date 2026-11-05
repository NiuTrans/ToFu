"""Global Router: the owner's dispatch-only front door over Project Coordinators.

The Global Router is the ordinary bound Coordinator of the owner's router home
(docs/ROUTER.md R1); this package adds only what the router role needs on top
of project coordination:

- ``home``: the owner-scoped router home Git repository.
- ``binding``: ``global_router`` — the binding view and on-demand creation.
- ``policy``: ``ROUTER_TOOLS``, ``router_scope`` and the role addendum.
- ``forwarding``: ``route_to_project`` and forwarded-origin verification (R2).
- ``projects``: ``get_router_projects``.
- ``relay``: ``relay_project_report`` and ``relay_project_question`` (R3).
- ``instructions``: standing instructions (R4).
- ``tools``: the router tool adapter called by ``execute_coordinator_tool``.

Submodules are imported explicitly; this package executes nothing at import.
"""
