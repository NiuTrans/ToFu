"""Tool execution for the paper-report agent — a THIN ADAPTER over chat's seams.

Two execution families, both owned by chat and reused here:

* ``web_search`` / ``fetch_url`` — chat's helpers (``_web_search_one`` /
  ``_fetch_url_one`` from ``lib.tasks_pkg.handlers.search``) so read-mode tool
  rounds emit the EXACT same display schema the frontend's
  ``renderToolRoundsHTML`` expects — vertical cards, engine-source breakdown,
  filtered-vs-raw char counts, File-Asset staging labels, rejected-scheme rows.
  Never re-implement the search/fetch call here: a parallel implementation
  silently drops whatever fields the chat helper computes.
* everything else (read_files / code_exec / memory / todo / scheduler / …) —
  routed through the SHARED single-tool dispatch
  (``lib.tasks_pkg.executor._execute_tool_one``), so the exact handlers chat
  runs serve the paper engines too. ``execute_paper_tool`` only translates
  paper's 5-tuple result contract + event/display schema; per-tool branches
  must NEVER grow here (charter: fix the chassis, don't patch the caller).

Also here: ``make_paper_exec_shim`` (the task-dict shim the shared dispatch
expects, with the explicit unattended auto-approval policy),
``PaperToolResultBudgetV2`` (the shared 8k/result + 24k/round owner-scoped
artifact contract), and ``paper_effective_tool_name`` (the no-project
run_command → code_exec flip).
"""

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from lib.log import audit_log, get_logger
from lib.tasks_pkg.handlers._adapter import run_batch_concurrent
# Reuse chat's canonical seams — DON'T reimplement them here:
#   • parse_and_repair_tool_args → JSON-decode + schema repair (the
#     bare-string-`queries` → single-element-array fix lives in ONE place,
#     so it covers chat AND the paper agents at once).
#   • tool_round_label → the exact string/dict-safe label chat renders,
#     incl. the multi-line batch form and empty-list guards.
from lib.tasks_pkg.tool_display import _short_url
from lib.tasks_pkg.tool_display import tool_round_label as display_query_for
from lib.tool_input_repair import parse_and_repair_tool_args
from lib.tools.contracts import (
    ToolContractError,
    compile_execution_contract_documents,
    validate_tool_arguments_from_documents,
)
from lib.tools.result_projection import (
    TOOL_RESULT_PRODUCER_METADATA_KEY,
    TOOL_RESULT_PROJECTION_ITEMS_KEY,
)
logger = get_logger(__name__)

@dataclass(frozen=True)
class PaperSearchBackend:
    """Lazy adapter to chat's canonical search and fetch presentation seams."""

    web_search_one: Any
    fetch_url_one: Any
    format_search_response: Any
    format_search_display: Any
    format_fetch_display: Any
    vertical_header_for_llm: Any
    vertical_to_sse_payload: Any


def load_paper_search_backend() -> PaperSearchBackend:
    """Load optional search dependencies only for an actual tool call."""
    import importlib

    from lib.tasks_pkg.handlers.search import _core as search_core
    from lib.tasks_pkg.handlers.search import _display as search_display

    tofu_search_module = importlib.import_module('tofu_search.search')
    return PaperSearchBackend(
        web_search_one=search_core._web_search_one,
        fetch_url_one=search_core._fetch_url_one,
        format_search_response=tofu_search_module.format_search_for_tool_response,
        format_search_display=search_display._format_search_display_for_results,
        format_fetch_display=search_display._format_fetch_display,
        vertical_header_for_llm=search_display._vertical_header_for_llm,
        vertical_to_sse_payload=search_display._vertical_to_sse_payload,
    )


__all__ = ['execute_paper_tool', 'make_research_tool_executor',
           'make_paper_exec_shim', 'cap_tool_result',
           'paper_effective_tool_name', 'freeze_paper_tool_epoch',
           'build_research_tool_schemas', 'build_paper_full_tool_context',
           'build_paper_full_tool_epoch', 'apply_paper_tool_epoch_guidance',
           'PaperToolEpochV2', 'PaperSearchBackend', 'load_paper_search_backend',
           'PaperToolResultBudgetV2']


_PAPER_FULL_DIRECT_NAMES = frozenset({
    'web_search',
    'fetch_url',
    'read_files',
    'inspect_image',
    # These two tiny schemas avoid an extra discovery round immediately after
    # an 8k result is truncated. Execution still fails closed without an owner.
    'read_tool_artifact',
    'search_tool_artifact',
})
_PAPER_UNATTENDED_BASE_EXCLUDED_NAMES = frozenset({'ask_human'})


def _paper_unattended_excluded_names() -> frozenset[str]:
    """Return capabilities that cannot truthfully execute without a person.

    Paper engines have no attended approval UI. Ordinary writes follow their
    explicit audited auto-apply policy, but ``confirmation_tools`` can only
    execute with a one-use receipt minted by that UI. Deriving this set from
    the registry prevents each new high-risk family from becoming a visible,
    permanently rejecting paper capability until someone remembers a second
    paper-specific denylist.
    """
    from lib.tools.registry import all_specs

    names = set(_PAPER_UNATTENDED_BASE_EXCLUDED_NAMES)
    for spec in all_specs():
        names.update(spec.confirmation_tools)
    return frozenset(names)


def build_research_tool_schemas() -> list[dict]:
    """Build the current search/fetch schemas for research-only paper agents."""
    from lib.tools.search import build_fetch_url_tool, build_search_tool

    return [build_search_tool(), build_fetch_url_tool()]


def build_paper_full_tool_context(*, cfg=None, owner_user_id=0):
    """Assemble one project-less chat-tier registry snapshot for paper agents."""
    from lib.tools.registry import (
        ToolContext,
        assemble_tool_list,
        resolve_enabled_plugins,
    )

    cfg = dict(cfg or {})
    context = ToolContext(
        cfg=cfg,
        task_id='',
        project_path='',
        project_enabled=False,
        search_mode='multi',
        search_enabled=True,
        fetch_enabled=True,
        code_exec_enabled=True,
        browser_enabled=False,
        desktop_enabled=False,
        enabled_plugins=resolve_enabled_plugins(cfg),
        owner_user_id=int(owner_user_id or 0),
    )
    tools, _has_base = assemble_tool_list(context)
    return tools, context


def _paper_schema_name(schema):
    if not isinstance(schema, dict):
        return ''
    function = schema.get('function')
    if isinstance(function, dict):
        return str(function.get('name') or '')
    return str(schema.get('name') or '')


