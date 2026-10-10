/* Owner-scoped DOMPurify IN_PLACE experiment, never installed globally. */
globalThis.createInplaceSanitizerExperiment = function ({document, sanitizer}) {
  const inert = document.implementation.createHTMLDocument('');
  const scratch = inert.createElement('div');
  let closed = false, active = false;
  return {
    sanitize(html) {
      if (closed) throw Error('in-place sanitizer disposed');
      if (active) throw Error('recursive in-place sanitation refused');
      active = true;
      try {
        scratch.innerHTML = html;
        const result = sanitizer.sanitize(scratch, {IN_PLACE: true});
        if (result !== scratch) throw Error('sanitizer did not honor in-place node contract');
        return scratch.innerHTML;
      } finally {
        scratch.replaceChildren();
        active = false;
      }
    },
    snapshot() { return {closed, active, retainedNodes: scratch.childNodes.length}; },
    dispose() { closed = true; scratch.replaceChildren(); },
  };
};

/* Full-document parser variant; still experimental and exact-oracle gated. */
globalThis.createInplaceDocumentSanitizerExperiment = function ({document, sanitizer}) {
  const inert = document.implementation.createHTMLDocument('');
  let closed = false, active = false;
  function clear() { inert.head?.replaceChildren(); inert.body?.replaceChildren(); }
  return {
    sanitize(html) {
      if (closed) throw Error('in-place document sanitizer disposed');
      if (active) throw Error('recursive in-place sanitation refused');
      active = true;
      try {
        inert.open(); inert.write(html); inert.close();
        const body = inert.body;
        // Match the installed DOMPurify string parser's explicit preservation
        // of leading whitespace; HTML document parsing discards that prefix.
        const leading = html.match(/^[\r\n\t ]+/)?.[0];
        if (body && html && leading) body.insertBefore(inert.createTextNode(leading), body.firstChild);
        if (!body || sanitizer.sanitize(body, {IN_PLACE: true}) !== body) {
          throw Error('sanitizer did not honor in-place body contract');
        }
        return body.innerHTML;
      } finally { clear(); active = false; }
    },
    snapshot() { return {closed, active, retainedNodes: (inert.head?.childNodes.length ?? 0)+(inert.body?.childNodes.length ?? 0)}; },
    dispose() { closed = true; clear(); },
  };
};
