"""Section 2 tool assembly ( slice 29).

Extracted 2026-07-31 from ``lib/tasks_pkg/orchestrator/_run.py``
run_task's pre-stream prep, where the block ran inline once per
invocation after config resolution (Section 1) and prefetch kick.
Byte-identical behaviour.

Three steps:

1. ``_assemble_tool_list`` — builds the per-turn tool schema from cfg
   + the mcfg feature flags. All flags are read from the ``mcfg``
   dict exactly as the inline original did (subscript for guaranteed
   keys, ``.get(..., False)`` for human_guidance / scheduler).
2. ``task['_tool_schema'] = tool_list`` stash — the compaction
   token-gate accounts for the tool-schema cost (the schema JSON
   ships in every request and the gateway tokenizes all of it, but
   the proactive gate only saw `messages`).

Returns ``(tool_list, has_real_tools)``.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.model_config import _assemble_tool_list


logger = get_logger(__name__)


def _disable_tools_for_turn(task) -> None:
    """Clear every tool authority/projection field for a text-only turn."""
    task['_tool_gateway_names'] = []
    task['_tool_schema'] = []
    task['_executable_tool_catalog'] = []
    task['_toolExecutionScope'] = 'available'
    task['_toolDiscoveryPolicyByName'] = {}
    task['_toolSearchCatalogSize'] = 0
    task['_toolSearchableCount'] = 0
    task['_toolSearchMode'] = 'off'
    task['_toolScriptSafeByName'] = {}
    task['_executableToolNamespaceByName'] = {}
    task['_executableToolSearchTextByName'] = {}
    task['_toolContractDocumentsByName'] = {}
    task['_frontendSelectedToolNames'] = []
    task['_toolNamespaceByName'] = {}
    task.pop('_toolExposureTelemetry', None)


def _attach_gateway_contract_documents(task, search_mode: str) -> bool:
    """Add contracts for the stable local gateway tools added after config.

    Registry-backed documents are assembled in model config. The local
    ``search_tools`` / ``execute_tools`` schemas are injected later at the
    provider boundary, so their execution contracts must be compiled at the
    same boundary or validation correctly fails closed on every gateway call.
    Returns whether the gateway documents were attached; ``False`` tells the
    caller to continue this otherwise valid turn without any tools.
    """
    from lib.tools.contracts import compile_execution_contract_documents
    from lib.tools.gateway import (
        EXECUTE_TOOLS_NAME,
        SEARCH_TOOLS_NAME,
        gateway_tool_schemas,
    )

    include_search = search_mode != 'off'
    task['_tool_gateway_names'] = [EXECUTE_TOOLS_NAME]
    if include_search:
        task['_tool_gateway_names'].append(SEARCH_TOOLS_NAME)

    try:
        gateway_documents = compile_execution_contract_documents(
            gateway_tool_schemas(
                include_search=include_search,
                include_execute=True,
            ),
            namespace='gateway',
        )
    except Exception as error:
        # These documents and schemas are derived request metadata, not the
        # authority itself. Never advertise an unvalidated gateway, but do not
        # abort an otherwise valid text turn either: the caller disables every
        # tool for this turn, which is fail-closed for side effects and
        # fail-soft for the main orchestration flow.
        task['_tool_gateway_names'] = []
        logger.error(
            '[ToolAssembly] gateway contract compilation failed; disabling '
            'tools for this turn and continuing text-only: %s',
            error, exc_info=True)
        return False
    task['_toolContractDocumentsByName'].update(gateway_documents)
    return True


def assemble_round_tools(cfg, task, mcfg):
    """Assemble this turn's tool schema.

    ``cfg`` / ``task`` / ``mcfg`` are positional carriers (mcfg is the
    resolved model-config dict from Section 1). Returns the 2-tuple
    ``(tool_list, has_real_tools)``. Tool availability remains stable for the
    entire turn and is not tied to a round counter.
    """
    try:
        tool_list, has_real_tools = _assemble_tool_list(
            cfg, mcfg['project_path'], mcfg['project_enabled'], task['id'],
            mcfg['search_mode'], mcfg['search_enabled'],
            mcfg['fetch_enabled'],
            mcfg['code_exec_enabled'], mcfg['browser_enabled'],
            mcfg['desktop_enabled'],
            image_gen_enabled=mcfg['image_gen_enabled'],
            human_guidance_enabled=mcfg.get('human_guidance_enabled', False),
            scheduler_enabled=mcfg.get('scheduler_enabled', False),
            messages=task['messages'],
            conv_id=task.get('convId', ''),
        )
    except Exception as error:
        # Tool schemas and their lookup metadata are a derived capability
        # projection. A broken registry/plugin must never gain execution
        # authority, but it also need not prevent the model from answering in
        # plain text. Clear every task-owned authority field because assembly
        # may have failed after producing only part of the projection.
        logger.error(
            '[ToolAssembly] registry assembly failed; disabling tools for '
            'this turn and continuing text-only: %s',
            error, exc_info=True)
        _disable_tools_for_turn(task)
        return [], False

    # Stash the assembled tool schema on the task so the compaction
    # token-gate can account for its cost. The tool-schema JSON ships
    # in every request and the gateway tokenizes all of it, but the
    # proactive gate (_count_tokens_authoritative) only saw `messages`
    # — under-counting by the full tool-schema size. Stashing here
    # (rather than threading through run_compaction_pipeline →
    # force_compact_if_needed → _should_force_compact) keeps the
    # pipeline signatures untouched.
    task['_tool_schema'] = tool_list
    _executable_catalog = cfg.get('_executableToolCatalog')
    task['_executable_tool_catalog'] = list(
        tool_list or []
        if not isinstance(_executable_catalog, list)
        else _executable_catalog)
    task['_toolExecutionScope'] = str(
        cfg.get('_toolExecutionScope') or 'available')
    task['_toolDiscoveryPolicyByName'] = dict(
        cfg.get('_toolDiscoveryPolicyByName') or {})
    _catalog_size = cfg.get('_toolSearchCatalogSize')
    task['_toolSearchCatalogSize'] = int(
        len(task['_executable_tool_catalog'])
        if _catalog_size is None else _catalog_size)
    task['_toolSearchableCount'] = int(
        cfg.get('_toolSearchableCount') or 0)
    task['_toolSearchMode'] = str(cfg.get('_toolSearchMode') or '')
    task['_toolScriptSafeByName'] = dict(
        cfg.get('_toolScriptSafeByName') or {})
    task['_executableToolNamespaceByName'] = dict(
        cfg.get('_executableToolNamespaceByName') or
        cfg.get('_toolNamespaceByName') or {})
    task['_executableToolSearchTextByName'] = dict(
        cfg.get('_executableToolSearchTextByName') or {})
    task['_toolContractDocumentsByName'] = dict(
        cfg.get('_toolContractDocumentsByName') or {})
    from lib.context_experiment_flags import normalize_context_experiment_flags
    _search_mode = (task['_toolSearchMode']
                    or normalize_context_experiment_flags(
                        cfg)['tools']['toolSearch'])
    # Provider-boundary strategy decides whether the stable gateway schema is
    # visible. Admission remains task-owned: every nested name is resolved
    # against the immutable executable catalog and enters the ordinary
    # approval / hook / executor pipeline. A derived contract defect must not
    # advertise a gateway that rejects every call, but it also must not abort
    # an otherwise valid text turn. Disable all tools for this turn: closed for
    # side effects, soft for the main orchestration flow.
    if not _attach_gateway_contract_documents(task, _search_mode):
        _disable_tools_for_turn(task)
        return [], False
    if isinstance(cfg.get('_toolExposureTelemetry'), dict):
        task['_toolExposureTelemetry'] = dict(cfg['_toolExposureTelemetry'])
    task['_frontendSelectedToolNames'] = list(
        cfg.get('_frontendSelectedToolNames') or [])
    task['_toolNamespaceByName'] = dict(
        cfg.get('_toolNamespaceByName') or {})

    return tool_list, has_real_tools
