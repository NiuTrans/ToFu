"""Closed policy inventory for automatic conversational context.

Entry points: ``context_block`` for snapshots; ``injection_spec`` for runtime
carriers. Source domains acquire evidence and decide their documented trigger;
this registry alone owns placement, authority, budgets and necessity metadata.
Execution permissions, inbox acknowledgements and compaction are NOT owned here.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from ._models import ContextBlock

# Closed presentation tones for a model-visible intervention's title chip.
# The frontend chip styles derive from these exact values; keep in sync with
# `.sw-note-reason-{tone}` (05-paper-report-media.css). 'info' is the neutral
# default for unknown/new reasons so a newer backend never renders blank.
NOTE_TONES = ('ok', 'warn', 'err', 'info')
NoteTone = Literal['ok', 'warn', 'err', 'info']


@dataclass(frozen=True)
class NotePresentation:
    """Human-facing rendering contract for one intervention reason.

    This is the single source of truth for how a system note's cause reads in
    the conversation timeline. The frontend chip map is GENERATED from these
    declarations (scripts/gen_system_note_presentation.mjs); it must never be
    hand-duplicated. ``label_key`` is the i18n contract key; ``tone`` encodes
    the category so the chip colour is scannable at a glance.
    """
    label_key: str
    tone: NoteTone = 'info'
    fallback: str = ''


@dataclass(frozen=True)
class InjectionSpec:
    id: str
    source: str
    necessity: str
    trigger: str
    lane: Literal['block', 'event', 'inbox', 'bootstrap', 'telemetry'] = 'block'
    authority: str = 'workflow'
    stability: str = 'turn'
    lifecycle: str = 'task'
    priority: int = 10
    max_tokens: int | None = None
    layer: str = 'hot_tail'
    required: bool = False
    global_budget_policy: Literal['fit', 'omit'] = 'fit'
    note_kind: str = ''
    placement: Literal['tail', 'system'] = 'tail'
    presentation: NotePresentation | None = None

    # Snapshot witnesses fold unless they are first-class interventions.
    group_context: bool = True
    provenance_field: str = ''

    # Acquisition absence is not invalidation of historical evidence.
    # These reasons leave only a receipt, never a model-visible tombstone.
    silent_absence_reasons: tuple[str, ...] = ()

_SPECS = (
    InjectionSpec('platform_static', 'system_prompt_base', 'required_contract',
                  'Platform contract unless operator supplied replace-mode system',
                  authority='platform', stability='static', lifecycle='conversation',
                  priority=0, layer='objective_constraints', required=True,
                  placement='system'),
    InjectionSpec('role_directive', 'endpoint.role', 'required_contract',
                  'Explicit specialized role', stability='round', lifecycle='round',
                  priority=0, max_tokens=5000, layer='objective_constraints', required=True),
    InjectionSpec('goal_objective', 'goal_runs.worker_context', 'required_contract',
                  'Chat Goal worker with a stamped durable objective',
                  stability='round', lifecycle='round',
                  priority=1, max_tokens=8000, layer='objective_constraints',
                  required=True),
    InjectionSpec('project_rules', 'project.AGENTS_CLAUDE', 'required_contract',
                  'Attached enabled project; rules only, history is on demand',
                  authority='project', stability='conversation', lifecycle='conversation',
                  priority=0, layer='objective_constraints', required=True),
    InjectionSpec('user_context', 'memory.user_context', 'user_preference',
                  'Preferences enabled and nonempty', authority='preference',
                  stability='conversation', lifecycle='conversation', priority=20,
                  max_tokens=1000, layer='objective_constraints',
                  provenance_field='preferencesApplied'),
    InjectionSpec('memory_guidance', 'memory', 'capability_guidance',
                  'Memory enabled and real tools available', authority='ambient',
                  stability='conversation', lifecycle='conversation', priority=60,
                  max_tokens=180, layer='cold_history'),
    InjectionSpec('skills_index', 'skills.registry', 'capability_discovery',
                  'Real tools and enabled installed skills; frozen per task',
                  stability='conversation', lifecycle='conversation', priority=40,
                  max_tokens=1400, layer='cold_history'),
    InjectionSpec('ui_nav_directory', 'ui.nav_targets', 'capability_discovery',
                  'Browser-frontend conversation turn; static contract-generated '
                  'directory of setting/page jump keys',
                  stability='static', lifecycle='conversation', priority=45,
                  max_tokens=1400, layer='cold_history', placement='system'),
    InjectionSpec('selected_skills', 'skills.activation', 'required_contract',
                  'Exact skill mentions in the current user request',
                  authority='workflow', stability='turn', lifecycle='task',
                  priority=15, max_tokens=8000, layer='objective_constraints',
                  required=True),
    InjectionSpec('credential_vault', 'credentials_vault', 'capability_discovery',
                  'Real tools and nonempty name-only vault index', authority='user',
                  stability='conversation', lifecycle='conversation', priority=30,
                  max_tokens=1000, layer='cold_history'),
    InjectionSpec('parallel_execution', 'swarm', 'capability_guidance',
                  'Enabled available multi-agent lane and independent task shape',
                  stability='static', lifecycle='conversation', priority=50,
                  max_tokens=128, layer='cold_history'),
    InjectionSpec('programmatic_execution', 'tools.programmatic', 'capability_guidance',
                  'Enabled programmatic tool lane', stability='static', lifecycle='conversation',
                  priority=55, max_tokens=128, layer='cold_history'),
    InjectionSpec('programmatic_inputs', 'tools.program_inputs', 'capability_discovery',
                  'Explicit context.programmaticInputs experiment, owner-scoped inputs and program action',
                  authority='ambient', stability='round', lifecycle='round',
                  priority=45, max_tokens=256, layer='hot_tail',
                  global_budget_policy='omit'),
    InjectionSpec('project_context', 'project.brain', 'coordination_evidence',
                  'Available owner/project-bound coordinator with unseen narrative; '
                  'ordinary workers use scoped inbox and overlap advisories',
                  authority='project', max_tokens=1800, layer='evidence',
                  global_budget_policy='omit',
                  silent_absence_reasons=('empty', 'project_disabled', 'disabled',
                                          'provider_unavailable', 'over_block_budget')),
    InjectionSpec('active_checklist', 'task.todo_state', 'required_continuity',
                  'Accepted current checklist missing from visible context',
                  priority=5, layer='task_state', required=True,
                  group_context=False,
                  presentation=NotePresentation('systemNote.reasonActiveChecklist', 'ok', 'Active checklist')),
    InjectionSpec('task_state_v1', 'task.events.projection', 'required_continuity',
                  'Explicit global context plan enabled', priority=5, max_tokens=2400,
                  layer='task_state', required=True),
    InjectionSpec('relevant_memories', 'memory.prefetch', 'retrieved_evidence',
                  'High-confidence owner-scoped prefetch matches', authority='evidence',
                  priority=70, max_tokens=1500, layer='evidence',
                  provenance_field='memoryPrefetch',
                  silent_absence_reasons=('no_high_confidence_matches',)),
    InjectionSpec('plan_mode', 'plan_mode.contract', 'required_contract',
                  'Plan mode explicitly enabled', priority=15, max_tokens=900,
                  layer='objective_constraints', required=True),
    InjectionSpec('environment', 'platform.environment', 'required_routing',
                  'Current execution environment and owner-scoped browser capability unless explicitly disabled',
                  authority='platform', priority=80, max_tokens=400, required=True),
    InjectionSpec('project_path_change', 'platform.environment.transition', 'required_routing',
                  'Observed path transition, never first sight or steady state',
                  authority='platform', priority=79, max_tokens=160, required=True,
                  provenance_field='projectPathChange'),
    InjectionSpec('mcp_tools_delta', 'mcp.catalog', 'capability_discovery',
                  'Reachable MCP names absent from frozen wire and schema discovery available',
                  priority=40, max_tokens=700, layer='evidence',
                  provenance_field='mcpToolsDelta'),
    InjectionSpec('disclosed_tools', 'tools.disclosure', 'capability_recovery',
                  'Previously disclosed deferred names remain executable',
                  priority=40, max_tokens=700, layer='evidence'),
    InjectionSpec('current_date', 'clock', 'ambient_fact',
                  'Current day absent from trusted model-visible context or changed',
                  authority='ambient', priority=90, max_tokens=80),
    InjectionSpec('modified_files', 'orchestrator.attachments', 'state_reminder',
                  'Journal shows files changed outside this conversation since turn start; '
                  'file-set signature changed', authority='ambient',
                  stability='round', lifecycle='round', max_tokens=1600,
                  group_context=False,
                  presentation=NotePresentation('systemNote.reasonModifiedFiles', 'warn', 'Modified files')),
    InjectionSpec('tool_progress', 'orchestrator.tool_loop_breaker', 'safety_recovery',
                  'Verified identical outcome, explicit no-op, redundant read or unresolved artifact',
                  lane='event', required=True, note_kind='system-intervention',
                  presentation=NotePresentation('systemNote.reasonToolProgress', 'warn', 'No progress')),
    InjectionSpec('execution_allowance', 'agent_core.execution_budget', 'required_contract',
                  'Finite invocation allowance at first safe dispatch, lower-water '
                  'milestones or observed unknown consumption',
                  lane='event', required=True, stability='round', lifecycle='task',
                  priority=5, max_tokens=350, note_kind='context-injection',
                  presentation=NotePresentation('systemNote.reasonExecutionAllowance', 'info', 'Execution budget')),
    InjectionSpec('tool_stagnation', 'orchestrator.tool_loop_breaker', 'safety_recovery',
                  'Persistent identical failure or identical successful polling; bounded correction',
                  lane='event', required=True, note_kind='system-intervention',
                  presentation=NotePresentation('systemNote.reasonToolStagnation', 'err', 'Stagnation')),
    InjectionSpec('tool_repeat_reminder', 'orchestrator.tool_loop_breaker', 'safety_recovery',
                  'Early consecutive exact-repeat notice before the corrective nudge; never vetoes',
                  lane='event', required=True, note_kind='system-intervention',
                  presentation=NotePresentation('systemNote.reasonToolRepeat', 'warn', 'Repeated call')),
    InjectionSpec('partial_stream_retry', 'stream_handler.analyse', 'protocol_recovery',
                  'Partial transport cut on a provider without assistant prefill',
                  lane='event', required=True, note_kind='system-intervention',
                  presentation=NotePresentation('systemNote.reasonStreamRetry', 'info', 'Stream retry')),
    InjectionSpec('terminal_recovery', 'stream_handler.terminal_recovery', 'protocol_recovery',
                  'Empty terminal response or unexecuted tool packet; at most two retries',
                  lane='event', required=True, note_kind='system-intervention',
                  presentation=NotePresentation('systemNote.reasonTerminalRecovery', 'info', 'Terminal recovery')),
    InjectionSpec('intermediate_response', 'stream_handler.analyse', 'protocol_recovery',
                  'Provider explicitly marks a commentary-only response; bounded retries',
                  lane='event', required=True, note_kind='system-intervention',
                  presentation=NotePresentation('systemNote.reasonIntermediate', 'info', 'Intermediate reply')),
    InjectionSpec('todo_incomplete', 'stream_handler.analyse', 'required_continuity',
                  'Historical checklist-continuation receipts only; no new execution emits this',
                  lane='event', required=True, note_kind='todo-continuation',
                  presentation=NotePresentation('systemNote.reasonTodoIncomplete', 'ok', 'Checklist continuation')),
    InjectionSpec('intent_stall', 'stream_handler.intent_stall', 'safety_recovery',
                  'Retryable tool failure followed by prose-only stop, no human handoff; once per task',
                  lane='event', required=True, note_kind='intent-stall',
                  presentation=NotePresentation('systemNote.reasonIntentStall', 'warn', 'Stalled intent')),
    InjectionSpec('fallback_synthesis', 'orchestrator.finalize', 'result_recovery',
                  'Fetched evidence but no final answer', lane='event', authority='evidence', required=True,
                  presentation=NotePresentation('systemNote.reasonFallback', 'info', 'Fallback synthesis')),
    InjectionSpec('evolution_guidance', 'evolution.guidance', 'retrieved_evidence',
                  'Owner-bound executor with learned strategies and current plan',
                  authority='evidence', max_tokens=800, layer='evidence'),
    InjectionSpec('evolution_updates', 'evolution.coordination', 'external_delivery',
                  'Live owner-bound goal updates at a closed tool boundary; explicit decisions remain separate',
                  lane='inbox', authority='evidence', required=True, note_kind='context-injection',
                  presentation=NotePresentation('systemNote.reasonEvolution', 'info', 'Goal update')),
    InjectionSpec('inbox_delivery', 'orchestrator.inbox', 'external_delivery',
                  'Pending swarm/peer/human/background/overlap payload at closed tool boundary; ACK stays with producer',
                  lane='inbox', authority='evidence', required=True),
    InjectionSpec('url_prefetch', 'message_builder.prefetch', 'retrieved_evidence',
                  'Successful prefetch of user-provided URL', lane='event', authority='evidence',
                  note_kind='context-injection',
                  presentation=NotePresentation('systemNote.reasonUrlPrefetch', 'info', 'URL prefetch')),
    InjectionSpec('lifecycle_recovery', 'conv_message_builder.load', 'required_safety',
                  'Interrupted lifecycle command has unknown outcome; do not blindly refire',
                  lane='event', required=True, note_kind='context-injection',
                  presentation=NotePresentation('systemNote.reasonLifecycle', 'warn', 'Lifecycle recovery')),
    InjectionSpec('branch_context', 'conv_message_builder.load', 'user_projection',
                  'Explicit branch title/selection', lane='event', authority='user', required=True,
                  note_kind='context-injection',
                  presentation=NotePresentation('systemNote.reasonBranch', 'info', 'Branch context')),
    InjectionSpec('paper_tool_guidance', 'paper.tools', 'capability_guidance',
                  'Paper schema discovery and execution gateways both available', lane='event'),
    InjectionSpec('worker_policy', 'swarm.agent', 'required_contract',
                  'Scoped worker role/tool/read-only/shared-workspace policy before dialogue',
                  lane='bootstrap', authority='platform', required=True),
    InjectionSpec('worker_artifacts', 'swarm.agent', 'capability_discovery',
                  'Shared artifact store available; names only, bounded', lane='event', authority='evidence',
                  note_kind='context-injection',
                  presentation=NotePresentation('systemNote.reasonWorkerArtifacts', 'info', 'Worker artifacts')),
    InjectionSpec('worker_dependencies', 'swarm.scheduler', 'delegated_evidence',
                  'Explicit prerequisite completed', lane='event', authority='evidence', required=True,
                  note_kind='context-injection',
                  presentation=NotePresentation('systemNote.reasonWorkerDeps', 'info', 'Dependency ready')),
    InjectionSpec('worker_wrapup', 'swarm.agent', 'result_recovery',
                  'Worker halted without final answer; tool-less honest partial report', lane='event', required=True,
                  note_kind='context-injection',
                  presentation=NotePresentation('systemNote.reasonWorkerWrapup', 'warn', 'Worker wrap-up')),
    InjectionSpec('goal_worker_directive', 'goal_runs.worker_context', 'required_contract',
                  'Pending engine directive for the continuing Goal worker',
                  lane='event', required=True, note_kind='context-injection',
                  presentation=NotePresentation('systemNote.reasonGoalDirective', 'info', 'Goal directive')),
    InjectionSpec('goal_worker_review', 'goal_runs.worker_context', 'review_evidence',
                  'Completed virtual-user review, never new human authority',
                  lane='event', authority='evidence', required=True, note_kind='context-injection',
                  presentation=NotePresentation('systemNote.reasonGoalReview', 'info', 'Goal review')),
    InjectionSpec('virtual_user_directive', 'autopilot', 'required_contract',
                  'Explicit autopilot simulated-user turn; pinned objective and role only',
                  lane='event', required=True, note_kind='context-injection',
                  presentation=NotePresentation('systemNote.reasonVirtualUser', 'info', 'Virtual user')),
    InjectionSpec('context_budget', 'compaction.pipeline', 'telemetry_only',
                  'Working-message estimate crosses diagnostic threshold; compaction owns capacity', lane='telemetry'),
)
INJECTION_REGISTRY = MappingProxyType({spec.id: spec for spec in _SPECS})
if len(INJECTION_REGISTRY) != len(_SPECS):
    raise ValueError('Duplicate context injection policy ID')


def injection_spec(injection_id: str) -> InjectionSpec:
    """Unknown production IDs fail closed rather than silently bypass policy."""
    return INJECTION_REGISTRY[injection_id]


def context_block(injection_id: str, content: str, *, instance: str = '',
                  reason: str = '', provenance: dict | None = None,
                  observed_at_ms: int = 0, world_version: str = '',
                  recovery_handle: str = '') -> ContextBlock:
    spec = injection_spec(injection_id)
    if spec.lane != 'block':
        raise ValueError(f'{injection_id} is not a snapshot block')
    if instance and (len(instance) > 64 or not all(c.isalnum() or c in '_-' for c in instance)):
        raise ValueError('Invalid context instance identity')
    return ContextBlock(
        id=spec.id + ('_' + instance if instance else ''), source=spec.source,
        content=content or '', authority=spec.authority, placement=spec.placement,
        stability=spec.stability, lifecycle=spec.lifecycle, priority=spec.priority,
        max_tokens=spec.max_tokens, layer=spec.layer, required=spec.required,
        suppressed_reason=reason, provenance=provenance or {},
        observed_at_ms=observed_at_ms, world_version=world_version,
        recovery_handle=recovery_handle,
    )


def block_spec(block_id: str) -> InjectionSpec | None:
    """Resolve registered snapshot identities; custom renderer inputs stay pure."""
    spec = INJECTION_REGISTRY.get(block_id)
    if spec:
        return spec
    for prefix in ('project_path_change', 'modified_files'):
        if block_id.startswith(prefix + '_'):
            return INJECTION_REGISTRY[prefix]
    return None


def _validate_presentations() -> None:
    """Fail closed on a malformed presentation contract at import time."""
    for spec in _SPECS:
        presentation = spec.presentation
        if presentation is None:
            continue
        if presentation.tone not in NOTE_TONES:
            raise ValueError(
                f'{spec.id}: presentation tone {presentation.tone!r} not in {NOTE_TONES}')
        if not presentation.label_key.startswith('systemNote.reason'):
            raise ValueError(
                f'{spec.id}: presentation label_key {presentation.label_key!r} '
                'must be a systemNote.reason* i18n key')


_validate_presentations()


def note_presentations() -> dict[str, NotePresentation]:
    """Read-only reason-id -> presentation map for the contract generator.

    Only specs with a model-visible intervention reason carry one; snapshot
    blocks with no timeline note and telemetry stay absent (the frontend then
    falls back to its neutral unknown-reason chip). Keys are registry ids.
    """
    return {spec.id: spec.presentation for spec in _SPECS
            if spec.presentation is not None}


def note_display_policies() -> dict[str, dict]:
    """Display taxonomy, including legacy repair, derived from source lanes.

    A block is a context snapshot regardless of its necessity/authority;
    explicit recovery kinds and group opt-outs remain standalone. This is
    presentation only: never upgrades prompt or execution authority.
    """
    policies = {
        spec.id: {
            'noteKind': spec.note_kind or (
                'context-injection' if spec.lane == 'block' else 'system-intervention'),
            'groupContext': spec.group_context,
            'provenanceField': spec.provenance_field,
        }
        for spec in _SPECS if spec.lane not in {'telemetry', 'bootstrap'}
    }
    # The composer owns this aggregate witness, not a separate prompt source.
    policies['session_context_committed'] = {
        'noteKind': 'session-context', 'groupContext': True, 'provenanceField': '',
    }
    return policies
