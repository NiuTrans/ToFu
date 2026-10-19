"""Deterministic renderer for :class:`ContextBlock` objects."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.context_composer._sections import (
    _canonical_section,
    _strip_to_body,
)
from lib.tasks_pkg.context_composer._models import (
    ComposeRequest,
    ComposeResult,
    ContextBlock,
    ContextPlanEntryV2,
    ContextPlanV2,
)
from lib.tasks_pkg.context_composer.registry import INJECTION_REGISTRY

logger = get_logger(__name__)

# Registry-declared system-prefix blocks (currently platform_static and
# ui_nav_directory). Each occupies its own dedicated system carrier; only a
# system-role witness may satisfy their content-addressed reuse — a quoted
# envelope in a user/tool message is data, never installed policy.
_SYSTEM_PLACEMENT_IDS = frozenset(
    spec.id for spec in INJECTION_REGISTRY.values() if spec.placement == 'system')

# Authoritative absences that REMOVE a dedicated system carrier instead of
# retaining or tombstoning it: the engine policy is gone (replace mode), the
# block was explicitly ablated, or the surface no longer qualifies. Transient
# acquisition failure is never here — it retains the previous carrier.
_SYSTEM_WITHDRAWAL_REASONS = frozenset({
    'replace_mode', 'disabled', 'non_browser_surface',
})

_AUTHORITY_ORDER = {
    # Lower-authority evidence is rendered first; higher-authority contracts
    # are physically closer to the generation boundary within each placement.
    'evidence': 10,
    'ambient': 20,
    'preference': 30,
    'workflow': 40,
    'project': 50,
    'user': 60,
    'platform': 70,
}
_PLACEMENT_ORDER = {'system': 0, 'head': 1, 'tail': 2, 'tool_result': 3}
_LAYER_ORDER = {
    'objective_constraints': 0,
    'task_state': 1,
    'evidence': 2,
    'hot_tail': 3,
    'cold_history': 4,
}
_AUTHORITATIVE_RETRACTION_REASONS = frozenset({
    'disabled',
    'memory_disabled',
    'multi_agent_disabled',
    'no_delta',
    'no_disclosed_tools',
    'no_deferred_tools',
    # NOTE: 'discovery_unavailable' and 'capability_unavailable' are computed
    # situationally from the live wire/tool catalog. Withdrawing a snapshot on
    # either confuses transient unavailability with authoritative absence (the
    # docs name this a rejected alternative), so they fall through to
    # retained_after: retention instead of retracting.
    'no_enabled_skills',
    'no_evolution_binding',
    'no_selected_skills',
    'not_applicable',
    'not_a_goal_worker',
    'ordinary_agent_role',
    'plan_mode_off',
    'preferences_disabled',
    'profile_empty',
    'programmatic_disabled',
    'project_disabled',
    'project_empty',
    'skills_disabled',
    'load_skill_not_enabled',
    'run_command_not_enabled',
    'vault_empty',
    'vault_disabled',
})


def _count_tokens(text: str, model: str) -> int:
    try:
        from lib.token_counter import count_text
        return max(0, int(count_text(text, model=model)))
    except Exception as exc:
        logger.debug('[ContextComposer] token counter fallback: %s', exc)
        return max(1, (len(text) + 3) // 4) if text else 0


def _truncate(text: str, max_tokens: int | None, model: str) -> tuple[str, bool]:
    if max_tokens is None or int(max_tokens) <= 0 \
            or _count_tokens(text, model) <= int(max_tokens):
        return text, False
    # The block-local ceiling is a hard content-token limit. Use the same
    # provider-neutral counter as the manifest and bisect characters instead
    # of assuming a fixed chars/token ratio (which is unsafe for CJK/base64).
    limit = int(max_tokens)
    marker = '\n\n[context block truncated by budget]'
    suffix = marker if _count_tokens(marker, model) <= limit else ''
    low, high, best = 0, len(text), ''
    while low <= high:
        middle = (low + high) // 2
        candidate = text[:middle].rstrip() + suffix
        if _count_tokens(candidate, model) <= limit:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best, True


def _fit_global_budget(block: ContextBlock, text: str, budget: int,
                       model: str) -> tuple[str, bool] | None:
    """Fit one optional block into ``budget`` with deterministic bisection."""
    if budget <= 0:
        return None
    rendered = _envelope(block, text)
    if _count_tokens(rendered, model) <= budget:
        return text, False
    from .registry import block_spec
    spec = block_spec(block.id)
    if spec is not None and spec.global_budget_policy == 'omit':
        return None
    marker = '\n\n[context block truncated by global budget]'
    low, high = 0, len(text)
    best = ''
    while low <= high:
        middle = (low + high) // 2
        candidate = text[:middle].rstrip() + marker
        if _count_tokens(_envelope(block, candidate), model) <= budget:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    if not best or not text[:max(0, len(best) - len(marker))].strip():
        return None
    return best, True


def _hash_join(values: list[str]) -> str:
    return hashlib.sha256('\x00'.join(values).encode('utf-8')).hexdigest()[:24]


def _cache_epoch(request: ComposeRequest, tool_manifest_hash: str) -> int:
    task = request.task
    if task is None:
        return 0
    state = {
        'permissions': sorted(request.granted_permissions),
        'tools': sorted(request.tool_names),
        'toolManifestHash': tool_manifest_hash,
        'compactionBoundary': str(task.get('_compactionBoundary') or ''),
    }
    key = hashlib.sha256(json.dumps(
        state, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()[:24]
    previous = str(task.get('_contextCacheEpochKey') or '')
    epoch = max(0, int(task.get('_contextCacheEpoch') or 0))
    if previous and previous != key:
        epoch += 1
    task['_contextCacheEpochKey'] = key
    task['_contextCacheEpoch'] = epoch
    return epoch


def _apply_global_budget(manifest: list[dict[str, Any]],
                         request: ComposeRequest) -> ContextPlanV2:
    """Select optional candidates while never evicting required blocks."""
    budget = max(0, int(request.global_budget_tokens or 0))
    base = max(0, int(request.base_context_tokens or 0))
    available = max(0, budget - base)
    candidates = [row for row in manifest if row.get('_rendered')]
    required = [row for row in candidates
                if row['_block'].required or row.get('_required_override')]
    optional = [row for row in candidates
                if not row['_block'].required
                and not row.get('_required_override')]
    optional.sort(key=lambda row: (
        _LAYER_ORDER[row['_block'].layer],
        row['_block'].priority,
        -max(0, row['_block'].observed_at_ms),
        -max(0, row['_block'].access_count),
        row['tokens'],
        row['id'],
    ))

    selected_tokens = sum(row['tokens'] for row in required)
    remaining = max(0, available - selected_tokens)
    for row in optional:
        block = row['_block']
        if row['tokens'] <= remaining:
            selected_tokens += row['tokens']
            remaining -= row['tokens']
            continue
        fitted = _fit_global_budget(
            block, row['_raw_text'], remaining, request.model)
        if fitted is None:
            row['_rendered'] = ''
            row['injected'] = False
            row['chars'] = 0
            row['tokens'] = 0
            row['hash'] = hashlib.sha256(
                row['_raw_text'].encode('utf-8')).hexdigest()[:16]
            row['reason'] = 'global_budget_exhausted'
            continue
        fitted_text, global_truncated = fitted
        rendered = _envelope(block, fitted_text)
        tokens = _count_tokens(rendered, request.model)
        row['_rendered'] = rendered
        row['chars'] = len(rendered)
        row['tokens'] = tokens
        row['hash'] = hashlib.sha256(
            _canonical_section(block.id, fitted_text).encode('utf-8')
        ).hexdigest()[:16]
        if global_truncated:
            row['reason'] = 'global_budget_truncated'
            row['_global_truncated'] = True
        selected_tokens += tokens
        remaining = max(0, remaining - tokens)

    tool_manifest = ((request.task or {}).get('_tool_schema')
                     if isinstance((request.task or {}).get('_tool_schema'), list)
                     else None)
    tool_manifest_values = ([json.dumps(
        tool_manifest, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str)]
        if tool_manifest is not None else sorted(request.tool_names))
    segment_values: dict[str, list[str]] = {
        'staticPrefix': [], 'toolManifest': tool_manifest_values,
        'conversation': [], 'dynamicTail': [],
    }
    for row in manifest:
        if not (row.get('_rendered') or row.get('reused')):
            continue
        block = row['_block']
        segment = ('staticPrefix' if block.stability == 'static' else
                   'conversation' if block.stability == 'conversation' else
                   'dynamicTail')
        # Hash the content-addressed identity rather than only newly appended
        # bytes. Reusing an existing carrier must keep segment telemetry stable.
        segment_values[segment].append(str(row.get('hash') or ''))
    segment_hashes = {
        name: _hash_join(values) for name, values in segment_values.items()
    }
    entries = tuple(ContextPlanEntryV2(
        id=row['id'],
        layer=row['_block'].layer,
        selected=bool(row.get('injected') or row.get('reused')),
        truncated=bool(row.get('reason') in {
            'truncated', 'global_budget_truncated'}),
        reason=str(row.get('reason') or ''),
        tokens=max(0, int(row.get('tokens') or 0)),
        content_hash=str(row.get('hash') or hashlib.sha256(
            row['_raw_text'].encode('utf-8')).hexdigest()[:16]),
        recovery_handle=row['_block'].recovery_handle,
        observed_at_ms=max(0, row['_block'].observed_at_ms),
        world_version=row['_block'].world_version,
    ) for row in manifest)
    return ContextPlanV2(
        contract_version='tofu.context-plan/v2',
        budget_tokens=budget,
        base_tokens=base,
        selected_tokens=selected_tokens,
        overflow_tokens=max(0, base + selected_tokens - budget),
        entries=entries,
        segment_hashes=segment_hashes,
        cache_epoch=_cache_epoch(request, segment_hashes['toolManifest']),
    )


def _managed_block_hashes(
    messages: list[dict[str, Any]],
) -> dict[str, str]:
    """Return the latest content hash for each already-rendered block.

    Hash actual marker envelopes rather than trusting side metadata, which a
    compactor could retain after altering content. Only composer-owned
    messages are inspected.
    """
    latest: dict[str, str] = {}
    marker_pattern = re.compile(
        r'<!-- tofu-context:([^:\n]+):start -->.*?'
        r'<!-- tofu-context:\1:end -->',
        re.DOTALL,
    )
    for message in messages:
        if not isinstance(message, dict) or not message.get('_contextComposer'):
            continue
        content = message.get('content')
        parts = content if isinstance(content, list) else [content]
        for part in parts:
            text = (part.get('text') if isinstance(part, dict) else part)
            if not isinstance(text, str):
                continue
            for match in marker_pattern.finditer(text):
                block_id = match.group(1).strip()
                # A tail quote/unverified legacy span cannot witness that a
                # required native policy prefix is already installed.
                if (block_id in _SYSTEM_PLACEMENT_IDS
                        and message.get('role') != 'system'):
                    continue
                # Hash the wrapper-independent body section so a block hashes
                # identically whether it rode a standalone reminder envelope
                # or an amalgamated same-authority wrapper on-wire.
                latest[block_id] = hashlib.sha256(
                    _canonical_section(block_id, _strip_to_body(match.group(0)))
                    .encode('utf-8')).hexdigest()[:16]
    return latest


def verified_block_hash(messages, block_id, *, wire=False, existing_hashes=None):
    """Read actual envelope bytes; final wire bodies no longer carry private flags.

    ``existing_hashes`` may carry the map the composition render pass already
    computed for THIS SAME list (same identity, unchanged length): the caller
    owns that proof, and the block lookup then skips a second full regex scan.
    Any other input — a foreign list, an unknown map, or a grown/shrunk
    history — falls back to measuring the actual envelopes here.
    """
    if not isinstance(messages, list):
        return None
    if existing_hashes is not None and not wire:
        if isinstance(existing_hashes, dict):
            return existing_hashes.get(block_id)
    if wire:
        messages = [{**message, '_contextComposer': True} for message in messages if isinstance(message, dict)]
    return _managed_block_hashes(messages).get(block_id)


def _envelope(block: ContextBlock, text: str) -> str:
    if block.id == 'project_context':
        # Project state is neutral request data.  It must remain a final
        # user-role suffix and must never masquerade as a system reminder.
        return (
            f'<!-- tofu-context:{block.id}:start -->\n'
            f'{text}\n'
            f'<!-- tofu-context:{block.id}:end -->'
        )
    return (
        f'<!-- tofu-context:{block.id}:start -->\n'
        '<system-reminder>\n'
        f'[Context authority: {block.authority}; source: {block.source}]\n'
        f'{text}\n'
        '</system-reminder>\n'
        f'<!-- tofu-context:{block.id}:end -->'
    )


def _amalgamate_tail(texts: list[str]) -> list[str]:
    """Merge adjacent same-authority reminder envelopes into one wrapper.

    ``_envelope`` wraps every block in its own ``<system-reminder>`` with a
    repeated ``[Context authority: …]`` line.  A session start appends ~8 tail
    blocks, so the model reads that boilerplate once per block.  Blocks are
    already sorted by ``(authority, priority, id)``, so same-authority blocks
    are adjacent: fold each maximal run into ONE ``<system-reminder>`` whose
    sections keep their own ``tofu-context:<id>`` markers.

    Content-addressed reuse is untouched: ``_managed_block_hashes`` scans each
    ``tofu-context:<id>`` span wherever it appears, so a changed block re-emits
    its group's amalgam while unchanged sibling sections are reused verbatim.
    Runs of length 1 pass through byte-identical (no churn for lone blocks).
    """
    # (authority, source, block_id, body); non-matching texts pass through.
    marker = re.compile(
        r'<!-- tofu-context:([^:\n]+):start -->\n'
        r'<system-reminder>\n'
        r'\[Context authority: ([^;\]]+); source: ([^\]]+)\]\n'
        r'(.*?)\n'
        r'</system-reminder>\n'
        r'<!-- tofu-context:\1:end -->',
        re.DOTALL)
    parts: list[tuple[str, str, str, str] | str] = []
    for text in texts:
        match = marker.fullmatch(text.strip())
        parts.append((match.group(2), match.group(3), match.group(1),
                      match.group(4)) if match else text)
    out: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if not isinstance(part, tuple):
            out.append(part)
            index += 1
            continue
        authority = part[0]
        run = [part]
        index += 1
        while index < len(parts) and isinstance(parts[index], tuple) \
                and parts[index][0] == authority:
            run.append(parts[index])
            index += 1
        if len(run) == 1:
            _auth, source, block_id, body = run[0]
            out.append(
                f'<!-- tofu-context:{block_id}:start -->\n'
                '<system-reminder>\n'
                f'[Context authority: {authority}; source: {source}]\n'
                f'{body}\n'
                '</system-reminder>\n'
                f'<!-- tofu-context:{block_id}:end -->')
            continue
        sections = '\n'.join(
            f'<!-- tofu-context:{block_id}:start -->\n'
            f'[source: {source}]\n'
            f'{body}\n'
            f'<!-- tofu-context:{block_id}:end -->'
            for _auth, source, block_id, body in run)
        out.append(
            '<system-reminder>\n'
            f'[Context authority: {authority}; {len(run)} context blocks]\n'
            f'{sections}\n'
            '</system-reminder>')
    return out


def _unwrap_reminder(text: str) -> str:
    """Avoid nested ``<system-reminder>`` wrappers from legacy providers."""
    stripped = text.strip()
    start = '<system-reminder>'
    end = '</system-reminder>'
    if stripped.startswith(start) and stripped.endswith(end):
        return stripped[len(start):-len(end)].strip()
    return stripped


def _emit_context_summary(request: ComposeRequest, names: str,
                          total: int) -> None:
    """Best-effort instrumentation; logging must never block a model turn."""
    try:
        from lib.tasks_pkg.context_composer import _round_num
        round_num = _round_num(request.task or {})
        logger.info('[Context] conv=%s round=%d blocks=[%s] total=%d',
                    (request.conv_id or '?')[:8], round_num, names, total)
    except Exception:
        # This handler guards the logging backend itself. Logging here could
        # recurse into the same failure and must never break prompt assembly.
        return


def _append_tail(messages: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[str]:
    """Append the tail carrier; return the ACTUAL emitted texts.

    Same-authority runs fold into one amalgamated reminder, so the emitted
    texts can be fewer (and smaller) than the rows' standalone envelopes.
    Callers measuring assembled byte growth must use the returned texts, not
    the pre-fold per-row ``chars``.
    """
    if not rows:
        return []
    # Recovery snapshots have a replaceable lifecycle, unlike append-only
    # evidence siblings. Isolate their carrier so withdrawal never parses todo
    # text for delimiters or touches another context block.
    checklist = [row for row in rows if row['id'] == 'active_checklist']
    if checklist and len(rows) > len(checklist):
        return (_append_tail(messages, [row for row in rows if row['id'] != 'active_checklist'])
                + _append_tail(messages, checklist))
    texts = _amalgamate_tail([row['_rendered'] for row in rows])
    messages.append({
        'role': 'user',
        'carrier': 'tail_context',
        'content': [
            {'type': 'text', 'text': text}
            for text in texts
        ],
        '_isMeta': True,
        '_contextComposer': True,
        '_contextBlocks': [
            {
                'id': row['id'],
                'hash': row['hash'],
                'stability': row['stability'],
                'lifecycle': row['lifecycle'],
            }
            for row in rows
        ],
    })
    return texts


def render_context(messages: list[dict[str, Any]], blocks: list[ContextBlock],
                   request: ComposeRequest, *,
                   replace_managed: bool = True) -> ComposeResult:
    """Compose trusted static policy and content-addressed runtime context.

    Runtime blocks append only missing/changed versions; ``replace_managed``
    remains a compatibility argument, not deletion authority. Registry-declared
    system policy alone has a verified legacy migration and dedicated-prefix
    replacement boundary; user-authored policy and quoted data are unchanged.
    """
    del replace_managed
    from ._policy_prefix import (migrate_policy_prefix, is_static_policy,
                                 policy_carrier)
    migrate_policy_prefix(messages)
    existing_hashes = _managed_block_hashes(messages)
    from dataclasses import replace
    from .registry import block_spec
    # One optional-context switch by registry ID. Required policy and
    # continuity cannot be turned off through an advisory ablation.
    blocks = [replace(block, content='', suppressed_reason='disabled')
              if (spec := block_spec(block.id)) is not None
              and not spec.required and spec.id in request.disabled_blocks
              else block for block in blocks]
    manifest: list[dict[str, Any]] = []
    winners: dict[str, ContextBlock] = {}
    ordered = sorted(
        blocks,
        key=lambda b: (
            _PLACEMENT_ORDER[b.placement], _AUTHORITY_ORDER[b.authority],
            b.priority, b.id,
        ),
    )
    for block in ordered:
        key = block.dedupe_key or block.id
        if key in winners:
            manifest.append({
                'id': block.id, 'source': block.source,
                'authority': block.authority, 'placement': block.placement,
                'stability': block.stability, 'lifecycle': block.lifecycle,
                'injected': False, 'chars': 0, 'tokens': 0,
                'reason': f'duplicate_of:{winners[key].id}',
                'provenance': dict(block.provenance),
                '_block': block, '_raw_text': _unwrap_reminder(block.content),
            })
            continue
        winners[key] = block
        missing_permissions = (
            block.required_permissions - request.granted_permissions)
        if missing_permissions:
            manifest.append({
                'id': block.id, 'source': block.source,
                'authority': block.authority, 'placement': block.placement,
                'stability': block.stability, 'lifecycle': block.lifecycle,
                'injected': False, 'chars': 0, 'tokens': 0,
                'reason': 'permission_denied:' + ','.join(
                    sorted(missing_permissions)),
                'provenance': dict(block.provenance),
                '_block': block, '_raw_text': _unwrap_reminder(block.content),
            })
            continue
        if block.suppressed_reason or not block.content.strip():
            manifest.append({
                'id': block.id, 'source': block.source,
                'authority': block.authority, 'placement': block.placement,
                'stability': block.stability, 'lifecycle': block.lifecycle,
                'injected': False, 'chars': 0, 'tokens': 0,
                'reason': block.suppressed_reason or 'empty',
                'provenance': dict(block.provenance),
                '_block': block, '_raw_text': _unwrap_reminder(block.content),
            })
            continue
        text, truncated = _truncate(_unwrap_reminder(block.content), block.max_tokens,
                                    request.model)

        if truncated and block.id == 'project_context':
            # A narrative cursor covers the whole page, never a clipped prefix.
            # Omit it instead of letting a reused truncated hash become eligible
            # for ACK on the next attempt after its reason becomes already_present.
            manifest.append({
                'id': block.id, 'source': block.source,
                'authority': block.authority, 'placement': block.placement,
                'stability': block.stability, 'lifecycle': block.lifecycle,
                'injected': False, 'appended': False, 'chars': 0, 'tokens': 0,
                'reason': 'over_block_budget', 'provenance': dict(block.provenance),
                '_block': block, '_raw_text': '',
            })
            continue
        rendered = _envelope(block, text)
        block_tokens = _count_tokens(rendered, request.model)
        manifest.append({
            'id': block.id, 'source': block.source,
            'authority': block.authority, 'placement': block.placement,
            'stability': block.stability, 'lifecycle': block.lifecycle,
            'injected': True, 'chars': len(rendered), 'tokens': block_tokens,
            'hash': hashlib.sha256(
                _canonical_section(block.id, text).encode('utf-8')
            ).hexdigest()[:16],
            'reason': 'truncated' if truncated else '',
            'provenance': dict(block.provenance),
            '_block': block, '_raw_text': text,
        })
        manifest[-1]['_rendered'] = rendered

    # Only a successful authoritative absence or explicit state transition
    # withdraws earlier context. Transient/ambiguous absence retains the last
    # known block and its failure receipt, never masquerading as current empty.
    for row in manifest:
        if row.get('_rendered') or row['id'] not in existing_hashes:
            continue
        reason = str(row.get('reason') or '')

        spec = block_spec(row['id'])
        if spec is not None and reason in spec.silent_absence_reasons:
            # No current match/page is not proof that prior evidence is false.
            # Keep immutable history; this acquisition contributes zero bytes.
            continue
        if (is_static_policy(row['_block'])
                and reason in _SYSTEM_WITHDRAWAL_REASONS):
            # An authoritative absence withdraws the dedicated system carrier
            # outright; the remaining prefix bytes stay untouched, so a
            # surface/profile change never rewrites sibling policy.
            messages[:] = [m for m in messages if not (
                m.get('role') == 'system' and m.get('_contextComposer')
                and any(b.get('id') == row['id'] for b in m.get('_contextBlocks', [])))]
            existing_hashes.pop(row['id'], None)
            continue
        retract = (
            reason in _AUTHORITATIVE_RETRACTION_REASONS
            or reason.startswith('permission_denied:')
        )
        if retract:
            text = (
                f'[Context update: {row["id"]} is no longer active; '
                'do not rely on its earlier version.]'
            )
            rendered = _envelope(row['_block'], text)
            row['_raw_text'] = text
            row['_rendered'] = rendered
            row['injected'] = True
            row['chars'] = len(rendered)
            row['tokens'] = _count_tokens(rendered, request.model)
            row['hash'] = hashlib.sha256(
                _canonical_section(row['id'], text).encode('utf-8')
            ).hexdigest()[:16]
            row['reason'] = f'retracted:{reason}'
            row['_required_override'] = True
            continue
        row['injected'] = True
        row['reused'] = True
        row['appended'] = False
        row['hash'] = existing_hashes[row['id']]
        row['reason'] = f'retained_after:{reason}'

    # The clock may already be supplied by trusted system/developer policy.
    # Check actual retained bytes, not task watermarks: compaction must be able
    # to reintroduce a missing fact, while user/tool quotes cannot suppress it.
    from ._clock import visible_clock_date
    visible_date = (visible_clock_date(messages) if any(
        row['id'] == 'current_date' and row.get('_rendered')
        for row in manifest) else '')
    for row in manifest:
        if (row['id'] == 'current_date' and row.get('_rendered')
                and visible_date == row['_raw_text']):
            row['_rendered'] = ''
            row['reused'] = True
            row['appended'] = False
            row['reason'] = 'already_present'

    # Content-addressed reuse happens before global budget selection. Existing
    # bytes are already included in ``base_context_tokens`` and must not spend
    # the incremental budget or be appended again.
    for row in manifest:
        rendered = row.get('_rendered') or ''
        if (rendered and row['id'] != 'current_date'
                and existing_hashes.get(row['id']) == row.get('hash')):
            row['_rendered'] = ''
            row['reused'] = True
            row['appended'] = False
            row['reason'] = 'already_present'

    # Keep cached history immutable, but make snapshot precedence visible to
    # the model. The notice is outside the hash identity for stable reuse.
    for row in manifest:
        if (row.get('_rendered') and existing_hashes.get(row['id'])
                and not is_static_policy(row['_block'])):
            row['_rendered'] = (
                f'[Context update: {row["id"]}] The following snapshot replaces '
                f'all earlier `{row["id"]}` snapshots. Earlier versions are '
                'historical only; use this version for the current task.\n'
                + row['_rendered'])
            row['chars'] = len(row['_rendered'])
            row['tokens'] = _count_tokens(row['_rendered'], request.model)
            # Contract corrections stay required. Project overview pages are
            # optional coordination evidence and may be omitted as a whole.
            if row['id'] != 'project_context':
                row['_required_override'] = True

    plan = (_apply_global_budget(manifest, request)
            if request.global_budget_tokens is not None else None)

    # Carrier kind is a stable structural identity for every block the
    # composer owns — fresh appends and content-addressed retentions alike —
    # so wire projections and the frontend can recognize a context carrier
    # after private fields (``_contextComposer``/``_isMeta``) are stripped.
    for row in manifest:
        if row.get('injected'):
            row['carrier'] = ('system_context'
                              if is_static_policy(row['_block'])
                              else 'tail_context')

    # Only registry-declared system-placement blocks get a system prefix,
    # each in its own dedicated carrier. A caller-supplied placement/authority
    # alone never promotes runtime data.
    policy_rows: list[dict[str, Any]] = []
    tail_rows: list[dict[str, Any]] = []
    for row in manifest:
        rendered = row.get('_rendered', '')
        if rendered:
            row['appended'] = True
            requested_placement = row.get('placement') or 'tail'
            if is_static_policy(row['_block']):
                row['placement'] = 'system'
                policy_rows.append(row)
                continue
            if requested_placement != 'tail':
                row['requestedPlacement'] = requested_placement
                row['placement'] = 'tail'
            previous_hash = existing_hashes.get(row['id'])
            if previous_hash and previous_hash != row.get('hash'):
                transition_reason = str(row.get('reason') or '')
                row['reason'] = f'supersedes:{previous_hash}' + (
                    f';{transition_reason}' if transition_reason else '')
            tail_rows.append(row)
    from .injections import can_append_injection
    if tail_rows and not can_append_injection(messages):
        # Snapshot acquisition has no destructive drain: leave it eligible for
        # the next closed boundary and record the reason instead of splitting
        # a protocol batch or pretending this context reached the model.
        for row in tail_rows:
            row.update(injected=False, appended=False, chars=0, tokens=0,
                       reason='deferred:open_tool_batch')
            row['_rendered'] = ''
        tail_rows = []
        plan = (_apply_global_budget(manifest, request)
                if request.global_budget_tokens is not None else None)
    if policy_rows:
        # Rebuild the dedicated system-prefix lane as one sorted unit: lift
        # every RETAINED engine-owned system carrier (fresh versions are
        # re-rendered below), then re-seat the current set in authority order
        # after operator/user policy. Retained carriers are re-inserted
        # byte-identical, so only a genuine membership/content change ever
        # moves prefix bytes, and user-authored system text (no composer
        # marker) is never touched.
        policy_ids = {row['id'] for row in policy_rows}
        retained_carriers: list[dict] = []
        kept: list[dict] = []
        for message in messages:
            carrier_ids = ({row.get('id') for row in message.get('_contextBlocks') or []}
                           if isinstance(message, dict) else set())
            if (message.get('role') == 'system'
                    and message.get('_contextComposer')
                    and carrier_ids
                    and carrier_ids <= _SYSTEM_PLACEMENT_IDS):
                if carrier_ids.isdisjoint(policy_ids):
                    retained_carriers.append(message)
                # else: superseded by this pass's fresh carrier — dropped.
            else:
                kept.append(message)
        messages[:] = kept

        def _policy_sort_key(authority: str, priority: int, row_id: str):
            return (-_AUTHORITY_ORDER[authority], priority, row_id)

        seated: list[tuple[tuple, dict]] = [
            (_policy_sort_key(row['authority'], row['_block'].priority, row['id']),
             policy_carrier([row]))
            for row in policy_rows
        ]
        for carrier in retained_carriers:
            keys = []
            for row_id in {row.get('id') for row in carrier.get('_contextBlocks') or []}:
                spec = block_spec(row_id)
                if spec is not None:
                    keys.append(_policy_sort_key(spec.authority, spec.priority,
                                                 spec.id))
            seated.append((min(keys) if keys else (0, 10, ''), carrier))
        seated.sort(key=lambda item: item[0])
        anchor = next((i for i, m in enumerate(messages)
                       if m.get('role') not in ('system', 'developer')),
                      len(messages))
        for offset, (_key, carrier) in enumerate(seated):
            messages.insert(anchor + offset, carrier)
    tail_texts = _append_tail(messages, tail_rows)
    # A just-created mid-turn handoff follows restoration, but never crosses
    # newer human steering or execution. Ordinary append-only reuse is unchanged.
    from lib.tasks_pkg.compaction._handoff import finalize_handoff_order
    finalize_handoff_order(messages)

    for row in manifest:
        spec = block_spec(row['id'])
        if spec is not None:
            row.update(registryId=spec.id, necessity=spec.necessity,
                       trigger=spec.trigger, required=spec.required,
                       maxTokens=spec.max_tokens, lane=spec.lane)
        # Preserve the human-readable model-visible text so a caller can emit
        # a display-only note witnessing this exact commit (segments bound its
        # length). Present only on rows whose bytes were appended this pass.
        if row.get('appended'):
            row['noteText'] = row.get('_raw_text', '')
        row['appendedTokens'] = row['tokens'] if row.get('appended') else 0
    for order, row in enumerate(manifest):
        row['order'] = order
        row.pop('_rendered', None)
        row.pop('_block', None)
        row.pop('_raw_text', None)
        row.pop('_global_truncated', None)
        row.pop('_required_override', None)
    # ``total`` is the REAL assembled byte growth of this pass: policy
    # carriers are single-block (``chars`` == wire bytes), while tail blocks
    # may fold into amalgamated same-authority reminders, so the actual
    # emitted tail texts are measured instead of the pre-fold standalone
    # envelopes. Reused (already-present) rows append nothing and count 0.
    total = (sum(row['chars'] for row in policy_rows)
             + sum(len(text) for text in tail_texts))
    names = ','.join(f"{row['id']}:{row['chars']}" for row in manifest
                     if row['injected'])
    _emit_context_summary(request, names, total)
    return ComposeResult(messages=messages, manifest=manifest, plan=plan,
                         managed_block_hashes=dict(existing_hashes))


__all__ = ['render_context']
