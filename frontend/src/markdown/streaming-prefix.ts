/**
 * Prefix reuse for the streaming Markdown hot path.
 *
 * A streaming frame appends to the accumulated source. Every completed leading
 * block — all lines up to the last blank-line boundary that sits outside any
 * open code fence and before which there is no math placeholder — is
 * idempotent under the whole prepare → parse → sanitize → project pipeline:
 * its output is a byte-identical prefix of the full frame. A frame can
 * therefore reuse the PRIOR frame's already-computed output for that stable
 * prefix and run the pipeline only over the grown tail, turning an O(source²)
 * stream into O(source).
 *
 * Reuse is strictly verified, never assumed: a snapshot seeds a frame only
 * when the raw/escaped variant matches, the new source still starts with the
 * exact stable prefix, AND that prefix produced no math (math markers are
 * globally numbered, so a prefix that contains any would renumber the tail's
 * fragments). Anything else falls back to a full render. Terminal renders
 * always re-render in full and never read a streaming snapshot, so an
 * approximate streaming frame can never be terminal-frozen.
 * Dependencies: none. No ambient application state.
 */

/**
 * Byte length of the stable leading run of ``text``: every line ending at or
 * before the last blank-line boundary that is outside any fenced code block.
 * Returns 0 when the prefix contains a math placeholder (``\x02MATH``) — those
 * are globally indexed and unsafe to split — or when no safe boundary exists.
 */
export function findStreamingPrefixEnd(text: string): number {
  let inFence = false;
  let fenceMarker = '';
  let fenceLength = 0;
  let lastMathLineEnd = -1;
  let boundary = 0;
  let offset = 0;
  while (offset <= text.length) {
    const newline = text.indexOf('\n', offset);
    const end = newline < 0 ? text.length : newline;
    const line = text.slice(offset, end);
    const fence = line.match(/^( {0,3})(`{3,}|~{3,})/);
    if (fence) {
      const marker = fence[2][0];
      const length = fence[2].length;
      if (!inFence) {
        inFence = true;
        fenceMarker = marker;
        fenceLength = length;
      } else if (marker === fenceMarker && length >= fenceLength
          && line.slice(fence[0].length).trim() === '') {
        inFence = false;
      }
    }
    if (!inFence) {
      if (line.includes('MATH')) lastMathLineEnd = end;
      if (!line.trim()) boundary = end;
    }
    if (newline < 0) break;
    offset = newline + 1;
  }
  return lastMathLineEnd >= 0 && lastMathLineEnd > boundary ? 0 : boundary;
}

export interface StreamingPrefixSnapshot {
  readonly escaped: boolean;
  readonly sourcePrefix: string;
  readonly htmlPrefix: string;
}

/** True when ``snapshot`` may seed a frame rendering ``text``: same variant and
 * the new source still extends the exact stable prefix. */
export function streamingPrefixApplies(
  snapshot: StreamingPrefixSnapshot, text: string, escaped: boolean,
): boolean {
  return snapshot.escaped === escaped
    && snapshot.sourcePrefix.length > 0
    && text.startsWith(snapshot.sourcePrefix);
}
