"""Task-processing domain namespace.

Import services from the module that owns them:

- :mod:`lib.tasks_pkg.manager` owns registered-task lifecycle and streaming.
- :mod:`lib.tasks_pkg.spawn` owns worker submission and serving-loop bindings.
- :mod:`lib.tasks_pkg.orchestrator` owns the agent turn loop.
- :mod:`lib.tasks_pkg.executor` owns tool execution.
- :mod:`lib.tasks_pkg.compaction` owns context compression.

This root deliberately exports no service facade.  Concrete imports keep
dependencies, monkeypatch targets, and ownership visible to language models.
"""

__all__: tuple[str, ...] = ()
