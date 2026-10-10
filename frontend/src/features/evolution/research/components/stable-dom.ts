/** Research-only DOM patching. Existing controls and open disclosures retain identity. */
export function patchResearchDom(host: Element, html: string): void {
  const template = document.createElement('template');
  template.innerHTML = html;
  reconcile(host, template.content);
}

function isElement(node: Node): node is Element { return node.nodeType === 1; }
function key(node: Node): string {
  if (!isElement(node)) return '';
  return node.id || node.getAttribute('data-rsw-field') || node.getAttribute('data-rs-panel') || node.getAttribute('data-rs-tab') || node.getAttribute('data-rs-kind') || '';
}
function reconcile(host: Node, source: Node): void {
  const wanted = [...source.childNodes];
  wanted.forEach((fresh, index) => {
    let current = host.childNodes[index];
    const id = key(fresh);
    if (id && key(current || fresh) !== id) {
      const match = [...host.childNodes].find((node) => key(node) === id);
      if (match) { host.insertBefore(match, current || null); current = match; }
    }
    if (!current) { host.appendChild(fresh.cloneNode(true)); return; }
    if (current.nodeType !== fresh.nodeType || current.nodeName !== fresh.nodeName || key(current) !== id) {
      host.replaceChild(fresh.cloneNode(true), current); return;
    }
    if (!isElement(current) || !isElement(fresh)) {
      if (current.textContent !== fresh.textContent) current.textContent = fresh.textContent;
      return;
    }
    // Workspace is a separate revision/CAS owner; telemetry must never repaint it.
    if (current.id === 'researchWorkspace') return;
    // The literature graph owns its canvas/legend/detail subtree; repaints
    // push new data through LiteratureGraph.update instead.
    if (current.id === 'researchLiteratureGraph' || current.id === 'researchExperimentHistory' || current.id === 'researchObservationRecovery') return;
    const editor = current.matches('input, textarea, select');
    for (const attr of [...current.attributes]) {
      if (attr.name === 'open' || (editor && attr.name === 'value')) continue;
      if (!fresh.hasAttribute(attr.name)) current.removeAttribute(attr.name);
    }
    for (const attr of [...fresh.attributes]) {
      if (attr.name === 'open' || (editor && attr.name === 'value')) continue;
      if (current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name, attr.value);
    }
    // The workspace may show/hide the host; PDF.js owns its descendants.
    if (!editor && current.id !== 'researchManuscriptReaderHost') reconcile(current, fresh);
  });
  while (host.childNodes.length > wanted.length) host.removeChild(host.lastChild!);
}
