"""Router Watchdog: an independent monitor session beside the Global Router.

The Watchdog (docs/ROUTER.md R7) is the ordinary bound Coordinator of one more
owner-scoped path inside the router home, so it reuses the coordination
binding, role, tool-ceiling and turn machinery instead of a new runtime. It
never executes or delegates work and never holds human authority.

- ``binding``: ``router_watchdog`` — identity, binding view and provisioning.
- ``policy``: ``WATCHDOG_TOOLS`` and the Watchdog role.
- ``signals``: durable, idempotent fault signals and the wake that follows them.
- ``snapshot``: the deterministic owner-scoped health snapshot.
- ``review``: periodic review and one-shot re-checks over the scheduler.
- ``tools``: the Watchdog tool adapter called by ``execute_coordinator_tool``.

Submodules are imported explicitly; this package executes nothing at import.
"""
