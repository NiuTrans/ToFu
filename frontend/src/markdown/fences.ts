/** Protect fenced code before text/math normalization.
 * Entry points: extractFencedBlocks, upgradeFenceIfNeeded. Dependencies: none. No ambient application state.
 */
export function extractFencedBlocks(text: string, codeStore: string[]): string {
  const lines = text.split("\n");
  const result = [];
  let i = 0;
  while (i < lines.length) {
    const open = lines[i].match(/^(`{3,}|~{3,})(.*)/);
    if (!open) {
      result.push(lines[i]);
      i++;
      continue;
    }
    const fChar = open[1][0],
      fLen = open[1].length,
      esc = fChar === "`" ? "`" : "~";
    const closeRe = new RegExp("^" + esc + "{" + fLen + ",}\\s*$");
    const innerOpenRe = new RegExp("^" + esc + "{3,}\\S");
    let closeIdx = -1,
      depth = 1;
    for (let j = i + 1; j < lines.length; j++) {
      if (closeRe.test(lines[j])) {
        depth--;
        if (depth === 0) {
          closeIdx = j;
          break;
        }
      } else if (innerOpenRe.test(lines[j])) depth++;
    }
    if (closeIdx === -1) {
      let lastClose = -1;
      for (let j = i + 1; j < lines.length; j++) {
        if (closeRe.test(lines[j])) lastClose = j;
        else if (lastClose !== -1 && innerOpenRe.test(lines[j])) {
          if (j > lastClose + 1) break;
        }
      }
      closeIdx = lastClose;
    }
    if (closeIdx === -1) {
      codeStore.push(lines.slice(i).join("\n"));
      result.push("\x02CODE" + (codeStore.length - 1) + "\x03");
      i = lines.length;
    } else {
      codeStore.push(lines.slice(i, closeIdx + 1).join("\n"));
      result.push("\x02CODE" + (codeStore.length - 1) + "\x03");
      i = closeIdx + 1;
    }
  }
  return result.join("\n");
}
export function upgradeFenceIfNeeded(block: string): string {
  const lines = block.split("\n");
  if (lines.length < 2) return block;
  const open = lines[0].match(/^(`{3,}|~{3,})(.*)/);
  if (!open) return block;
  const fChar = open[1][0],
    fLen = open[1].length,
    lang = open[2],
    esc = fChar === "`" ? "`" : "~";
  let maxInner = 0;
  for (let k = 1; k < lines.length - 1; k++) {
    const m = lines[k].match(new RegExp("^(" + esc + "{3,})"));
    if (m) maxInner = Math.max(maxInner, m[1].length);
  }
  if (maxInner >= fLen) {
    const nf = fChar.repeat(maxInner + 1);
    lines[0] = nf + lang;
    lines[lines.length - 1] = nf;
    return lines.join("\n");
  }
  return block;
}
