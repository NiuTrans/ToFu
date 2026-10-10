// Tofu Browser Bridge — Popup

document.addEventListener('DOMContentLoaded', () => {
  // The badge is DERIVED from the manifest — its hardcoded twin sat at
  // v4.3 through two version bumps and read as "you didn't update".
  const badge = document.getElementById('versionBadge');
  if (badge) badge.textContent = 'v' + chrome.runtime.getManifest().version;
  const statusDot = document.getElementById('statusDot');
  const statusText = document.getElementById('statusText');
  const statusReason = document.getElementById('statusReason');
  const serverInput = document.getElementById('serverUrl');
  const saveBtn = document.getElementById('saveBtn');
  const secretInput = document.getElementById('bridgeSecret');
  const saveSecretBtn = document.getElementById('saveSecretBtn');
  const toggleBtn = document.getElementById('toggleBtn');
  const statsDiv = document.getElementById('stats');
  const repairRow = document.getElementById('repairRow');
  const repairBtn = document.getElementById('repairBtn');
  const clientIdChip = document.getElementById('clientIdText');

  // A poll refresh must never fight the user's keyboard: an auto-refreshed
  // field is written ONLY when it is neither focused nor dirty. The dirty
  // latch is set by the user's first keystroke and cleared when the value
  // is committed (save) — the popup always opens clean. EVERY field the
  // 2s poll ever refreshes must go through this gate.
  function guardedField(input) {
    input.addEventListener('input', () => { input.dataset.dirty = '1'; });
    return {
      refresh(value) {
        if (document.activeElement === input || input.dataset.dirty === '1') return;
        input.value = value;
      },
      commit() { delete input.dataset.dirty; },
    };
  }
  const serverField = guardedField(serverInput);

  // Load current secret state (we never echo the actual value into the
  // popup — only show whether one is set, like a password reset flow).
  chrome.storage.local.get(['bridgeSecret'], (data) => {
    if (data.bridgeSecret) {
      secretInput.placeholder = '••••••••  (configured — leave blank to keep)';
    }
  });

  function setStatus(word, reason, dotState) {
    statusDot.className = 'status-dot ' + dotState;
    statusText.textContent = word;
    statusReason.textContent = reason;
  }

  function statTile(value, label, isBad) {
    const div = document.createElement('div');
    div.className = 'stat' + (isBad ? ' stat-bad' : '');
    const num = document.createElement('span');
    num.className = 'stat-num';
    num.textContent = value;
    const lab = document.createElement('span');
    lab.className = 'stat-label';
    lab.textContent = label;
    div.append(num, lab);
    return div;
  }

  function flashSaved(btn) {
    btn.textContent = 'Saved';
    setTimeout(() => { btn.textContent = 'Save'; }, 1500);
  }

  function updateStatus() {
    chrome.runtime.sendMessage({ type: 'getStatus' }, (resp) => {
      if (chrome.runtime.lastError || !resp) {
        setStatus('Offline', 'Service worker inactive — reopen the popup or reload the extension.', 'disconnected');
        return;
      }

      if (resp.connected) {
        setStatus('Connected', 'Commands from your Tofu server run in this browser.', 'connected');
      } else if (!resp.pollActive) {
        setStatus('Paused', 'Polling is off — the bridge accepts no commands.', 'paused');
      } else {
        setStatus('Disconnected', resp.lastError || 'Reaching for the server…', 'disconnected');
      }

      // The repair row appears exactly when the background has declared the
      // credential dead — and disappears the moment the silent ladder heals
      // it. It never asks for a secret: the button runs the same automatic
      // ladder, only allowed to open a foreground Tofu tab (user gesture).
      if (repairRow) {
        repairRow.style.display = (!resp.connected && resp.needsRepair) ? '' : 'none';
        if (repairBtn && resp.repairBusy) {
          repairBtn.disabled = true;
          repairBtn.textContent = 'Re-pairing…';
        } else if (repairBtn && repairBtn.textContent === 'Re-pairing…') {
          repairBtn.disabled = false;
          repairBtn.textContent = 'Re-pair now';
        }
      }

      if (resp.serverUrl && serverInput) {
        serverField.refresh(resp.serverUrl);
      }

      toggleBtn.textContent = resp.pollActive ? 'Pause' : 'Resume';

      // Client ID chip: truncated for layout, click copies the full ID.
      if (clientIdChip && resp.clientId) {
        clientIdChip.textContent = resp.clientId.substring(0, 12) + '…';
        clientIdChip.dataset.full = resp.clientId;
        clientIdChip.title = resp.clientId + ' — click to copy';
      }

      if (statsDiv) {
        const failed = resp.commandsFailed || 0;
        statsDiv.replaceChildren(
          statTile(resp.commandsExecuted || 0, 'Executed', false),
          statTile(failed, 'Failed', failed > 0),
          statTile(resp.resultQueue || 0, 'Queued', false),
          statTile(resp.inflight || 0, 'In-flight', false),
        );
      }
    });
  }

  saveBtn.addEventListener('click', () => {
    const url = serverInput.value.trim();
    if (!url) return;
    serverField.commit();
    chrome.runtime.sendMessage({ type: 'setServer', url }, () => {
      flashSaved(saveBtn);
      setTimeout(updateStatus, 500);
    });
  });

  saveSecretBtn.addEventListener('click', () => {
    const secret = secretInput.value;
    chrome.runtime.sendMessage({ type: 'setBridgeSecret', secret }, () => {
      flashSaved(saveSecretBtn);
      secretInput.value = '';
      setTimeout(updateStatus, 500);
    });
  });

  if (repairBtn) {
    repairBtn.addEventListener('click', () => {
      repairBtn.disabled = true;
      repairBtn.textContent = 'Re-pairing…';
      chrome.runtime.sendMessage({ type: 'repairNow' }, (resp) => {
        repairBtn.disabled = false;
        repairBtn.textContent = (resp && resp.ok) ? 'Re-paired' : 'Re-pair now';
        setTimeout(updateStatus, 500);
      });
    });
  }

  toggleBtn.addEventListener('click', () => {
    chrome.runtime.sendMessage({ type: 'toggle' }, () => {
      setTimeout(updateStatus, 300);
    });
  });

  if (clientIdChip) {
    clientIdChip.addEventListener('click', () => {
      const full = clientIdChip.dataset.full;
      if (!full || !navigator.clipboard) return;
      navigator.clipboard.writeText(full).then(() => {
        const shown = clientIdChip.textContent;
        clientIdChip.textContent = 'Copied';
        setTimeout(() => { clientIdChip.textContent = shown; }, 900);
      }, () => {});
    });
  }

  updateStatus();
  setInterval(updateStatus, 2000);
});