@dataclass(frozen=True)
class PaperToolEpochV2:
    """One immutable-by-convention paper tool visibility/authority snapshot.

    ``wire_schemas`` is the bounded provider projection.  The larger
    ``executable_schemas`` catalog stays server-side and is the only authority
    searched and executed by the local gateways.  All maps are derived from the
    same registry/ToolContract snapshot, preventing a search hit from resolving
    against a different schema or permission epoch.
    """

    wire_schemas: tuple[dict[str, Any], ...]
    executable_schemas: tuple[dict[str, Any], ...]
    contract_documents_by_name: dict[str, dict[str, Any]]
    discovery_policy_by_name: dict[str, str]
    namespace_by_name: dict[str, str]
    search_text_by_name: dict[str, str]
    script_safe_by_name: dict[str, bool]
    schema_tokens: int
    gateway_schema_tokens: int
    schema_budget_tokens: int
    result_envelope: str
    epoch_hash: str
    degraded_reason: str = ''

    def telemetry(self):
        """Return the bounded benchmark/runtime projection for this epoch."""
        telemetry = {
            'contractVersion': 'tofu.paper-tool-epoch/v2',
            'epochHash': self.epoch_hash,
            'wireSchemaTokens': self.schema_tokens,
            'gatewaySchemaTokens': self.gateway_schema_tokens,
            'configuredSchemaBudgetTokens': self.schema_budget_tokens,
            'resultEnvelope': self.result_envelope,
            'wireToolCount': len(self.wire_schemas),
            'executableToolCount': len(self.executable_schemas),
            'searchableToolCount': sum(
                value == 'searchable'
                for value in self.discovery_policy_by_name.values()),
        }
        if self.degraded_reason:
            telemetry['degradedReason'] = self.degraded_reason
        return telemetry


def _resolve_paper_schema_budget(model, cfg, explicit_budget):
    if explicit_budget is not None:
        return max(0, int(explicit_budget))
    config = cfg if isinstance(cfg, dict) else {}
    tools_cfg = config.get('tools')
    if isinstance(tools_cfg, dict) and 'schemaBudgetTokens' in tools_cfg:
        return max(0, int(tools_cfg.get('schemaBudgetTokens') or 0))
    if 'tools.schemaBudgetTokens' in config:
        return max(0, int(config.get('tools.schemaBudgetTokens') or 0))
    return 0


def _paper_schema_budget_is_explicit(cfg, explicit_budget):
    if explicit_budget is not None:
        return True
    config = cfg if isinstance(cfg, dict) else {}
    tools_cfg = config.get('tools')
    return (
        isinstance(tools_cfg, dict) and 'schemaBudgetTokens' in tools_cfg
    ) or 'tools.schemaBudgetTokens' in config


def _resolve_paper_result_envelope(cfg):
    """Use safe V2 by default, while honoring an explicit experiment arm."""
    config = cfg if isinstance(cfg, dict) else {}
    tools_cfg = config.get('tools')
    value = (tools_cfg.get('resultEnvelope')
             if isinstance(tools_cfg, dict) and 'resultEnvelope' in tools_cfg
             else config.get('tools.resultEnvelope'))
    if value is None:
        return 'v2'
    normalized = str(value or '').strip().lower()
    if normalized not in {'legacy', 'v2'}:
        logger.warning(
            '[Paper:ToolResult] invalid resultEnvelope=%r; using safe v2',
            value)
        return 'v2'
    return normalized


