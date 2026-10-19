/* ===== migrated source: core/markdown.js ===== */
/* Runtime ports for the typed Markdown pipeline. Copy/fold controls below
 * retain the established delegated-action boundary; rendering policy and
 * cache lifetime are owned by frontend/src/markdown/. */
const markdownRenderer = createMarkdownRenderer({
  maximumCacheEntries: 300,
  getEnvironment: () => ({
    document: typeof document === 'undefined' ? null : document,
    parser: typeof marked === 'undefined' ? null : marked,
    sanitizer: typeof DOMPurify === 'undefined' ? null : DOMPurify,
    highlighter: typeof hljs === 'undefined' ? null : hljs,
    mathRenderer: typeof katex === 'undefined' ? null : katex,
    basePath: typeof BASE_PATH === 'undefined' ? '' : BASE_PATH,

    language: _i18nLang,
    text: t,
    projectActive: typeof projectState !== 'undefined' && Boolean(projectState && projectState.active),
  }),
  ensureMath: () => typeof _ensureKatex === 'function' ? _ensureKatex() : undefined,
  ensureHighlightLanguage: typeof ensureHljsLanguage === 'function' ? ensureHljsLanguage : undefined,
  canEnsureHighlightLanguage: typeof canEnsureHljsLanguage === 'function' ? canEnsureHljsLanguage : undefined,
});
function renderMarkdown(text, options) {
  return markdownRenderer.render(text, options);
}
function toggleCodeBlock(btn) {
  const pre = btn.closest("pre");
  const c = pre.getAttribute("data-collapsed") === "true";
  pre.setAttribute("data-collapsed", c ? "false" : "true");
  btn.textContent = c ? "Collapse" : "Expand";
}
function copyCode(btn) {
  const code = btn.closest("pre").querySelector("code").textContent;
  _safeClipboardWrite(code);
  btn.textContent = "Copied!";
  setTimeout(() => (btn.textContent = "Copy"), 1500);
}
function _cellToMarkdown(cell) {
  /* Reconstruct approximate markdown from a table cell's DOM,
   * preserving bold, italic, and inline code formatting. */
  let md = '';
  cell.childNodes.forEach(node => {
    if (node.nodeType === 3) { // text node
      md += node.textContent;
    } else if (node.nodeType === 1) { // element
      const tag = node.tagName.toLowerCase();
      const inner = node.textContent;
      if (tag === 'strong' || tag === 'b') {
        md += '**' + inner + '**';
      } else if (tag === 'em' || tag === 'i') {
        md += '*' + inner + '*';
      } else if (tag === 'code') {
        md += '`' + inner + '`';
      } else {
        md += inner;
      }
    }
  });
  return md.replace(/\|/g, '\\|').trim();
}
function copyTableMarkdown(btn) {
  const wrapper = btn.closest(".md-table-wrapper");
  const table = wrapper.querySelector("table");
  if (!table) return;
  const rows = table.querySelectorAll("tr");
  if (!rows.length) return;
  const lines = [];
  rows.forEach((tr, i) => {
    const cells = tr.querySelectorAll("th, td");
    const vals = Array.from(cells).map(c => _cellToMarkdown(c));
    lines.push("| " + vals.join(" | ") + " |");
    if (i === 0) {
      lines.push("| " + vals.map(() => "---").join(" | ") + " |");
    }
  });
  _safeClipboardWrite(lines.join("\n"));
  btn.textContent = "Copied!";
  setTimeout(() => (btn.textContent = "Copy"), 1500);
}
