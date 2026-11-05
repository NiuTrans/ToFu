/**
 * Owns one active browser bridge request, retry timer and connection status.
 * Entry: BrowserPollTransport. Dependencies are explicit ports: connection,
 * telemetry, fetch, timers, badge/diagnostics, repair, dispatch and result outbox.
 * Retired requests cannot acknowledge results, dispatch commands or change UI.
 */
class BrowserPollTransport {
  constructor(ports, policy) {
    this.ports = ports;
    this.policy = policy;
    this.running = false;
    this.connection = null;
    this.request = null;
    this.retryTimer = null;
    this.state = {connected: false, lastError: '', authFailures: 0, needsRepair: false};
  }

  status() { return {...this.state, pollActive: this.running}; }
  isRunning() { return this.running; }
  resetAuthentication() { this.state.authFailures = 0; this.state.needsRepair = false; }

  clearRetry() {
    if (this.retryTimer !== null) this.ports.clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  schedule(delay) {
    this.clearRetry();
    if (!this.running) return;
    this.retryTimer = this.ports.setTimeout(() => {
      this.retryTimer = null;
      void this.poll();
    }, delay);
  }

  finishDeadline(request) {
    if (request.timer !== null) this.ports.clearTimeout(request.timer);
    request.timer = null;
  }

  retireRequest() {
    const request = this.request;
    this.request = null;
    if (!request) return;
    this.finishDeadline(request);
    request.receipt?.rollback();
    try { request.controller.abort(); } catch (_) {}
  }

  start() {
    if (this.running) return this.request?.promise || Promise.resolve();
    const connection = this.ports.connection();
    if (!connection.serverUrl) return Promise.resolve();
    this.connection = connection;
    this.ports.outbox.select(connection);
    this.running = true;
    this.ports.log('Polling started');
    return this.poll();
  }

  stop() {
    this.running = false;
    this.clearRetry();
    this.retireRequest();
    this.ports.outbox.select(null);
    this.state.connected = false;
    this.ports.log('Polling stopped');
  }

  reconnect() {
    const running = this.running;
    this.stop();
    this.resetAuthentication();
    return running ? this.start() : Promise.resolve();
  }

  nudge(connection) {
    if (!this.running || this.ports.outbox.scopeKey(connection)
        !== this.ports.outbox.scopeKey(this.connection)) return;
    this.retireRequest();
    this.schedule(0);
  }

  owns(request) { return this.running && this.request === request; }

  poll() {
    if (!this.running) return Promise.resolve();
    if (this.request) return this.request.promise;
    const request = {connection: this.connection, controller: new AbortController(),
      timer: null, receipt: null, promise: null};
    this.request = request;
    request.promise = this.perform(request);
    return request.promise;
  }

  async perform(request) {
    const {ports, policy} = this;
    try {
      request.receipt = ports.outbox.begin(request.connection);
      request.timer = ports.setTimeout(() => request.controller.abort(), policy.fetchTimeout);
      const response = await ports.fetch(`${request.connection.serverUrl}/api/browser/poll`, {
        method: 'POST', signal: request.controller.signal,
        headers: {...request.connection.headers, 'X-Browser-Protocol-Version': String(policy.protocolVersion)},
        credentials: 'include', body: JSON.stringify({results: request.receipt.items,
          ...ports.frame(request.connection)}),
      });
      if (!this.owns(request)) return;
      this.finishDeadline(request);
      if (!response.ok) {
        await this.rejectResponse(request, response);
        return;
      }
      const data = await response.json();
      if (!this.owns(request)) return;
      // Access before acknowledgement: malformed JSON/null responses replay.
      const commands = data.commands;
      request.receipt.acknowledge();
      this.state.connected = true;
      this.state.lastError = '';
      this.resetAuthentication();
      ports.badge('on');
      if (commands && commands.length > 0) {
        // This whole batch is accepted. A synchronous command setup failure
        // may nudge the next poll; it must not discard the remaining commands.
        for (const command of commands) {
          ports.dispatch(command, request.connection);
        }
      }
      if (this.owns(request)) this.schedule(policy.interval);
    } catch (error) {
      if (!this.owns(request)) return;
      request.receipt?.rollback();
      if (error?.name === 'AbortError') {
        this.state.connected = true;
        this.schedule(policy.interval);
      } else {
        this.state.connected = false;
        this.state.lastError = error?.message || 'Connection failed';
        ports.badge('error');
        ports.warn(`Poll error: ${this.state.lastError}`);
        this.schedule(policy.retryDelay);
      }
    } finally {
      this.finishDeadline(request);
      if (this.request === request) this.request = null;
    }
  }

  async rejectResponse(request, response) {
    const {ports, policy} = this;
    const {receipt} = request;
    if (response.status === 401) {
      const body = await response.json().catch(() => null);
      if (!this.owns(request)) return;
      receipt.rollback();
      this.state.authFailures += 1;
      this.state.connected = false;
      this.state.needsRepair = this.state.authFailures >= policy.authGiveUpAfter;
      this.state.lastError = this.authMessage(body);
      ports.badge(this.state.needsRepair ? 'repair' : 'error');
      ports.warn(this.state.lastError);
      void ports.repair(request.connection).catch(() => {});
      this.schedule(Math.min(policy.authRetryBase * (2 ** (this.state.authFailures - 1)), policy.authRetryMax));
      return;
    }
    if (response.status === 426) {
      const body = await response.json().catch(() => null);
      if (!this.owns(request)) return;
      receipt.rollback();
      this.resetAuthentication();
      this.state.connected = false;
      const required = Number(body && body.requiredProtocolVersion) || '?';
      this.state.lastError = `Extension upgrade required (protocol ${policy.protocolVersion} → ${required})`;
      ports.badge('repair');
      ports.warn(`${this.state.lastError}; parked until the next upgrade probe`);
      this.schedule(policy.upgradeRetryDelay);
      return;
    }
    if (response.status === 429) {
      const body = await response.json().catch(() => null);
      if (!this.owns(request)) return;
      receipt.rollback();
      const headerSeconds = Number(response.headers.get('Retry-After')) || 0;
      const bodySeconds = Number(body && body.retryAfter) || 0;
      const delay = Math.min(policy.authRetryMax, Math.max(1000, (headerSeconds || bodySeconds || 3) * 1000));
      this.state.connected = true;
      this.state.lastError = '';
      ports.badge('on');
      ports.warn(`Server admission busy; retrying in ${Math.ceil(delay / 1000)}s`);
      this.schedule(delay);
      return;
    }
    if (response.status === 413) {
      const backoff = receipt.rejectPayload();
      this.state.connected = true;
      this.state.lastError = '';
      ports.badge('on');
      this.schedule(backoff ? policy.retryDelay : 0);
      return;
    }
    if (response.status >= 500) {
      receipt.rollback();
      this.state.connected = true;
      ports.warn(`Server/proxy returned ${response.status}, retrying...`);
      this.schedule(policy.retryDelay);
      return;
    }
    throw new Error(`HTTP ${response.status}`);
  }

  authMessage(body) {
    const {authFailures, needsRepair} = this.state;
    if (body && body.error === 'bridge_auth_required') {
      return needsRepair
        ? `Bridge auth failed (401) ×${authFailures} — re-pairing automatically; an open Tofu tab finishes it instantly`
        : 'Bridge auth failed (401) — re-pairing automatically…';
    }
    return needsRepair
      ? `Bridge blocked by a proxy/SSO edge (401 ×${authFailures}) — it clears by itself once your Tofu panel is open in a tab`
      : 'Bridge blocked by a proxy/SSO edge (401) — retrying with your browser session…';
  }
}