def _build_paper_full_tool_epoch(*, owner_user_id=None, model='', cfg=None,
                                 schema_budget_tokens=None):
    """Build the bounded full-paper Tool Search epoch from one registry pass.

    Full-paper agents keep the chat-tier executable catalog (including media,
    scheduler, memory, and agent capabilities). The default provider surface is
    the full uncapped catalog for every model. An explicit, model-neutral budget
    may defer optional tools behind ``search_tools``/``execute_tools``; that
    gateway pair independently targets 600 tokens. Budget drift degrades schema
    detail but never makes a report request unavailable.
    """
    from lib.tools.gateway import (
        EXECUTE_TOOLS_NAME,
        LOCAL_GATEWAY_MAX_TOKENS,
        SEARCH_TOOLS_NAME,
        local_wire_tools,
        tool_schema_tokens,
    )

    owner_id = int(owner_user_id or 0)
    if owner_id < 0:
        raise ValueError('paper tool epoch owner must be non-negative')
    budget = _resolve_paper_schema_budget(
        model, cfg or {}, schema_budget_tokens)
    budget_explicit = _paper_schema_budget_is_explicit(
        cfg or {}, schema_budget_tokens)
    result_envelope = _resolve_paper_result_envelope(cfg or {})
    registry_cfg = copy.deepcopy(dict(cfg or {}))
    tools_cfg = dict(registry_cfg.get('tools') or {})
    tools_cfg['resultEnvelope'] = result_envelope
    registry_cfg['tools'] = tools_cfg
    if owner_id > 0:
        registry_cfg['userId'] = owner_id
    registry_wire, ctx = build_paper_full_tool_context(
        cfg=registry_cfg, owner_user_id=owner_id)
    unattended_excluded_names = _paper_unattended_excluded_names()

    executable = []
    # The uncapped product default retains the complete task-authorized catalog.
    # Explicit zero remains the registered long-agent control arm's historical
    # eager-only surface; it is an experiment choice, not a model-name default.
    source_catalog = (
        ctx.executable_tool_catalog
        if budget or not budget_explicit
        else registry_wire
    )
    for schema in source_catalog:
        name = _paper_schema_name(schema)
        if not name or name in unattended_excluded_names:
            continue
        if owner_id <= 0 and name in {
                'read_tool_artifact', 'search_tool_artifact'}:
            continue
        executable.append(copy.deepcopy(schema))
    # The legacy full-paper surface predated Tool Search but already had the two
    # owner continuation tools. Preserve that exact rollback shape when an arm
    # explicitly selects schemaBudgetTokens=0.
    if not budget and owner_id > 0:
        known = {_paper_schema_name(schema) for schema in executable}
        for schema in ctx.executable_tool_catalog:
            name = _paper_schema_name(schema)
            if name in {'read_tool_artifact', 'search_tool_artifact'} \
                    and name not in known:
                executable.append(copy.deepcopy(schema))
                known.add(name)

    executable_names = [_paper_schema_name(schema) for schema in executable]
    if len(executable_names) != len(set(executable_names)):
        raise ValueError('duplicate executable tool contract in paper full epoch')
    executable_name_set = set(executable_names)
    policy = ({name: ('eager' if name in _PAPER_FULL_DIRECT_NAMES
                      else 'searchable')
               for name in executable_names}
              if budget else {name: 'eager' for name in executable_names})
    searchable_count = sum(value == 'searchable' for value in policy.values())
    wire = (local_wire_tools(
        executable,
        discovery_policy_by_name=policy,
        discovery_catalog_size=len(executable),
        searchable_count=searchable_count,
        include_search=True,
        schema_budget_tokens=budget,
        model=model,
        priority_names=_PAPER_FULL_DIRECT_NAMES,
        required_names=_PAPER_FULL_DIRECT_NAMES,
    ) if budget else copy.deepcopy(executable))
    wire_names = {_paper_schema_name(schema) for schema in wire}
    hidden_names = executable_name_set - wire_names
    gateway_names = {SEARCH_TOOLS_NAME, EXECUTE_TOOLS_NAME}
    if hidden_names and not gateway_names.issubset(wire_names):
        raise ValueError(
            'paper schema budget hid executable tools without Tool Search')

    required_direct = {'web_search', 'fetch_url', 'read_files'}
    missing_direct = required_direct - wire_names
    if missing_direct:
        raise ValueError(
            'paper schema budget cannot retain required direct tools: '
            + ', '.join(sorted(missing_direct)))

    # Gateway schemas are visibility capabilities rather than members of the
    # executable catalog, but their outer calls still need exact validation.
    gateway_schemas = [
        schema for schema in wire
        if _paper_schema_name(schema) in gateway_names
    ]
    contract_schemas = [*executable, *gateway_schemas]
    documents = compile_execution_contract_documents(
        contract_schemas,
        authoritative_documents_by_name=ctx.tool_contract_documents_by_name,
        namespace='paper',
    )
    wire_tokens = tool_schema_tokens(wire, model=model)
    gateway_tokens = (tool_schema_tokens(gateway_schemas, model=model)
                      if gateway_schemas else 0)
    if budget and wire_tokens > budget:
        logger.warning(
            '[Paper:ToolEpoch] functional wire schema exceeds cost target: '
            'tokens=%d target=%d; request continues', wire_tokens, budget)
    if gateway_tokens > LOCAL_GATEWAY_MAX_TOKENS:
        logger.warning(
            '[Paper:ToolEpoch] compact gateway exceeds cost target: '
            'tokens=%d target=%d; request continues',
            gateway_tokens, LOCAL_GATEWAY_MAX_TOKENS)
    epoch_payload = json.dumps({
        'wire': wire,
        'executable': executable,
        'policy': policy,
        'resultEnvelope': result_envelope,
        'namespaces': {
            name: str(ctx.tool_namespace_by_name.get(name) or 'paper')
            for name in executable_names
        },
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    epoch_hash = hashlib.sha256(epoch_payload.encode('utf-8')).hexdigest()

    return PaperToolEpochV2(
        wire_schemas=tuple(copy.deepcopy(wire)),
        executable_schemas=tuple(copy.deepcopy(executable)),
        contract_documents_by_name=dict(documents),
        discovery_policy_by_name=policy,
        namespace_by_name={
            name: str(ctx.tool_namespace_by_name.get(name) or 'paper')
            for name in executable_names
        },
        search_text_by_name={
            name: str(ctx.search_text_by_name.get(name) or '')
            for name in executable_names
        },
        script_safe_by_name={
            name: bool(ctx.script_safe_by_name.get(name))
            for name in executable_names
        },
        schema_tokens=wire_tokens,
        gateway_schema_tokens=gateway_tokens,
        schema_budget_tokens=budget,
        result_envelope=result_envelope,
        epoch_hash=epoch_hash,
    )


def _text_only_paper_tool_epoch(*, model='', cfg=None,
                                schema_budget_tokens=None, error):
    """Return a fail-closed epoch while preserving the paper text request."""
    try:
        budget = _resolve_paper_schema_budget(
            model, cfg or {}, schema_budget_tokens)
    except Exception as error:
        logger.debug(
            'Paper text-only fallback could not resolve schema budget: %s',
            type(error).__name__,
        )
        budget = 0
    try:
        result_envelope = _resolve_paper_result_envelope(cfg or {})
    except Exception as error:
        logger.debug(
            'Paper text-only fallback could not resolve result envelope: %s',
            type(error).__name__,
        )
        result_envelope = 'v2'
    epoch_payload = json.dumps({
        'wire': [],
        'executable': [],
        'policy': {},
        'resultEnvelope': result_envelope,
        'namespaces': {},
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    # Keep user-visible/task telemetry useful without copying arbitrary plugin
    # exception text, which may contain provider configuration or credentials.
    reason = f'tool_epoch_assembly_failed:{type(error).__name__}'
    return PaperToolEpochV2(
        wire_schemas=(),
        executable_schemas=(),
        contract_documents_by_name={},
        discovery_policy_by_name={},
        namespace_by_name={},
        search_text_by_name={},
        script_safe_by_name={},
        schema_tokens=0,
        gateway_schema_tokens=0,
        schema_budget_tokens=budget,
        result_envelope=result_envelope,
        epoch_hash=hashlib.sha256(epoch_payload.encode('utf-8')).hexdigest(),
        degraded_reason=reason,
    )


def build_paper_full_tool_epoch(*, owner_user_id=None, model='', cfg=None,
                                schema_budget_tokens=None):
    """Build a paper tool epoch, degrading derived defects to text-only.

    Owner identity remains a hard boundary. Once that input is valid, registry,
    projection, and contract compilation are derived capabilities: on a defect
    no tool may be advertised or executed, while the report/Q&A model request
    can still run without tools.
    """
    owner_id = int(owner_user_id or 0)
    if owner_id < 0:
        raise ValueError('paper tool epoch owner must be non-negative')
    try:
        return _build_paper_full_tool_epoch(
            owner_user_id=owner_id,
            model=model,
            cfg=cfg,
            schema_budget_tokens=schema_budget_tokens,
        )
    except Exception as error:
        logger.error(
            '[Paper:ToolEpoch] derived tool assembly failed; disabling tools '
            'and continuing text-only: %s', error, exc_info=True)
        return _text_only_paper_tool_epoch(
            model=model,
            cfg=cfg,
            schema_budget_tokens=schema_budget_tokens,
            error=error,
        )


def apply_paper_tool_epoch_guidance(messages, epoch, *, lang='en'):
    """Append gateway guidance when this arm exposes the gateway."""
    wire_names = {_paper_schema_name(schema) for schema in epoch.wire_schemas}
    if 'search_tools' not in wire_names or 'execute_tools' not in wire_names:
        return False
    if str(lang or '').lower().startswith('zh'):
        guidance = (
            '为控制上下文，部分已授权工具未直接显示。需要这些能力时先调用 '
            'search_tools，再严格使用返回的精确工具名和 arguments_schema 调用 '
            'execute_tools；不要猜隐藏工具名。')
    else:
        guidance = (
            'To bound context, some authorized tools are not shown directly. '
            'For such a capability, call search_tools first, then execute_tools '
            'with the exact returned name and arguments_schema; never guess a '
            'hidden tool name.')
    if any(guidance in str(message.get('content', ''))
           for message in messages or () if isinstance(message, dict)):
        return True
    messages.append({'role': 'user', 'content': guidance, '_isMeta': True})
    return True


def paper_effective_tool_name(fn_name):
    """The dispatch/display tool name for a call in a paper (project-less) engine.

    Mirrors chat's tool_display override (``_build_tool_round_entry``): with no
    project attached, ``run_command`` IS the standalone code_exec tool (its
    schema is a deepcopy of the project run_command schema), and the registry
    keys its special ``__code_exec__`` handler + the frontend's terminal-block
    rendering off ``round_entry['toolName'] == 'code_exec'`` — not
    ``'run_command'``. Without this flip a paper ``run_command`` call would
    fall through to the PROJECT handler and die with "No project path".
    """
    from lib.tools.code_exec import CODE_EXEC_TOOL_NAMES
    if fn_name in CODE_EXEC_TOOL_NAMES:
        return 'code_exec'
    return fn_name


def freeze_paper_tool_epoch(schemas, *, owner_user_id=None):
    """Freeze the exact schemas sent in one paper-agent tool epoch.

    Lazy registry-backed lists are materialized once, then the same snapshot
    compiles the execution authority. Provider visibility and dispatch can
    therefore never observe different registry states within a round.  A
    positive owner adds the two bounded artifact-continuation tools.  They are
    directly relevant only after ToolResultEnvelopeV2 is active, and are never
    added to an empty/forced-final epoch.
    """
    frozen_schemas = list(schemas or ())
    owner_id = int(owner_user_id or 0)
    if owner_id < 0:
        raise ValueError('paper tool epoch owner must be non-negative')
    if frozen_schemas and owner_id > 0:
        from lib.tools.tool_result_artifacts import (
            build_tool_result_artifact_tools,
        )
        known_names = {
            str(((schema.get('function') or {}).get('name') or ''))
            for schema in frozen_schemas if isinstance(schema, dict)
        }
        frozen_schemas.extend(
            schema for schema in build_tool_result_artifact_tools()
            if str(((schema.get('function') or {}).get('name') or ''))
            not in known_names
        )
    documents = compile_execution_contract_documents(
        frozen_schemas, namespace='paper')
    return frozen_schemas, documents


def make_paper_exec_shim(*, task_id, conv_id='', abort=None, cfg=None,
                         owner_user_id=None,
                         tool_contract_documents_by_name=None,
                         tool_epoch=None, model=''):
    """Build the shim task dict the SHARED dispatch (``_execute_tool_one``)
    expects, for a headless paper engine run.

    Approval policy (EXPLICIT — do not change silently): chat's write-approval
    gate lives in the batch pipeline and fires ONLY for ATTENDED tasks (a
    human must be present to answer); unattended / headless chat tasks
    auto-apply. Paper engines are unattended by construction — there is no
    human to prompt — so they inherit chat's own unattended semantics:
    ordinary write-partition tools (memory CRUD, scheduler create/manage, MCP
    tools) execute without a prompt, and EVERY such call is recorded via
    ``audit_log('paper_tool_auto_approve', …)`` in ``_execute_shared_tool``.
    Tools whose registry contract requires attended confirmation are removed
    from the frozen paper epoch instead: no headless runtime may mint the
    one-use human receipt their handlers consume.
    Never route a paper engine through the attended pipeline (a background
    task must never block on a click that cannot happen), and never strip the
    audit trail (it is the visible record of this policy).

    ``_suppressEvents`` follows the swarm sub-agent precedent
    (``lib/tasks_pkg/manager/_events.py:append_event``): the inner handler's
    ``_finalize_tool_round`` / progress events never leak onto a stream — the
    paper engines emit their OWN ``tool_start`` / ``tool_done`` events from
    the finalized ``round_entry``.
    """
    if tool_epoch is not None and not isinstance(tool_epoch, PaperToolEpochV2):
        raise TypeError('tool_epoch must be PaperToolEpochV2')
    cfg_payload = dict(cfg or {})
    tools_cfg = dict(cfg_payload.get('tools') or {})
    # Keep the shared handler aligned with the exact result contract entering
    # model history. Safe shipped Paper behavior is V2; an explicit registered
    # control arm may request the bounded legacy baseline.
    tools_cfg['resultEnvelope'] = (
        tool_epoch.result_envelope if tool_epoch is not None
        else _resolve_paper_result_envelope(cfg_payload))
    cfg_payload['tools'] = tools_cfg
    shim = {
        'id': task_id,
        'convId': conv_id,
        '_suppressEvents': True,
        # Mirrored from the engine's abort predicate on EVERY call by
        # _execute_shared_tool (the shared dispatch reads task['aborted']).
        'aborted': bool(abort and abort()),
        '_abort': abort,
        '_cfg': cfg_payload,
        'model': str(model or ''),
        # Nested execute_tools children use the ordinary shared pipeline, whose
        # receipts and display projection are task-owned. They stay private to
        # this headless shim because _suppressEvents is true.
        'toolRounds': [],
        'programRuns': [],
    }
    if owner_user_id is not None:
        owner_id = int(owner_user_id)
        shim['_userId'] = owner_id
        cfg_payload.setdefault('userId', owner_id)
    if tool_epoch is not None:
        executable = copy.deepcopy(list(tool_epoch.executable_schemas))
        shim['_tool_schema'] = copy.deepcopy(list(tool_epoch.wire_schemas))
        shim['_executable_tool_catalog'] = executable
        shim['_toolContractDocumentsByName'] = dict(
            tool_epoch.contract_documents_by_name)
        shim['_toolDiscoveryPolicyByName'] = dict(
            tool_epoch.discovery_policy_by_name)
        shim['_executableToolNamespaceByName'] = dict(
            tool_epoch.namespace_by_name)
        shim['_executableToolSearchTextByName'] = dict(
            tool_epoch.search_text_by_name)
        shim['_toolScriptSafeByName'] = dict(tool_epoch.script_safe_by_name)
        shim['_toolSearchCatalogSize'] = len(executable)
        shim['_toolSearchableCount'] = sum(
            value == 'searchable'
            for value in tool_epoch.discovery_policy_by_name.values())
        shim['_toolExecutionScope'] = 'available'
        shim['_toolEpochHash'] = tool_epoch.epoch_hash
        from lib.tools.gateway import EXECUTE_TOOLS_NAME, SEARCH_TOOLS_NAME
        wire_names = {
            _paper_schema_name(schema) for schema in tool_epoch.wire_schemas}
        shim['_tool_gateway_names'] = [
            name for name in (EXECUTE_TOOLS_NAME, SEARCH_TOOLS_NAME)
            if name in wire_names]
        shim['_toolSearchMode'] = (
            'local' if SEARCH_TOOLS_NAME in wire_names else 'off')
        if EXECUTE_TOOLS_NAME in wire_names:
            # The eligible set shapes hosted-PTC activation/guidance only.
            # Local ToolScript authority is the task catalog plus the ordinary
            # contract, permission, and approval pipeline.
            shim['_ptc_local'] = {
                'tier': 'program',
                'eligible': sorted(
                    name for name, safe
                    in tool_epoch.script_safe_by_name.items() if safe),
            }
    elif tool_contract_documents_by_name is not None:
        shim['_toolContractDocumentsByName'] = dict(
            tool_contract_documents_by_name)
    return shim


def _validate_paper_tool_arguments(name, args, documents_by_name, *, task_id=''):
    """Return validated/defaulted args or a stable no-execution rejection."""
    try:
        return (
            validate_tool_arguments_from_documents(
                documents_by_name, name, args),
            None,
            None,
        )
    except ToolContractError as exc:
        metadata = exc.to_dict()
        message = (
            f'ERROR: Tool call `{name}` was NOT executed. '
            f'[{exc.code}] {exc} Path: {exc.path}. {exc.next_action}'
        )
        logger.warning(
            '[Paper:ToolContract] rejected tool=%s code=%s path=%s task=%s',
            name, exc.code, exc.path, task_id)
        audit_log(
            'paper_tool_contract_rejected', tool=name, code=exc.code,
            path=exc.path, retryable=exc.retryable, task_id=task_id)
        return {}, metadata, message


def cap_tool_result(content, tool_name, tool_use_id='', *, owner_user_id=0,
                    model='', observed_at_ms=0, world_version='',
                    tool_arguments=None, projection_items=None,
                    producer_metadata=None):
    """Build one bounded internal ToolResultEnvelopeV2 record.

    Every tool, including ``read_files``, is bounded to 8k model-visible
    tokens.  Oversized content is persisted only through the owner-scoped
    semantic artifact repository; no filesystem path or SQLite detail enters
    application/model data.  Owner-less compatibility calls remain bounded but
    honestly receive no recovery handle when persistence is unavailable.

    ``tool_use_id`` is retained for source compatibility and diagnostics; the
    content-addressed repository intentionally does not use call IDs as keys.
    """
    del tool_use_id
    from lib.tasks_pkg.compaction.api import budget_tool_result_v2
    return budget_tool_result_v2(
        tool_name, content, user_id=int(owner_user_id or 0), model=model,
        observed_at_ms=int(observed_at_ms or 0),
        world_version=str(world_version or ''),
        tool_arguments=tool_arguments,
        projection_items=projection_items,
        producer_metadata=producer_metadata,
    )


class PaperToolResultBudgetV2:
    """Own one paper loop's selected single/aggregate result contract.

    ``append`` is the only ingress for model-visible paper tool messages.
    ``finish_round`` is wired to :func:`run_agent_loop`'s ``on_round_end`` so
    duplicate or empty provider call IDs cannot evade the 24k aggregate cap.
    A positive owner enables semantic artifact recovery; zero is a bounded,
    no-persistence compatibility mode and is never promoted to a real owner.

    Safe shipped Paper requests use ``v2``. ``legacy`` exists only as the
    explicit pre-registered experiment baseline; it still passes through the
    historical hard ceiling and aggregate budget rather than becoming
    unbounded. Keeping both paths behind this one ingress prevents an arm from
    silently changing unrelated loop semantics.
    """

    def __init__(self, *, owner_user_id=None, model='',
                 result_envelope='v2', conv_id=''):
        owner_id = int(owner_user_id or 0)
        if owner_id < 0:
            raise ValueError('paper tool result owner must be non-negative')
        normalized_contract = str(result_envelope or '').strip().lower()
        if normalized_contract not in {'legacy', 'v2'}:
            raise ValueError('paper result envelope must be legacy or v2')
        self.owner_user_id = owner_id
        self.model = str(model or '')
        self.result_envelope = normalized_contract
        self.conv_id = str(conv_id or '')
        self._sequence = 0
        self._records_by_round = {}

    def telemetry(self):
        return {
            'contractVersion': 'tofu.paper-tool-result-policy/v1',
            'resultEnvelope': self.result_envelope,
            'singleResultTokenBudget': (
                8_000 if self.result_envelope == 'v2' else None),
            'roundResultTokenBudget': (
                24_000 if self.result_envelope == 'v2' else None),
        }

    def _annotate_round_entry(self, record, visible, evidence=None, *,
                              aggregate=False):
        round_entry = record.get('round_entry')
        if not isinstance(round_entry, dict):
            return
        from lib.tasks_pkg.compaction._budget import _result_tokens
        raw = record['raw_content']
        value = evidence if isinstance(evidence, dict) else {}
        round_entry.update({
            'resultContract': (
                'tofu.tool-result/v2'
                if self.result_envelope == 'v2' else 'legacy'),
            'rawToolBytes': len(raw.encode('utf-8', errors='replace')),
            'visibleToolBytes': len(visible.encode('utf-8', errors='replace')),
            'rawToolTokens': _result_tokens(raw, self.model),
            'visibleToolTokens': _result_tokens(visible, self.model),
            'toolResultArtifactRef': str(value.get('artifactRef') or ''),
            'toolResultTruncated': (
                bool(value.get('truncated'))
                if self.result_envelope == 'v2'
                else visible != raw),
            'toolContent': visible[:4000],
        })
        if value:
            round_entry['toolResultEvidence'] = dict(value)
        if aggregate:
            round_entry['aggregateResultBudgetApplied'] = True

    def append(self, messages, *, round_index, tool_name, tool_call_id,
               content, round_entry=None, world_version='',
               tool_arguments=None):
        """Budget, append, and track one tool message for its logical round."""
        import time
        raw = (content if isinstance(content, str)
               else json.dumps(content, ensure_ascii=False, default=str))
        from lib.tasks_pkg.compaction.api import mark_empty_result
        raw = mark_empty_result(tool_name, raw)
        projection_items = (
            round_entry.pop(TOOL_RESULT_PROJECTION_ITEMS_KEY, None)
            if isinstance(round_entry, dict) else None)
        producer_metadata = (
            round_entry.pop(TOOL_RESULT_PRODUCER_METADATA_KEY, None)
            if isinstance(round_entry, dict) else None)
        evidence = None
        if self.result_envelope == 'v2':
            envelope = cap_tool_result(
                raw, tool_name, tool_call_id,
                owner_user_id=self.owner_user_id, model=self.model,
                observed_at_ms=int(time.time() * 1000),
                world_version=world_version,
                tool_arguments=tool_arguments,
                projection_items=projection_items,
                producer_metadata=producer_metadata,
            )
            from lib.tools.result_envelope import split_tool_result_delivery

            delivery = split_tool_result_delivery(envelope)
            visible = delivery.model_text
            evidence = (dict(delivery.evidence)
                        if delivery.evidence is not None else None)
        else:
            from lib.tasks_pkg.compaction.api import (
                budget_tool_result,
                clamp_tool_result_text,
            )
            visible = budget_tool_result(
                tool_name, raw, tool_use_id=tool_call_id,
                conv_id=self.conv_id)
            visible = clamp_tool_result_text(
                tool_name, visible, tc_id=tool_call_id,
                conv_id=self.conv_id)
            envelope = visible
        message = {
            'role': 'tool', 'tool_call_id': tool_call_id,
            'content': visible,
        }
        messages.append(message)
        self._sequence += 1
        record = {
            'key': f'{int(round_index)}:{self._sequence}',
            'message': message,
            'tool_name': str(tool_name or ''),
            'tool_call_id': str(tool_call_id or ''),
            'raw_content': raw,
            'envelope_content': envelope,
            'round_entry': round_entry,
        }
        self._records_by_round.setdefault(int(round_index), []).append(record)
        self._annotate_round_entry(record, visible, evidence)
        return visible

    def finish_round(self, round_index):
        """Enforce the selected aggregate policy on appended tool messages."""
        records = self._records_by_round.pop(int(round_index), [])
        if not records:
            return
        from lib.tasks_pkg.compaction.api import (
            enforce_round_aggregate_budget,
            enforce_round_aggregate_budget_v2,
        )
        values = {
            record['key']: (
                (record['envelope_content']
                 if self.result_envelope == 'v2'
                 else record['message']['content']),
                record['tool_name'],
                record['tool_call_id'],
            )
            for record in records
        }
        if self.result_envelope == 'v2':
            updated = enforce_round_aggregate_budget_v2(
                values, user_id=self.owner_user_id, model=self.model,
                observed_at_ms=0,
            )
        else:
            updated = enforce_round_aggregate_budget(
                values, conv_id=self.conv_id)
        for record in records:
            bounded = updated[record['key']][0]
            evidence = None
            if self.result_envelope == 'v2':
                from lib.tools.result_envelope import (
                    split_tool_result_delivery,
                )
                delivery = split_tool_result_delivery(bounded)
                visible = delivery.model_text
                evidence = (dict(delivery.evidence)
                            if delivery.evidence is not None else None)
                record['envelope_content'] = bounded
            else:
                visible = bounded
            aggregate = visible != record['message']['content']
            record['message']['content'] = visible
            self._annotate_round_entry(
                record, visible, evidence, aggregate=aggregate)


def _execute_shared_tool(name, args, shim, round_entry, abort):
    """Route one non-search tool call through chat's SHARED single-tool dispatch.

    Returns the same 5-tuple ``execute_paper_tool`` produces for search
    tools; the display payload is whatever the chat handler finalized onto
    ``round_entry['results']`` (a ``_build_simple_meta`` / project-meta list —
    the exact shape ``renderToolRoundsHTML`` already renders for chat).
    """
    from lib.log import audit_log
    from lib.tasks_pkg.executor import _execute_tool_one
    from lib.tasks_pkg.tool_dispatch._flags import _WRITE_TOOLS
    from lib.tools.gateway import EXECUTE_TOOLS_NAME, normalize_execute_request

    # Refresh the abort mirror — the shared dispatch reads task['aborted'].
    shim['aborted'] = bool(abort and abort())

    tc_id = (round_entry or {}).get('toolCallId', '')
    # Unattended auto-approval — explicit + audited (see make_paper_exec_shim).
    # A hidden write enters through execute_tools, so normalize its child names
    # against the exact epoch before auditing; checking only the outer gateway
    # would silently erase the paper-specific approval trail.
    approval_names = [name]
    if name == EXECUTE_TOOLS_NAME:
        normalized = normalize_execute_request(
            args,
            catalog=shim.get('_executable_tool_catalog') or [],
            namespace_by_name=(
                shim.get('_executableToolNamespaceByName') or {}),
            gateway_call_id=str((round_entry or {}).get('toolCallId') or ''),
            source='paper_execute_calls',
            contract_documents_by_name=(
                shim.get('_toolContractDocumentsByName')
                if '_toolContractDocumentsByName' in shim else None),
        )
        approval_names.extend(
            str((call.get('function') or {}).get('name') or '')
            for call in normalized.get('calls') or ())
    for approval_name in dict.fromkeys(approval_names):
        # MCP tools get chat's conservative default-write classification.
        if (approval_name not in _WRITE_TOOLS
                and not approval_name.startswith('mcp__')):
            continue
        audit_log('paper_tool_auto_approve', tool=approval_name,
                  task_id=shim.get('id', ''),
                  reason='unattended_headless_engine')
        logger.info('[Paper:Tool] auto-approved write-partition tool %s '
                    '(unattended engine, task=%s)', approval_name,
                    str(shim.get('id', ''))[:8])

    tc = {'id': tc_id,
          'function': {'name': name,
                       'arguments': json.dumps(args, ensure_ascii=False)}}
    try:
        _tc_id, content, _is_search = _execute_tool_one(
            shim, tc, name, tc_id, args,
            (round_entry or {}).get('roundNum', 0),
            round_entry if round_entry is not None
            else {'query': name, 'toolCallId': tc_id},
            shim.get('_cfg') or {}, None, False)
    except Exception as e:
        logger.error('[Paper:Tool] shared dispatch of %s failed: %s',
                     name, e, exc_info=True)
        return (f'Error: tool "{name}" execution failed: {e}',
                [], None, None, None)

    # Image reads come back as a __screenshot__ DICT — the paper message
    # channel is text-only, so degrade to the text fallback with an explicit
    # note instead of crashing on dict slicing downstream.
    if isinstance(content, dict):
        if content.get('__screenshot__'):
            fallback = content.get('_text_fallback') or ''
            content = (
                (fallback + '\n\n' if fallback else '')
                + f"[Image loaded: {content.get('filename', '?')} — the paper "
                  "channel is text-only, so the image itself is not attached. "
                  "Work from the fallback text above or the file path.]")
        else:
            content = json.dumps(content, ensure_ascii=False)

    display = list((round_entry or {}).get('results') or [])
    return content, display, None, None, None


def execute_paper_tool(name, args_str, user_question='', abort=None,
                         force_vertical=None, exec_shim=None, round_entry=None,
                         contract_documents_by_name=None, search_backend=None):
    """Execute a tool call from the report agent.

    Args:
        name: tool name (``web_search`` / ``fetch_url``).
        args_str: raw tool-call arguments (JSON string, schema-repaired here).
        user_question: short context string for search relevance filtering.
        abort: optional ``() -> bool`` predicate. When it trips, queued
            (not-yet-started) items in a batch short-circuit instead of firing
            — so a Stop pressed while a report is mid-search does not spray the
            remaining batched queries/fetches. Threaded down to
            ``run_batch_concurrent``.
        force_vertical: optional vertical domain (e.g. ``'academic'``) that
            OVERRIDES whatever vertical the model chose for every web_search
            query in this call — including ``'auto'`` / ``'off'`` / a wrong
            domain. Default ``None`` leaves the model's choice untouched, so
            the shared report / QA / insight callers are byte-identical. The
            describe-to-recommend engine passes ``'academic'`` so a known-title
            lookup always consults the arXiv / Semantic Scholar JSON APIs
            (whose uptime is independent of the HTML-engine fleet and its
            per-engine circuit breakers), rather than hoping the model asks.
        exec_shim: optional shim task dict from ``make_paper_exec_shim``. When
            provided, ANY tool name beyond web_search / fetch_url is routed
            through chat's shared single-tool dispatch (full-set engines:
            report + Q&A). When absent (research-only engines), unknown names
            keep the legacy ``Unknown tool`` reply — a hallucinated tool name
            can never escape into the shared dispatch.
        round_entry: the caller's chat-shaped round entry dict. The shared
            handler finalizes it in place (``results`` + ``status``), and the
            adapter returns those results as the display payload.

    Returns:
        tuple: (tool_content_str, display_results, search_diag, engine_breakdown, verticals)
            - tool_content_str: Formatted text for the LLM.
            - display_results: List of dicts for the frontend (same schema as
              chat mode's tool_result event). In batch search each dict is
              tagged with ``_q`` (its source query) for per-query grouping.
            - search_diag: Diagnostic dict when search returns 0 results, else None.
            - engine_breakdown: Per-engine raw URL breakdown for a single-query
              web_search (mirrors chat); None for batch / fetch_url.
            - verticals: List of vertical-search payloads (HF Papers / arXiv /
              Semantic Scholar / …) for the frontend's vertical card, or None.
    """
    # JSON-decode + schema repair in one place (mirrors the chat dispatcher).
    # This coerces a schema-violating ``queries``/``urls`` string into a
    # single-element array BEFORE the per-item loops below, so a bare string
    # is never iterated character-by-character.
    args, _repair_log = parse_and_repair_tool_args(name, args_str)

    documents = contract_documents_by_name
    if (documents is None and exec_shim is not None
            and '_toolContractDocumentsByName' in exec_shim):
        documents = exec_shim['_toolContractDocumentsByName']
    args, contract_error, rejection = _validate_paper_tool_arguments(
        name, args, documents,
        task_id=str((exec_shim or {}).get('id') or ''))
    if contract_error is not None:
        if round_entry is not None:
            round_entry['status'] = 'rejected'
            round_entry['results'] = []
            round_entry['contractError'] = contract_error
            round_entry['toolContent'] = rejection[:4000]
        return rejection, [], None, None, None

    if name == 'web_search':
        try:
            backend = search_backend or load_paper_search_backend()
        except Exception as exc:
            logger.error('[Paper:Report:Tool] search backend unavailable: %s', exc)
            return f'Error: search backend unavailable: {exc}', [], None, None, None
        freshness = args.get('freshness', '')
        batch_vertical = args.get('vertical', 'auto')
        queries = args.get('queries', [])
        # Defensive: if repair could not normalize ``queries`` into a list
        # (e.g. tool schema unavailable), treat any non-list as a single
        # query rather than iterating it. Never iterate a raw string.
        if queries and not isinstance(queries, list):
            queries = [queries]
        if not queries:
            q = args.get('query', '')
            if q:
                queries = [{'query': q}]
        if not queries:
            return 'Error: no query provided', [], None, None, None

        # Build (query, freshness, vertical) specs — mirrors chat's batch path.
        query_specs = []
        for qobj in queries[:5]:
            if isinstance(qobj, dict):
                q = qobj.get('query', '')
                f = qobj.get('freshness', '') or freshness
                v = qobj.get('vertical') or batch_vertical
            elif isinstance(qobj, str):
                q, f, v = qobj, freshness, batch_vertical
            else:
                continue
            # A forced vertical (e.g. the recommend engine's 'academic') wins
            # over the model's choice — the robust JSON-API path is guaranteed
            # by code, not left to the model's discretion.
            if force_vertical:
                v = force_vertical
            if q and q.strip():
                query_specs.append((q.strip(), f, v))
        if not query_specs:
            return 'Error: no valid query provided', [], None, None, None

        query_list = [q for q, _, _ in query_specs]
        single = len(query_specs) == 1

        def _search_one(spec):
            q, f, v = spec
            logger.info('[Paper:Report:Tool] web_search query=%r', q[:100])
            # Reuse chat's helper so vertical search runs CONCURRENTLY (zero
            # added latency) and we get the engine breakdown for free.
            results, search_diag, engine_breakdown, vertical_result = backend.web_search_one(
                q, user_question, f, vertical=v)
            formatted = backend.format_search_response(
                results, search_diag=search_diag, query=q)
            if vertical_result:
                formatted = backend.vertical_header_for_llm(vertical_result) + formatted
            display = backend.format_search_display(results)
            return (formatted, display, search_diag, engine_breakdown, vertical_result)

        ordered = run_batch_concurrent(query_specs, _search_one, max_workers=2, tag='PaperSearch', abort=abort)

        all_formatted = []
        all_display = []
        all_verticals = []
        last_diag = None
        engine_breakdown_out = None
        for idx, item in enumerate(ordered):
            q = query_list[idx]
            if item is None:
                all_formatted.append(f'Search for "{q}" failed: internal error')
                continue
            formatted, display, diag, engine_breakdown, vertical_result = item
            # Tag each result with its source query so the frontend can group
            # batch results under per-query subheaders (chat parity).
            for dr in display:
                dr['_q'] = q
            all_display.extend(display)
            if diag:
                last_diag = diag
            # engine_breakdown only renders for a single-query search (chat
            # behaviour); a batch flattens results so a per-query breakdown
            # has no place to attach.
            if single and engine_breakdown:
                engine_breakdown_out = engine_breakdown
            v_payload = backend.vertical_to_sse_payload(vertical_result)
            if v_payload:
                v_payload = dict(v_payload)
                v_payload['query'] = q
                all_verticals.append(v_payload)
            if not single:
                all_formatted.append(f'=== Search: {q} ===\n{formatted}')
            else:
                all_formatted.append(formatted)

        tool_content = '\n\n'.join(all_formatted)
        # Only propagate diag if we ended up with 0 display results
        final_diag = last_diag if not all_display else None
        return (tool_content, all_display, final_diag,
                engine_breakdown_out, all_verticals or None)

    elif name == 'fetch_url':
        try:
            backend = search_backend or load_paper_search_backend()
        except Exception as exc:
            logger.error('[Paper:Report:Tool] fetch backend unavailable: %s', exc)
            return f'Error: fetch backend unavailable: {exc}', [], None, None, None
        urls = args.get('urls', [])
        # Defensive: a non-list ``urls`` (string that repair could not
        # normalize) becomes a single entry — never iterated per-character.
        if urls and not isinstance(urls, list):
            urls = [urls]
        if not urls:
            u = args.get('url', '')
            if u:
                urls = [{'url': u}]
        if not urls:
            return 'Error: no url provided', [], None, None, None

        url_list = []
        for uobj in urls[:5]:
            if isinstance(uobj, dict):
                u = uobj.get('url', '')
            elif isinstance(uobj, str):
                u = uobj
            else:
                continue
            if u and u.strip():
                url_list.append(u.strip())
        if not url_list:
            return 'Error: no valid url provided', [], None, None, None

        def _fetch_one(u):
            logger.info('[Paper:Report:Tool] fetch_url url=%.100s', u)
            # Reuse chat's helper so binary-asset staging, content filtering,
            # rejected-scheme handling and filtered-vs-raw char counts all
            # match chat exactly.
            return backend.fetch_url_one(u, user_question, fetch_reason='')

        ordered = run_batch_concurrent(url_list, _fetch_one, max_workers=3, tag='PaperFetch', abort=abort)

        all_parts = []
        all_display = []
        for idx, item in enumerate(ordered):
            u = url_list[idx]
            if item is None:
                # Synthesize a failure item so display/text stay aligned —
                # same shape chat's batch handler uses.
                item = {
                    'url': u, 'page_content': None, 'is_pdf': False,
                    'raw_chars': 0, 'filtered_chars': 0,
                    'error_msg': 'internal fetch error (see logs)',
                }
            all_display.append(backend.format_fetch_display(item, _short_url))
            page_content = item['page_content']
            filtered_chars = item['filtered_chars']
            error_msg = item.get('error_msg')
            if page_content:
                all_parts.append(
                    f"Content from {u} ({filtered_chars:,} chars):\n\n{page_content}")
            else:
                all_parts.append(
                    f"Failed to fetch {u}." + (f' ({error_msg})' if error_msg else ''))
        return '\n\n---\n\n'.join(all_parts), all_display, None, None, None

    else:
        # ── Full-set branch: every non-search tool goes through chat's SHARED
        #    dispatch — never grow parallel per-tool branches here. Engines
        #    without a shim (research-only set) keep the legacy Unknown-tool
        #    reply so a hallucinated name stops at the adapter.
        if exec_shim is None:
            return f'Unknown tool: {name}', [], None, None, None
        return _execute_shared_tool(name, args, exec_shim, round_entry, abort)



def make_research_tool_executor(messages, *, user_question, abort_signal,
                                result_budget,
                                paper_tool_executor=None,
                                on_tool_event=None, log_prefix='[Paper]',
                                force_vertical=None,
                                contract_documents_for_round=None,
                                exec_shim=None):
    """Build the ``run_agent_loop`` ``execute_tool(rnd, tc)`` closure shared by
    the paper insight + recommend research agents.

    The two engines' per-tool-call handling was line-identical except for three
    axes — the log prefix, whether web_search is forced onto a vertical, and
    which ``execute_paper_tool`` binding is used — so all three are
    parameters here. The closure:
      1. parse+schema-repairs the tool args (``parse_and_repair_tool_args``),
      2. fires an ``on_tool_event`` ``tool_start`` (round-numbered),
      3. runs ``paper_tool_executor`` (passing ``force_vertical`` through),
      4. fires ``tool_done`` with results + optional engineBreakdown/verticals,
      5. appends the bounded V2 ``role:'tool'`` message to ``messages``.

    ``paper_tool_executor`` is an explicit dependency injection point for an
    engine-specific executor; it defaults to this module's
    ``execute_paper_tool``.
    ``messages`` is mutated in place (the loop appends the tool turn), matching
    the former inline closures exactly. A private per-executor round counter
    numbers the tool-events independently of the loop's round index (parity
    with the original ``_round_counter`` closure state).
    """
    _exec_report = paper_tool_executor or execute_paper_tool
    _round_counter = {'n': 0}

    def _execute_tool(rnd, tc):
        fn_name = tc['function']['name']
        fn_args_raw = tc['function']['arguments']
        tc_id = tc.get('id', '')
        # Parse + schema-repair once so the display label and the executor see
        # the same normalized shape (a bare-string queries/urls → array).
        fn_args, _ = parse_and_repair_tool_args(fn_name, fn_args_raw)
        documents = None
        if contract_documents_for_round is not None:
            documents = contract_documents_for_round(rnd)
            # A requested v2 epoch that is unexpectedly absent is an empty
            # authority map, never a silent downgrade to legacy ``None``.
            if documents is None:
                documents = {}
        fn_args, contract_error, rejection = _validate_paper_tool_arguments(
            fn_name, fn_args, documents)
        _round_counter['n'] += 1
        rn = _round_counter['n']
        display_query = display_query_for(fn_name, fn_args)
        round_entry = {
            'roundNum': rn, 'llmRound': rnd,
            'toolName': fn_name, 'query': display_query,
            'toolCallId': tc_id, 'status': 'searching', 'results': None,
        }

        if on_tool_event:
            on_tool_event({
                'type': 'tool_start', 'roundNum': rn, 'toolName': fn_name,
                'query': display_query, 'toolCallId': tc_id,
            })

        import time as _time
        tool_t0 = _time.time()
        if contract_error is not None:
            # The model sees the stable retry hint, but no network/tool backend
            # is touched and the event remains ``rejected`` rather than done.
            result = rejection
            display_results, search_diag = [], None
            engine_breakdown, verticals = None, None
            round_entry['status'] = 'rejected'
            round_entry['contractError'] = contract_error
        else:
            # Execute the validated/defaulted value, not the pre-contract raw
            # JSON. Keep the facade call signature compatible with engine
            # monkeypatch seams by enforcing the contract in this closure.
            normalized_args = json.dumps(fn_args, ensure_ascii=False)
            _extra = ({'force_vertical': force_vertical}
                      if force_vertical else {})
            # Research epochs expose only search/fetch plus owner-scoped result
            # continuation.  Continuation must use the same shared dispatcher
            # as chat so ownership and typed errors remain authoritative.
            if exec_shim is not None and fn_name not in {
                    'web_search', 'fetch_url'}:
                _extra.update({
                    'exec_shim': exec_shim,
                    'round_entry': round_entry,
                    'contract_documents_by_name': documents,
                })
            (result, display_results, search_diag,
             engine_breakdown, verticals) = _exec_report(
                fn_name, normalized_args, user_question=user_question,
                abort=abort_signal.is_set, **_extra)
            if round_entry.get('status') != 'rejected':
                round_entry['status'] = 'done'
            round_entry['results'] = display_results
        tool_elapsed = _time.time() - tool_t0
        logger.info('%s:Tool %s → %d chars in %.1fs',
                    log_prefix, fn_name, len(result), tool_elapsed)

        if on_tool_event:
            done_ev = {
                'type': 'tool_done', 'roundNum': rn, 'toolName': fn_name,
                'toolCallId': tc_id, 'elapsed': round(tool_elapsed, 1),
                'results': display_results,
                'status': ('rejected' if round_entry.get('status') == 'rejected'
                           else 'done'),
            }
            if contract_error is not None:
                done_ev['contractError'] = contract_error
                done_ev['toolContent'] = result[:4000]
            if engine_breakdown:
                done_ev['engineBreakdown'] = engine_breakdown
            if verticals:
                done_ev['verticals'] = verticals
            on_tool_event(done_ev)

        visible_result = result_budget.append(
            messages, round_index=rnd, tool_name=fn_name,
            tool_call_id=tc_id, content=result, round_entry=round_entry,
            tool_arguments=fn_args)
        return visible_result

    return _execute_tool
