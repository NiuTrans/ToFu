/** Repair ambiguous Markdown table pipes while preserving column evidence.
 * Entry points: repairTablePipes. Dependencies: none. No ambient application state.
 */
export function repairTablePipes(text: string): string {
  const lines = text.split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    if (!/^\s*\|/.test(lines[i])) { out.push(lines[i]); i++; continue; }
    // Collect consecutive table lines
    const tStart = i;
    while (i < lines.length && /^\s*\|/.test(lines[i])) i++;
    const tLines = lines.slice(tStart, i);
    if (tLines.length < 2) { out.push(...tLines); continue; }
    // Find separator row (within first 3 lines)
    let sepIdx = -1, expectedCols = -1;
    for (let s = 0; s < Math.min(tLines.length, 3); s++) {
      const cells = tLines[s].trim().replace(/^\|/, '').replace(/\|$/, '').split('|');
      if (cells.length > 0 && cells.every(c => /^\s*:?-+:?\s*$/.test(c))) {
        sepIdx = s; expectedCols = cells.length; break;
      }
    }
    if (sepIdx === -1 || expectedCols < 1) { out.push(...tLines); continue; }

    // Phase 1: Replace \| with &#124; in non-separator rows.
    // The LLM often writes \| intending a literal pipe, but marked.js
    // doesn't treat \| as an escape inside table rows.
    for (let t = 0; t < tLines.length; t++) {
      if (t !== sepIdx) {
        tLines[t] = tLines[t].replace(/\\\|/g, '&#124;');
      }
    }

    // Build a reference pattern from rows with correct column count.
    // For each column, record how often it's empty across correct rows.
    const emptyRate = new Array(expectedCols).fill(0);
    let correctRowCount = 0;
    for (let t = 0; t < tLines.length; t++) {
      if (t === sepIdx) continue;
      const inner = tLines[t].trim().replace(/^\|/, '').replace(/\|$/, '');
      const cells = inner.split('|');
      if (cells.length === expectedCols) {
        correctRowCount++;
        for (let c = 0; c < expectedCols; c++) {
          if (!cells[c] || !cells[c].trim()) emptyRate[c]++;
        }
      }
    }

    // Process each row
    for (let t = 0; t < tLines.length; t++) {
      if (t === sepIdx) { out.push(tLines[t]); continue; }
      const trimmed = tLines[t].trim();
      const hasLeading = trimmed.startsWith('|');
      const hasTrailing = trimmed.endsWith('|');
      let inner = trimmed;
      if (hasLeading) inner = inner.slice(1);
      if (hasTrailing) inner = inner.slice(0, -1);
      const cells = inner.split('|');
      if (cells.length <= expectedCols) { out.push(tLines[t]); continue; }

      // Phase 2: Too many columns — try each merge position, pick best.
      const excess = cells.length - expectedCols;
      let bestMerge: string[] | null = null, bestScore = -Infinity;
      for (let pos = 0; pos <= cells.length - excess - 1; pos++) {
        const merged = cells.slice(pos, pos + excess + 1);
        const trial = [
          ...cells.slice(0, pos),
          merged.join('&#124;'),
          ...cells.slice(pos + excess + 1)
        ];
        let score = 0;
        // Strong penalty: if the merge window absorbs a genuinely empty cell,
        // it's almost certainly merging across a real column boundary.
        const mergedHasEmpty = merged.some(c => !c || !c.trim());
        if (mergedHasEmpty) score -= 10;
        // Pattern matching against correct rows' empty/non-empty distribution
        for (let c = 0; c < expectedCols; c++) {
          const tEmpty = !trial[c] || !trial[c].trim();
          if (correctRowCount > 0) {
            const colUsuallyEmpty = emptyRate[c] > correctRowCount * 0.5;
            if (colUsuallyEmpty === tEmpty) score += 3;
          }
          // Penalize merged cells that end with backslash (broken \| escape)
          if (!tEmpty && trial[c].trimEnd().endsWith('\\')) score -= 2;
        }
        // Tiebreaker: prefer interior merges — the first and last cells
        // in LLM tables are rarely the ones with accidental pipes.
        if (pos > 0 && pos + excess < cells.length - 1) score += 0.5;
        if (score > bestScore) { bestScore = score; bestMerge = trial; }
      }
      if (bestMerge && bestMerge.length === expectedCols) {
        out.push((hasLeading ? '|' : '') + bestMerge.join('|') + (hasTrailing ? '|' : ''));
      } else {
        out.push(tLines[t]); // fallback: unchanged
      }
    }
  }
  return out.join('\n');
}

