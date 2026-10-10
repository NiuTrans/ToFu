/** One expiring artifact reservation for a loading or visible PDF.
 * The cache owns expiry and admission; this handle owns bounded renewal and
 * release. A late retain response after closing is released before retirement.
 */
import type { PaperBuildApi } from '../../api/paper-build';
import { PAPER_BUILD_LIMITS as limits, type PaperBuildViewerResultV1 } from '../../api/paper-build.generated';

export class BuildViewerLease {
  private readonly identity = crypto.randomUUID().replaceAll('-', '');
  private closed = false;
  private busy = false;
  private released = false;
  private retained = false;
  private deadline = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly resume = (): void => {
    if (document.visibilityState === 'visible' && this.retained) void this.renew();
  };
  private readonly leave = (): void => this.expire();

  constructor(private readonly api: PaperBuildApi, private readonly documentId: string,
    private readonly buildId: string, private readonly lost: () => void) {
    window.addEventListener('pagehide', this.leave);
  }

  async retain(): Promise<boolean> {
    if (this.closed || this.busy) return false;
    this.busy = true;
    const started = performance.now();
    try {
      const result = await this.api.viewer(this.documentId, this.buildId, { viewer_id: this.identity, operation: 'retain' });
      if (this.closed) return false;
      if (!this.accept(result, started)) { this.expire(); return false; }
      document.addEventListener('visibilitychange', this.resume);
      return true;
    } catch (error) {
      this.close(); throw error;
    } finally {
      this.busy = false;
      if (this.closed) this.release();
    }
  }

  close(): void {
    if (this.closed) return;
    this.closed = true; this.clearTimer();
    document.removeEventListener('visibilitychange', this.resume);
    window.removeEventListener('pagehide', this.leave);
    if (!this.busy) this.release();
  }

  private release(): void {
    if (this.released) return;
    this.released = true;
    void this.api.viewer(this.documentId, this.buildId, { viewer_id: this.identity, operation: 'release' })
      .catch(() => { /* The bounded server lease expires if release cannot arrive. */ });
  }

  private accept(result: PaperBuildViewerResultV1, started: number): boolean {
    if (result.retained !== true || !Number.isInteger(result.expires_in_seconds)
      || result.expires_in_seconds <= 0 || result.expires_in_seconds > limits.viewer_lease_seconds) return false;
    this.deadline = started + result.expires_in_seconds * 1000;
    if (performance.now() >= this.deadline) return false;
    this.retained = true; this.schedule(); return true;
  }

  private clearTimer(): void { if (this.timer !== null) clearTimeout(this.timer); this.timer = null; }
  private schedule(): void {
    this.clearTimer();
    this.timer = setTimeout(() => { this.timer = null; void this.renew(); },
      Math.max(0, Math.min(limits.viewer_renew_seconds * 1000, this.deadline - performance.now())));
  }
  private expire(): void { if (!this.closed) { this.close(); this.lost(); } }
  private async renew(): Promise<void> {
    if (this.closed || this.busy) return;
    this.clearTimer();
    if (performance.now() >= this.deadline) { this.expire(); return; }
    this.busy = true;
    const started = performance.now();
    try {
      const result = await this.api.viewer(this.documentId, this.buildId, { viewer_id: this.identity, operation: 'renew' });
      if (!this.closed && !this.accept(result, started)) this.expire();
    } catch {
      if (!this.closed) {
        if (performance.now() >= this.deadline) this.expire();
        else this.schedule();
      }
    } finally { this.busy = false; if (this.closed) this.release(); }
  }
}
