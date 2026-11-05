/**
 * Accepted command lifecycle: scoped deduplication, deadline, temporary-resource
 * teardown, counters and result publication. Entry: BrowserCommandExecutor.
 * Injected handlers own tool behavior; this owner never imposes an admission cap.
 */
class BrowserCommandExecutor {
  constructor(ports) {
    this.ports = ports;
    this.inflight = new Set();
    this.completed = 0;
    this.failed = 0;
  }

  status() {
    return {inflight: this.inflight.size, commandsExecuted: this.completed, commandsFailed: this.failed};
  }

  async execute(command, connection) {
    const {ports} = this;
    const key = JSON.stringify([ports.outbox.scopeKey(connection), command.id]);
    if (this.inflight.has(key)) {
      ports.warn(`Skipping duplicate command: ${command.id}`);
      return;
    }
    this.inflight.add(key);
    let result = null;
    let error = null;
    let resources;
    try {
      resources = ports.createResources();
      ports.log(`▶ ${command.type} (${command.id.slice(0, 8)})`);
      const start = ports.now();
      const timeout = ports.timeout(command.type);
      result = await ports.withTimeout(
        ports.execute(command, resources, connection), timeout,
        `Command '${command.type}' timed out after ${timeout / 1000}s`);
      this.completed += 1;
      ports.log(`✓ ${command.type} (${ports.now() - start}ms)`);
    } catch (failure) {
      error = failure?.message || String(failure);
      this.failed += 1;
      ports.error(`✗ ${command.type}: ${error}`);
    } finally {
      if (resources) {
        try { await resources.close(); }
        catch (failure) {
          ports.warn(`Temporary tab cleanup failed: ${ports.describeError(failure)}`);
          if (!error) {
            error = `Temporary browser tab cleanup failed: ${ports.describeError(failure)}`;
            result = null;
            this.completed -= 1;
            this.failed += 1;
          }
        }
      }
    }
    ports.outbox.enqueue(connection, {id: command.id, result, error});
    this.inflight.delete(key);
    ports.settled(connection);
  }
}
