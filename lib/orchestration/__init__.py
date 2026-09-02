"""Orchestration domain package.

This package deliberately re-exports no symbols. Import the module that owns
the behavior so code search has one unambiguous destination:

* graph schema and validation: ``_definition_contract`` and ``_validate``;
* graph construction and projection: ``_builtin_definitions``, ``_layout``,
  ``_execution_projection`` and ``_subflow_expansion``;
* authoring and definition use cases: ``authoring_service`` and
  ``definition_service``;
* runtime use cases: ``runtime_start_service``, ``runtime_mutation_service``
  and ``run_service``;
* owner-scoped persistence: ``store`` and ``sidecar_run_store``.

Public wire documents are owned by the focused ``*_wire_contract`` modules.
The architectural map and dependency rules live in
``docs/modules/orchestration_dag.md``.
"""
