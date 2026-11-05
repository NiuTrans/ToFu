/**
 * Transient browser results, partitioned by server/device/profile.
 * Entry: BrowserResultOutbox. Batch receipts own acknowledgement or rollback;
 * queue limits bound each transport frame and never refuse command execution.
 * Loaded synchronously by the worker; no Chrome, network or timer dependency.
 */
class BrowserResultOutbox {
  constructor({maxResults = 32, maxBytes = 12 * 1024 * 1024} = {}) {
    this.maxResults = maxResults;
    this.maxBytes = maxBytes;
    this.oversizeError = `Browser command result exceeded the ${maxBytes / (1024 * 1024)} MiB poll transport limit`;
    this.scopes = new Map();
    this.activeScope = null;
    this.encoder = new TextEncoder();
  }

  scopeKey(connection) {
    return JSON.stringify([connection.serverUrl, connection.clientId, connection.profile || '']);
  }

  select(connection) {
    this.activeScope = connection ? this.scopeKey(connection) : null;
    this.prune();
  }

  prune() {
    for (const [key, scope] of this.scopes) {
      if (key !== this.activeScope && !scope.pending.length && !scope.leased) this.scopes.delete(key);
    }
  }

  scope(connection) {
    const key = this.scopeKey(connection);
    if (!this.scopes.has(key)) {
      this.scopes.set(key, {pending: [], leased: 0, batchMax: this.maxResults});
    }
    return this.scopes.get(key);
  }

  enqueue(connection, result) {
    this.scope(connection).pending.push(result);
  }

  get size() {
    let count = 0;
    for (const scope of this.scopes.values()) count += scope.pending.length + scope.leased;
    return count;
  }

  compact(candidate) {
    return {id: candidate && candidate.id, result: null, error: this.oversizeError};
  }

  take(scope) {
    const candidates = scope.pending.splice(0, Math.min(scope.batchMax, scope.pending.length));
    const batch = [];
    let encodedBytes = 0;
    for (let index = 0; index < candidates.length; index += 1) {
      let candidate = candidates[index];
      let serialized;
      try { serialized = JSON.stringify(candidate); }
      catch (_) { candidate = this.compact(candidate); serialized = JSON.stringify(candidate); }
      let bytes = this.encoder.encode(serialized).byteLength;
      if (bytes > this.maxBytes) {
        candidate = this.compact(candidate);
        bytes = this.encoder.encode(JSON.stringify(candidate)).byteLength;
      }
      if (batch.length && encodedBytes + bytes > this.maxBytes) {
        scope.pending.unshift(...candidates.slice(index));
        break;
      }
      batch.push(candidate);
      encodedBytes += bytes;
    }
    return batch;
  }

  begin(connection) {
    const scope = this.scope(connection);
    const items = this.take(scope);
    scope.leased += items.length;
    let open = true;
    const settle = replay => {
      if (!open) return false;
      open = false;
      scope.leased -= items.length;
      if (replay) scope.pending.unshift(...replay);
      this.prune();
      return true;
    };
    return Object.freeze({
      items,
      acknowledge: () => settle(null),
      rollback: () => settle(items),
      rejectPayload: () => {
        if (!open) return false;
        if (items.length > 1) {
          scope.batchMax = Math.max(1, Math.floor(items.length / 2));
          settle(items);
          return false;
        }
        if (items.length === 1 && items[0].error !== this.oversizeError) {
          settle(items.map(item => this.compact(item)));
          return false;
        }
        settle(items);
        return true;
      },
    });
  }
}
