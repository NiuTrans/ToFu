/* ===== migrated source: orchestration-composer.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-composer.js — AI authoring conversation controller

   Owns Composer history, request single-flight/epoch state and revision-safe
   graph adoption. DOM, accessibility and focus behavior live in the injected
   orchestration-composer-view.js module.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationComposerController(options) {
  options = options || {};
  var state = { history: [], busy: false, epoch: 0 };
  var view = options.view || {};
  var limitPolicy = orchestrationRequestLimitPolicy(
    options.limitPolicy || options.requestLimits);
  var requests = createOrchestrationComposerRequestClient({
    api: options.api,
    normalizeComposeResult: options.normalizeComposeResult,
  });

  function normalizeInspection(value) {
    return projectOrchestrationInspection(options, value);
  }

  function _clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function _requestHistory(history) {
    var retained = limitPolicy.composeHistoryLimit();
    var selected = retained == null ? history : history.slice(-retained);
    var messageLimit = limitPolicy.composeHistoryMessageLimit();
    if (messageLimit == null) return selected;
    return selected.map(function (turn) {
      return Object.assign({}, turn, {
        content: String(turn && turn.content || '')
          .trim().slice(0, messageLimit),
      });
    });
  }

  function snapshot() {
    return { history: _clone(state.history), busy: state.busy };
  }

  function setEnabled(enabled) {
    if (typeof view.setEnabled === 'function') view.setEnabled(enabled);
  }

  function render() {
    if (typeof view.render === 'function') return view.render(snapshot());
    return false;
  }

  function toggle(force) {
    if (typeof view.toggle === 'function') return view.toggle(force, snapshot());
    return false;
  }

  function isOpen() {
    return typeof view.isOpen === 'function' && view.isOpen();
  }

  function close() {
    return toggle(false);
  }

  function open() {
    return toggle(true);
  }

  function _requestFailureMessage(response) {
    return orchestrationRequestFailureMessage(
      response, _translate, '', {keys: {
        'server-failed': 'orch.ai.serverFailed',
        'request-rejected': 'orch.ai.requestRejected',
        'malformed-response': 'orch.ai.malformedResponse',
        'transport-failed': 'orch.ai.requestFailed',
      }, defaultKey: 'orch.ai.requestFailed'});
  }

  function _adoptDefinition(definition, id, opts) {
    var value;
    if (typeof options.applyDefinitionResult === 'function') {
      value = options.applyDefinitionResult(definition, id, opts);
    } else if (typeof options.applyDefinition === 'function') {
      value = options.applyDefinition(definition, id, opts);
    }
    return normalizeOrchestrationDefinitionAdoption(value);
  }

  function clear() {
    // Invalidate an in-flight request. It may finish at the transport layer,
    // but it can no longer append history or apply a graph after the user has
    // explicitly cleared the conversation.
    state.epoch++;
    state.history.splice(0, state.history.length);
    state.busy = false;
    setEnabled(true);
    render();
    return snapshot();
  }

  function handleKey(event) {
    if (event.key !== 'Enter' || event.shiftKey) return;
    event.preventDefault();
    send();
  }

  async function send(requirement) {
    if (state.busy) return { ok: false, reason: 'busy' };
    var text = String(requirement == null
      ? (typeof view.requirement === 'function' ? view.requirement() : '')
      : requirement).trim();
    if (!text) return { ok: false, reason: 'empty' };

    if (!requests.available()) {
      if (typeof options.toast === 'function') {
        options.toast(_translate('orch.api.unavailable'), true);
      }
      return { ok: false, reason: 'unavailable' };
    }

    if (typeof view.clearRequirement === 'function') view.clearRequirement();
    var priorHistory = _clone(state.history);
    state.history.push({ role: 'user', content: text });
    state.busy = true;
    var requestEpoch = ++state.epoch;
    var composeRevision = typeof options.revision === 'function'
      ? options.revision() : 0;
    var current = typeof options.currentDefinition === 'function'
      ? options.currentDefinition() : null;
    render();
    setEnabled(false);

    var response = await requests.compose(
      text, current, _requestHistory(priorHistory));
    if (response.cause) {
      reportOrchestrationDiagnostic(options.onError, 'compose', response.cause);
    }
    // clear() or a newer request invalidated this response. Do not touch busy
    // state here: a newer request may currently own the controls.
    if (requestEpoch !== state.epoch) {
      return { ok: false, reason: 'stale' };
    }

    state.busy = false;
    setEnabled(true);
    var result = response.result;
    if (!response.ok || !result) {
      state.history.push({
        role: 'assistant', content: _requestFailureMessage(response),
      });
      render();
      return {
        ok: false,
        reason: response.reason,
        status: response.status,
        error: response.error,
      };
    }
    var inspection = normalizeInspection(result);

    var reply = result.reply || (result.ok
      ? _translate('orch.ai.updated') : _translate('orch.ai.invalid'));
    var errors = orchestrationIssueMessages(result, { maxMessages: 3 });
    if (!result.ok && errors.length) {
      reply += '\n' + errors.join('; ');
    }
    state.history.push({ role: 'assistant', content: String(reply) });
    render();

    if (result.ok && result.definition) {
      var currentRevision = typeof options.revision === 'function'
        ? options.revision() : composeRevision;
      if (currentRevision !== composeRevision) {
        if (typeof options.toast === 'function') {
          options.toast(_translate('orch.doc.composeConflict'), true);
        }
        return { ok: false, reason: 'revision-conflict', result: result };
      }
      var currentId = typeof options.currentId === 'function'
        ? options.currentId() : null;
      var adoption = _adoptDefinition(result.definition, currentId, {
        dirty: true,
        inspection: inspection,
      });
      if (!adoption.ok) {
        if (adoption.cause) {
          reportOrchestrationDiagnostic(options.onError, 'adopt', adoption.cause);
        }
        state.history[state.history.length - 1].content =
          _translate('orch.store.readFailed');
        render();
        return { ok: false, reason: 'invalid-definition',
          adoption: adoption, result: result };
      }
      if (typeof options.warn === 'function') {
        options.warn(
          _translate('orch.ai.graphUpdated'),
          (inspection && inspection.warnings) || []
        );
      }
    }
    return { ok: !!result.ok, result: result };
  }

  return {
    state: state,
    snapshot: snapshot,
    render: render,
    isOpen: isOpen,
    open: open,
    toggle: toggle,
    close: close,
    clear: clear,
    handleKey: handleKey,
    send: send,
    setEnabled: setEnabled,
  };
}

