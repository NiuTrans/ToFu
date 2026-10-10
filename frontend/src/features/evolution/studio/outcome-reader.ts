/** One bounded, owner-routed terminal source page. Explicit reads only.
 * The parent owns selection, visibility and teardown; stale responses cannot
 * repaint another run/task. No model calls, durable writes or page cache.
 */
import type { EvolutionStageOutcomePage, EvolutionSourceKind, EvolutionSourceReference } from '../../../api/evolution-runs';
import { createButton } from '../../../ui/index';
import type { StudioCopy } from '../goal-workbench';

export type OutcomeRead = (runId: string, taskId: string, offset: number, signal: AbortSignal, sourceKind?: EvolutionSourceKind) =>
  Promise<{outcome: EvolutionStageOutcomePage}>;
export interface OutcomeSelection {offset?: number; kind?: EvolutionSourceKind; references?: Partial<Record<EvolutionSourceKind, EvolutionSourceReference>>}

export function mountOutcomeReader(host: HTMLElement, ports: {
  copy: StudioCopy; read: OutcomeRead; visible?: () => boolean; fallbackFocus?: () => void;
}) {
  let disposed = false, epoch = 0, runId = '', taskId = '', requestedOffset = 0;
  let controller: AbortController | undefined;
  let page: EvolutionStageOutcomePage | undefined;
  let opener: HTMLElement | undefined;
  let loading = false;
  let sourceKind: EvolutionSourceKind = 'deliverable', toolEvidenceAvailable = false, artifactEvidenceAvailable = false, backgroundEvidenceAvailable = false;
  let frozenFacts = '', restoredSource = false;
  let expectedSources: OutcomeSelection['references'];
  host.hidden = true;host.className = 'evo-mission-section evo-outcome-reader';
  const title = document.createElement('h3');title.tabIndex = -1;title.textContent = ports.copy('sourceOriginal');
  const identity = document.createElement('small');identity.className = 'evo-outcome-identity';
  const state = document.createElement('p');state.setAttribute('role', 'status');state.setAttribute('aria-live', 'polite');
  const provenance = document.createElement('p');
  const range = document.createElement('p');range.className = 'evo-outcome-range';
  const consumption = document.createElement('small');consumption.className = 'evo-outcome-consumption';
  const original = document.createElement('pre');original.className = 'evo-mission-prose';original.tabIndex = 0;
  original.setAttribute('aria-label', ports.copy('sourceOriginal'));
  const fingerprint = document.createElement('details');
  const fingerprintTitle = document.createElement('summary');fingerprintTitle.textContent = ports.copy('sourceFingerprint');
  const fingerprintValue = document.createElement('small');fingerprint.append(fingerprintTitle, fingerprintValue);
  const actions = document.createElement('div');actions.className = 'evo-mission-actions';
  const deliveryChoice = createButton({label: ports.copy('sourceDeliverable'), size: 'sm',
    onClick: () => choose('deliverable')});
  const toolChoice = createButton({label: ports.copy('sourceToolEvidence'), size: 'sm',
    onClick: () => choose('tool_evidence')});
  deliveryChoice.dataset.sourceAction = 'deliverable';toolChoice.dataset.sourceAction = 'tool_evidence';
  const artifactChoice = createButton({label:ports.copy('sourceArtifacts'), size:'sm', onClick:()=>choose('artifact_evidence')});
  artifactChoice.dataset.sourceAction='artifact_evidence';
  const backgroundChoice=createButton({label:ports.copy('sourceBackgroundEvidence'),size:'sm',onClick:()=>choose('background_evidence')});
  backgroundChoice.dataset.sourceAction='background_evidence';
  function sourceTitle(kind:EvolutionSourceKind) {return ports.copy(kind==='background_evidence'?'sourceBackgroundEvidence':kind==='artifact_evidence'?'sourceArtifacts':kind==='tool_evidence'?'sourceToolEvidence':'sourceOriginal');}
  const previous = createButton({label: ports.copy('sourcePrevious'), size: 'sm', onClick: () => {
    if (page) void load(Math.max(0, page.offset - 6000));
  }});
  const next = createButton({label: ports.copy('sourceNext'), size: 'sm', onClick: () => {
    if (page?.next_offset !== null && page?.next_offset !== undefined) void load(page.next_offset);
  }});
  const retry = createButton({label: ports.copy('sourceRetry'), size: 'sm', onClick: () => {void load(requestedOffset);}});
  const close = createButton({label: ports.copy('sourceClose'), size: 'sm', onClick: () => reset(true)});
  previous.dataset.sourceAction = 'previous';next.dataset.sourceAction = 'next';
  retry.dataset.sourceAction = 'retry';close.dataset.sourceAction = 'close';
  actions.append(deliveryChoice, toolChoice, artifactChoice, backgroundChoice, previous, next, retry, close);
  host.append(title, identity, state, provenance, range, consumption, original, fingerprint, actions);

  function stop() {epoch++;controller?.abort();controller = undefined;loading = false;}
  function controls() {
    host.setAttribute('aria-busy', String(loading));
    previous.disabled = loading || !page || page.offset === 0;
    next.disabled = loading || !page || page.next_offset === null;
    retry.disabled = loading;
    deliveryChoice.hidden = !toolEvidenceAvailable && !artifactEvidenceAvailable && !backgroundEvidenceAvailable;
    backgroundChoice.hidden=!backgroundEvidenceAvailable;backgroundChoice.disabled=loading;
    backgroundChoice.setAttribute('aria-pressed',String(sourceKind==='background_evidence'));
    toolChoice.hidden = !toolEvidenceAvailable;artifactChoice.hidden = !artifactEvidenceAvailable;
    deliveryChoice.disabled = toolChoice.disabled = artifactChoice.disabled = loading;
    artifactChoice.setAttribute('aria-pressed',String(sourceKind==='artifact_evidence'));
    deliveryChoice.setAttribute('aria-pressed', String(sourceKind === 'deliverable'));
    toolChoice.setAttribute('aria-pressed', String(sourceKind === 'tool_evidence'));
  }
  function choose(kind: EvolutionSourceKind) {
    if (disposed || loading || kind === sourceKind || (kind==='tool_evidence' && !toolEvidenceAvailable) || (kind==='artifact_evidence' && !artifactEvidenceAvailable) || (kind==='background_evidence' && !backgroundEvidenceAvailable)) return;
    stop();page = undefined;sourceKind = kind;requestedOffset = 0;
    original.textContent = '';fingerprintValue.textContent = '';fingerprint.hidden = true;
    range.textContent = '';consumption.textContent = '';
    title.textContent = sourceTitle(kind);
    original.setAttribute('aria-label', title.textContent);
    void load(0);
  }
  function reset(returnFocus = false) {
    stop();page = undefined;taskId = '';runId = '';requestedOffset = 0;
    sourceKind = 'deliverable';toolEvidenceAvailable = false;artifactEvidenceAvailable = false;backgroundEvidenceAvailable=false;frozenFacts = '';restoredSource = false;
    expectedSources = undefined;
    title.textContent = ports.copy('sourceOriginal');original.setAttribute('aria-label', title.textContent);
    original.textContent = '';fingerprintValue.textContent = '';fingerprint.open = false;
    identity.textContent = '';state.textContent = '';range.textContent = '';consumption.textContent = '';
    provenance.textContent = '';host.hidden = true;
    if (returnFocus) {
      if (opener?.isConnected && !opener.hidden) opener.focus({preventScroll: true});
      else ports.fallbackFocus?.();
    }
    opener = undefined;controls();
  }
  function valid(value: EvolutionStageOutcomePage, offset: number) {
    // Response identity and page continuity protect the visible source. The
    // server's canonical schema remains the field/authority owner.
    if (!value || value.task_id !== taskId || value.offset !== offset
        || (value.source_kind ?? 'deliverable') !== sourceKind
        || (value.tool_evidence_available !== undefined && typeof value.tool_evidence_available !== 'boolean')
        || (value.artifact_evidence_available !== undefined && typeof value.artifact_evidence_available !== 'boolean')
        || (sourceKind==='background_evidence' && value.background_evidence_scope!=='owned_background_command_completions')
        || (sourceKind==='artifact_evidence' && (value.artifact_evidence_scope!=='declared_project_artifact_snapshots' || typeof value.artifact_evidence_complete!=='boolean'))
        || (sourceKind === 'tool_evidence' && !['terminal_retained_tool_messages', 'terminal_retained_tool_messages_and_background_completions'].includes(value.tool_evidence_scope || ''))
        || !Number.isSafeInteger(value.total_chars) || value.total_chars < offset
        || typeof value.content !== 'string' || value.content.length > 12000
        || !/^[0-9a-f]{64}$/.test(value.source_sha256)
        || !['completed','interrupted','truncated','failed'].includes(value.terminal_verdict)
        || !['collecting','sealed'].includes(value.archive_state)
        || typeof value.actuals_complete !== 'boolean' || !value.actuals
        || ![value.actuals.tokens,value.actuals.tool_calls,value.actuals.wall_clock_seconds]
          .every(count => Number.isSafeInteger(count) && count >= 0)) return false;
    const expected = expectedSources?.[sourceKind];
    if (expected && (expected.task_id !== value.task_id || expected.source_sha256 !== value.source_sha256
        || expected.total_chars !== value.total_chars || (expected.source_kind !== undefined && expected.source_kind !== sourceKind)
        || (sourceKind==='artifact_evidence' && expected.complete!==undefined && expected.complete!==value.artifact_evidence_complete))) return false;
    const chars = Array.from(value.content).length;
    if (chars > 6000 || offset + chars > value.total_chars
        || (value.next_offset === null ? offset + chars !== value.total_chars
            : value.next_offset !== offset + chars || chars === 0)) return false;
    if (frozenFacts && (facts(value) !== frozenFacts
        || (value.tool_evidence_available === true) !== toolEvidenceAvailable
        || (value.artifact_evidence_available === true) !== artifactEvidenceAvailable)) return false;
    return !page || (value.source_sha256 === page.source_sha256
                    && value.total_chars === page.total_chars
                    && value.stage === page.stage && value.terminal_verdict === page.terminal_verdict
                    && value.status === page.status && value.actuals_complete === page.actuals_complete
                    && value.actuals.tokens === page.actuals.tokens
                    && value.actuals.tool_calls === page.actuals.tool_calls
                    && value.actuals.wall_clock_seconds === page.actuals.wall_clock_seconds
                    && value.artifact_evidence_complete === page.artifact_evidence_complete
                    && (page.archive_state !== 'sealed' || value.archive_state === 'sealed'));
  }
  function facts(value: EvolutionStageOutcomePage) {
    return JSON.stringify([value.stage, value.status, value.terminal_verdict, value.actuals_complete,
      value.actuals.tokens, value.actuals.tool_calls, value.actuals.wall_clock_seconds]);
  }
  async function load(offset: number) {
    if (disposed || !taskId || ports.visible?.() === false) return;
    stop();const current = epoch;const selectedRun = runId, selectedTask = taskId;
    requestedOffset = offset;loading = true;retry.hidden = true;
    const focusedControl = actions.contains(document.activeElement) ? document.activeElement as HTMLElement : undefined;
    controller = new AbortController();const signal = controller.signal;
    state.textContent = ports.copy('sourceLoading');controls();
    try {
      const response = await ports.read(selectedRun, selectedTask, offset, signal, sourceKind);
      if (disposed || current !== epoch || signal.aborted || selectedRun !== runId
          || selectedTask !== taskId || ports.visible?.() === false) return;
      if (!valid(response.outcome, offset)) throw new Error('source_identity_or_continuity_changed');
      page = response.outcome;
      frozenFacts ||= facts(page);toolEvidenceAvailable = page.tool_evidence_available === true;
      artifactEvidenceAvailable = page.artifact_evidence_available === true;
      backgroundEvidenceAvailable=page.background_evidence_available===true;
      provenance.textContent = [restoredSource ? ports.copy('sourceRestored') : '',
        ports.copy('sourceReadOnly'), sourceKind==='background_evidence'?ports.copy('sourceBackgroundScope'):sourceKind === 'tool_evidence' ? ports.copy('sourceToolEvidenceScope') : sourceKind==='artifact_evidence' ? ports.copy('sourceArtifactScope') : ''].filter(Boolean).join(' · ');
      const verdictKey = {completed: 'sourceCompleted', interrupted: 'sourceInterrupted',
        truncated: 'sourceTruncated', failed: 'sourceFailed'}[page.terminal_verdict];
      state.textContent = `${ports.copy(verdictKey)} · ${ports.copy(page.archive_state === 'sealed' ? 'sourceSaved' : 'sourceCollecting')}`;
      if (sourceKind==='artifact_evidence' && page.artifact_evidence_complete===false) state.textContent+=` · ${ports.copy('sourceArtifactsIncomplete')}`;
      const end = page.offset + Array.from(page.content).length;
      range.textContent = `${ports.copy('sourceRange')}: ${end ? page.offset + 1 : 0}–${end} / ${page.total_chars.toLocaleString()}`;
      const lower = page.actuals_complete ? '' : '≥ ';
      consumption.textContent = `${ports.copy('sourceConsumption')}: ${ports.copy('missionTokens')} ${lower}${page.actuals.tokens.toLocaleString()} · ${ports.copy('toolCalls')} ${lower}${page.actuals.tool_calls.toLocaleString()} · ${ports.copy('missionElapsedSeconds')} ${lower}${page.actuals.wall_clock_seconds.toLocaleString()}${page.actuals_complete ? '' : ` · ${ports.copy('sourceLowerBound')}`}`;
      original.textContent = page.content;
      if (!page.content) state.textContent += ` · ${ports.copy('sourceEmpty')}`;
      original.scrollTop = 0;
      fingerprintValue.textContent = page.source_sha256;
      fingerprint.hidden = false;
    } catch {
      if (disposed || current !== epoch || signal.aborted || selectedRun !== runId || selectedTask !== taskId) return;
      state.textContent = ports.copy('sourceReadFailed');retry.hidden = false;
    } finally {
      if (!disposed && current === epoch && !signal.aborted) {
        loading = false;controller = undefined;controls();
        if (focusedControl?.isConnected && document.activeElement === document.body)
          focusedControl.focus({preventScroll: true});
      }
    }
  }
  return {
    select(selectedRun: string, selectedTask: string, restored: boolean, trigger: HTMLElement, selection?: OutcomeSelection) {
      if (disposed) return;
      const kind = selection?.kind || 'deliverable';
      const offset = selection?.offset ?? 0, reference = selection?.references?.[kind];
      if (!Number.isSafeInteger(offset) || offset < 0 || (reference && offset > reference.total_chars)) return;
      if (runId === selectedRun && taskId === selectedTask && sourceKind === kind
          && JSON.stringify(expectedSources) === JSON.stringify(selection?.references)) {
        host.hidden = false;
        if (selection?.offset !== undefined) {
          opener = trigger;
          if (requestedOffset !== offset) void load(offset);
          host.scrollIntoView?.({block: 'nearest', behavior: 'instant'});
        }
        return;
      }
      reset();runId = selectedRun;taskId = selectedTask;opener = trigger;restoredSource = restored;
      sourceKind = kind;
      expectedSources = selection?.references ? Object.fromEntries(Object.entries(selection.references).map(([key, value]) => [key, {...value}])) : undefined;
      title.textContent = sourceTitle(kind);
      original.setAttribute('aria-label', title.textContent);
      identity.textContent = selectedTask;provenance.textContent = [restored ? ports.copy('sourceRestored') : '', ports.copy('sourceReadOnly')].filter(Boolean).join(' · ');
      host.hidden = false;fingerprint.hidden = true;retry.hidden = true;
      title.focus({preventScroll: true});host.scrollIntoView?.({block: 'nearest', behavior: 'instant'});
      void load(offset);
    },
    reset: () => reset(),
    visibilityChanged(visible = ports.visible?.() !== false) {
      if (!visible && controller) {
        stop();state.textContent = ports.copy('sourceReadPaused');retry.hidden = false;controls();
      }
    },
    dispose() {disposed = true;reset();host.replaceChildren();},
  };
}
