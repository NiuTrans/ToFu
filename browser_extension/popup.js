// ChatUI Browser Bridge — Popup (v4)

document.addEventListener('DOMContentLoaded', () => {
  const statusDot = document.getElementById('statusDot');
  const statusText = document.getElementById('statusText');
  const serverInput = document.getElementById('serverUrl');
  const saveBtn = document.getElementById('saveBtn');
  const toggleBtn = document.getElementById('toggleBtn');
  const statsDiv = document.getElementById('stats');

  function updateStatus() {
    chrome.runtime.sendMessage({ type: 'getStatus' }, (resp) => {
      if (chrome.runtime.lastError || !resp) {
        statusDot.className = 'status-dot disconnected';
        statusText.textContent = 'Service Worker inactive';
        return;
      }

      statusDot.className = resp.connected ? 'status-dot connected' : 'status-dot disconnected';
      statusText.textContent = resp.connected ? 'Connected' : (resp.lastError || 'Disconnected');

      if (resp.serverUrl && serverInput) {
        serverInput.value = resp.serverUrl;
      }

      toggleBtn.textContent = resp.pollActive ? '⏸ Pause' : '▶ Resume';

      // Show client ID (first 12 chars for readability)
      const clientIdText = document.getElementById('clientIdText');
      if (clientIdText && resp.clientId) {
        clientIdText.textContent = resp.clientId.substring(0, 12) + '…';
        clientIdText.title = resp.clientId;  // Full ID on hover
      }

      // Stats
      if (statsDiv) {
        statsDiv.innerHTML = `
          <div>✓ ${resp.commandsExecuted || 0} executed</div>
          <div>✗ ${resp.commandsFailed || 0} failed</div>
          <div>📤 ${resp.resultQueue || 0} queued</div>
          <div>⏳ ${resp.inflight || 0} in-flight</div>
        `;
      }
    });
  }

  saveBtn.addEventListener('click', () => {
    const url = serverInput.value.trim();
    if (!url) return;
    chrome.runtime.sendMessage({ type: 'setServer', url }, () => {
      saveBtn.textContent = '✓ Saved';
      setTimeout(() => { saveBtn.textContent = 'Save'; }, 1500);
      setTimeout(updateStatus, 500);
    });
  });

  toggleBtn.addEventListener('click', () => {
    chrome.runtime.sendMessage({ type: 'toggle' }, () => {
      setTimeout(updateStatus, 300);
    });
  });

  updateStatus();
  setInterval(updateStatus, 2000);
});
