/* ===== migrated source: orchestration-inspector-content.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-inspector-content.js — Inspector read-only content provider

   Builds headers, collapsible sections, run traces, backend personas and
   control-flow summaries. Inspector selection/layout lives in
   orchestration-inspector-view.js; editable FieldSpecs live in
   orchestration-inspector.js. Loads after orchestration-graph.js so all
   surfaces partition incoming/outgoing edges through one topology seam.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationInspectorContent(options) {
  options = options || {};

  function _escape(value) {
    return typeof options.escape === 'function'
      ? options.escape(value) : String(value == null ? '' : value);
  }
  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }
  function _richCopy(value) {
    return typeof options.richCopy === 'function'
      ? options.richCopy(value) : _escape(value);
  }
  function _edges() {
    return typeof options.edges === 'function' ? options.edges() : [];
  }
  function _find(id) {
    return typeof options.findNode === 'function' ? options.findNode(id) : null;
  }
  function _nodeLabel(node) {
    return typeof options.nodeLabel === 'function'
      ? options.nodeLabel(node) : (node.name || node.id || '');
  }
  function _traceSnapshot(nodeId) {
    if (typeof options.traceSnapshotFor === 'function') {
      var snapshot = options.traceSnapshotFor(nodeId);
      if (snapshot && typeof snapshot === 'object'
          && Array.isArray(snapshot.attempts)) return snapshot;
    }
    var trace = typeof options.traceFor === 'function'
      ? options.traceFor(nodeId) : null;
    var history = typeof options.traceHistoryFor === 'function'
      ? options.traceHistoryFor(nodeId) : [];
    history = Array.isArray(history) ? history.slice() : [];
    var count = typeof options.traceCountFor === 'function'
      ? options.traceCountFor(nodeId) : 0;
    var attempts = projectOrchestrationTraceAttempts(trace, history, count);
    return Object.freeze({
      current: trace,
      history: Object.freeze(history),
      total: attempts.length ? attempts[attempts.length - 1].total : count,
      attempts: attempts,
    });
  }

  function header(node) {
    var avatar = typeof options.avatar === 'function' ? options.avatar(node) : '';
    var kind = typeof options.kindLabel === 'function'
      ? options.kindLabel(node) : '';
    var blurb = typeof options.blurb === 'function' ? options.blurb(node) : '';
    var html = '<div class="orch-insp-head">' + avatar
      + '<div class="orch-insp-htext">'
      + '<span class="orch-insp-kind">' + _escape(kind) + '</span>'
      + '<span class="orch-insp-type">' + _escape(_nodeLabel(node)) + '</span>'
      + '</div></div>';
    if (blurb) {
      html += '<div class="orch-insp-blurb">' + _escape(blurb) + '</div>';
    }
    return html;
  }

  function section(titleKey, icon, open, inner, hintKey) {
    var html = '<details class="orch-sec" data-orch-section-key="'
      + _escape(titleKey) + '"' + (open ? ' open' : '') + '>';
    html += '<summary class="orch-sec-sum">' + (icon || '')
      + '<span>' + _escape(_translate(titleKey)) + '</span>'
      + '<span class="orch-sec-chev">\u203a</span></summary>'
      + '<div class="orch-sec-body">';
    if (hintKey) {
      html += '<div class="orch-sec-hint">'
        + _richCopy(_translate(hintKey)) + '</div>';
    }
    return html + (inner || '') + '</div></details>';
  }

  function _traceAttemptBody(trace) {
    var statusProjection = projectOrchestrationTraceStatusPresentation(
      trace.status, options.traceContract, _translate);
    var status = statusProjection.status;
    var html = '<div class="orch-runtrace-row"><span class="orch-runtrace-lbl">'
      + _escape(_translate('orch.run.status')) + '</span>'
      + '<span class="orch-runtrace-status orch-runtrace-'
      + _escape(status) + '">' + _escape(statusProjection.label)
      + '</span></div>';
    var activity = projectOrchestrationTraceActivity(
      trace, options.traceContract);
    if (activity.stateChanging > 0) {
      html += '<div class="orch-runtrace-row"><span class="orch-runtrace-lbl">'
        + _escape(_translate('orch.run.actions')) + '</span><span>'
        + activity.stateChanging + '</span></div>';
    }
    var output = projectOrchestrationTraceSections(
      trace, ['output'], options.traceContract)[0];
    if (output) {
      html += '<div class="orch-runtrace-lbl orch-runtrace-outlbl">'
        + _escape(_translate('orch.run.output'))
        + (output.truncated ? ' <span class="orch-runtrace-trunc">'
          + _escape(_translate('orch.run.truncated')) + '</span>' : '')
        + '</div>'
        + '<pre class="orch-runtrace-out">'
        + _escape(output.text) + '</pre>';
    } else if (status === 'running') {
      var phaseText = '';
      if (trace.phaseDetailKey) {
        phaseText = _translate(
          trace.phaseDetailKey, trace.phaseDetailArgs || {}
        );
        if (phaseText === trace.phaseDetailKey) phaseText = '';
      }
      if (!phaseText && trace.phase) {
        var phaseKey = 'orch.run.phase.' + trace.phase;
        phaseText = _translate(phaseKey);
        if (phaseText === phaseKey) phaseText = '';
      }
      html += '<div class="orch-runtrace-waiting">'
        + _escape(phaseText || _translate('orch.run.streaming')) + '</div>';
    }
    return html;
  }

  function runTraceBody(node) {
    var attempts = _traceSnapshot(node.id).attempts;
    if (!attempts.length) return null;
    if (attempts.length === 1) return _traceAttemptBody(attempts[0].trace);
    return '<div class="orch-runtrace-attempts">'
      + attempts.map(function (attempt, index) {
        var status = projectOrchestrationTraceStatusPresentation(
          attempt.trace.status, options.traceContract, _translate);
        var delta = index > 0
          ? projectOrchestrationTraceAttemptDeltaPresentation(
            attempts[index - 1].trace, attempt.trace,
            options.traceContract, _translate
          ) : null;
        return '<details class="orch-sec orch-runtrace-attempt" '
          + 'data-orch-section-key="trace-' + _escape(attempt.key) + '"'
          + (attempt.current ? ' open' : '') + '>'
          + '<summary class="orch-sec-sum"><span>'
          + _escape(_translate('tm.trace.iter')) + ' ' + attempt.ordinal
          + ' / ' + attempt.total + ' · ' + _escape(status.label)
          + '</span>' + (delta && delta.label
            ? '<span class="orch-runtrace-delta">'
              + _escape(delta.label) + '</span>' : '')
          + '<span class="orch-sec-chev">\u203a</span></summary>'
          + '<div class="orch-sec-body">' + _traceAttemptBody(attempt.trace)
          + '</div></details>';
      }).join('') + '</div>';
  }

  function personaSectionBody(node) {
    var persona = typeof options.persona === 'function'
      ? options.persona(node.role) : null;
    if (!persona || !persona.prompt) {
      return '<div class="orch-persona-empty">'
        + _escape(_translate('orch.persona.none')) + '</div>';
    }
    return '<div class="orch-persona-lbl orch-persona-promptlbl">'
      + _escape(_translate('orch.persona.prompt')) + '</div>'
      + '<pre class="orch-persona-prompt" readonly>'
      + _escape(persona.prompt) + '</pre>';
  }

  function flowSummaryBody(node) {
    var connections = orchestrationConnections(_edges(), node.id);
    var incoming = connections.incoming
      .map(function (edge) {
        var source = _find(edge.from);
        return source ? _nodeLabel(source) : edge.from;
      });
    var outgoing = connections.outgoing
      .map(function (edge) {
        var target = _find(edge.to);
        return target ? _nodeLabel(target) : edge.to;
      });
    var inputText;
    var outputText;
    if (node.kind === 'start') {
      var seed = String(node.params && node.params.seed || '').trim();
      inputText = _escape(seed
        ? _translate('orch.flow.seedSet') : _translate('orch.flow.fromUser'));
    } else {
      inputText = incoming.length
        ? incoming.map(_escape).join(', ') : _escape(_translate('orch.flow.none'));
    }
    outputText = node.kind === 'stop'
      ? _escape(_translate('orch.flow.toChat'))
      : (outgoing.length
        ? outgoing.map(_escape).join(', ') : _escape(_translate('orch.flow.none')));
    var html = '<div class="orch-flow-row"><span class="orch-flow-arrow">\u2192</span>'
      + '<span class="orch-flow-lbl">' + _escape(_translate('orch.flow.in'))
      + '</span><span class="orch-flow-val">' + inputText + '</span></div>'
      + '<div class="orch-flow-row"><span class="orch-flow-arrow">\u2190</span>'
      + '<span class="orch-flow-lbl">' + _escape(_translate('orch.flow.out'))
      + '</span><span class="orch-flow-val">' + outputText + '</span></div>';
    var carryKey = 'orch.flow.carry.' + node.kind;
    var carry = _translate(carryKey);
    if (carry && carry !== carryKey) {
      html += '<div class="orch-flow-carry">' + _escape(carry) + '</div>';
    }
    return html;
  }

  return {
    header: header,
    section: section,
    runTraceBody: runTraceBody,
    personaSectionBody: personaSectionBody,
    flowSummaryBody: flowSummaryBody,
    traceSnapshot: _traceSnapshot,
  };
}

