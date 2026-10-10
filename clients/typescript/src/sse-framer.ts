/** Bounded UTF-8 line framing. No transport ownership or application JSON policy.
 * All fields/comments count toward a frame; CRLF and CR normalize to one LF.
 */
import { SDK_SSE_MAX_LINE_BYTES, SDK_SSE_MAX_EVENT_BYTES } from './api-v4.generated.js';

export class SSELimitError extends Error {}

export class SSELineFramer {
  private pending = new Uint8Array(1024);
  private length = 0;
  private eventBytes = 0;
  private skipLF = false;
  private firstLine = true;
  private readonly decoder = new TextDecoder('utf-8', { ignoreBOM: true });

  *feed(chunk: Uint8Array): Iterable<string> {
    let start = 0;
    if (this.skipLF && chunk.length) {
      start = chunk[0] === 10 ? 1 : 0;
      this.skipLF = false;
    }
    let cr = chunk.indexOf(13, start);
    let lf = chunk.indexOf(10, start);
    while (start < chunk.length) {
      const end = cr < 0 ? (lf < 0 ? chunk.length : lf) : (lf < 0 ? cr : Math.min(cr, lf));
      const size = end - start;
      const required = this.length + size;
      if (required > SDK_SSE_MAX_LINE_BYTES) throw new SSELimitError('SSE line exceeded the SDK byte budget');
      this.eventBytes += size + Number(end < chunk.length);
      if (this.eventBytes > SDK_SSE_MAX_EVENT_BYTES) throw new SSELimitError('SSE event exceeded the SDK byte budget');
      if (required > this.pending.length) {
        const grown = new Uint8Array(Math.min(SDK_SSE_MAX_LINE_BYTES, Math.max(required, this.pending.length * 2)));
        grown.set(this.pending.subarray(0, this.length));
        this.pending = grown;
      }
      this.pending.set(chunk.subarray(start, end), this.length);
      this.length = required;
      if (end === chunk.length) return;
      let line = this.decoder.decode(this.pending.subarray(0, this.length));
      if (this.firstLine) {
        this.firstLine = false;
        line = line.replace(/^\uFEFF/, '');
      }
      if (this.length === 0) this.eventBytes = 0;
      this.length = 0;
      start = end + 1;
      if (chunk[end] === 13) {
        if (start < chunk.length && chunk[start] === 10) start += 1;
        else if (start === chunk.length) this.skipLF = true;
      }
      if (cr >= 0 && cr < start) cr = chunk.indexOf(13, start);
      if (lf >= 0 && lf < start) lf = chunk.indexOf(10, start);
      yield line;
    }
  }
}
