"""Reusable agent-foundation namespace.

The package root deliberately exports no cross-package facade. Import the
contract owner directly:

* lifecycle and storage-free tasks: ``task_runtime``;
* event protocol and push delivery: ``events`` and ``push``;
* admission, identity and scope: ``admission``, ``principal`` and
  ``personal_scope``;
* host seams: ``store``, ``activity`` and ``settlement``;
* capability defaults: ``profiles``;
* orchestration entry point: ``lib.tasks_pkg.orchestrator.api``;
* model dispatch entry point: ``lib.llm_dispatch.api``.

The executable core/plugin boundary is declared in
:mod:`lib.agent_core_manifest`.
"""

__all__ = ()
