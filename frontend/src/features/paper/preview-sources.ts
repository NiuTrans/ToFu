/** Overleaf project selection form. Parses identity only; the owned document
 * API validates the selected MCP capability and persists the project grant.
 */
import type { PaperArxivImportResultV1, PaperOverleafDocumentV1 } from '../../api/paper-build.generated';
import type { Translator } from '../../i18n';

function arxivId(value: string): string | null {
  const text = value.trim();
  const match = /(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+\/\d{7}(?:v\d+)?)/i.exec(text);
  return match?.[1] ?? null;
}

/** arXiv e-print import form: download → bounded extract → auto-selection,
 * all server-side; the form only forwards identity and reports the receipt.
 */
export function createArxivImportForm(tr: Translator,
  submit: (arxiv: string, report: (state: 'pending' | 'failed' | 'imported', detail?: string) => void)
    => Promise<PaperArxivImportResultV1 | null>,
  done: (result: PaperArxivImportResultV1) => void): HTMLElement {
  const form = document.createElement('form'); form.className = 'paper-preview-overleaf-form';
  const hint = document.createElement('p'); hint.textContent = tr('preview.arxivImportHint');
  const field = document.createElement('input'); field.type = 'text'; field.required = true;
  field.maxLength = 160; field.autocomplete = 'off'; field.spellcheck = false;
  field.autocapitalize = 'none'; field.inputMode = 'url';
  field.placeholder = 'arXiv:2301.12345';
  const status = document.createElement('p'); status.className = 'paper-preview-arxiv-status';
  const button = document.createElement('button'); button.type = 'submit';
  button.textContent = tr('preview.arxivImport');
  const identity = `paper-arxiv-${crypto.randomUUID()}`;
  const row = document.createElement('div'); row.className = 'paper-preview-source-field';
  const label = document.createElement('label'); label.textContent = 'arXiv';
  field.id = `${identity}-arxiv`; label.htmlFor = field.id;
  row.append(label, field);
  form.append(hint, row, button, status);
  field.oninput = () => field.setCustomValidity('');
  form.onsubmit = event => {
    event.preventDefault();
    const id = arxivId(field.value);
    if (!id) { field.setCustomValidity(tr('preview.invalidArxiv')); field.reportValidity(); return; }
    button.disabled = true; field.disabled = true;
    status.textContent = tr('preview.arxivImporting');
    const report = (state: 'pending' | 'failed' | 'imported', detail = '') => {
      if (state === 'pending') { status.textContent = detail || tr('preview.arxivImporting'); return; }
      button.disabled = false; field.disabled = false;
      status.textContent = detail;
    };
    void submit(id, report).then(result => {
      if (!result) { button.disabled = false; field.disabled = false; return; }
      status.textContent = tr('preview.arxivImported', {
        files: String(result.files), dir: result.directory });
      done(result);
    }).catch(() => {
      button.disabled = false; field.disabled = false;
      status.textContent = tr('preview.arxivImportFailed');
    });
  };
  return form;
}

function projectId(value: string): string | null {
  const text = value.trim();
  if (/^[a-f\d]{24}$/i.test(text)) return text.toLowerCase();
  try {
    const url = new URL(text);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return null;
    return /^\/project\/([a-f\d]{24})\/?$/i.exec(url.pathname)?.[1]?.toLowerCase() ?? null;
  } catch { return null; }
}

export function createOverleafSourceForm(servers: string[], tr: Translator,
  select: (document: PaperOverleafDocumentV1) => void): HTMLElement {
  if (!servers.length) {
    const message = document.createElement('p'); message.textContent = tr('preview.overleafUnavailable');
    return message;
  }
  const form = document.createElement('form'); form.className = 'paper-preview-overleaf-form';
  const connection = document.createElement('select');
  for (const name of servers) { const option = document.createElement('option'); option.value = name; option.textContent = name; connection.append(option); }
  const project = document.createElement('input'); project.type = 'text'; project.maxLength = 2048; project.required = true;
  project.autocomplete = 'off'; project.spellcheck = false; project.autocapitalize = 'none'; project.inputMode = 'url';
  const main = document.createElement('input'); main.type = 'text'; main.maxLength = 512;
  main.placeholder = tr('preview.projectMain'); main.autocomplete = 'off'; main.spellcheck = false; main.autocapitalize = 'none';
  const identity = `paper-overleaf-${crypto.randomUUID()}`;
  for (const [key, field] of [['preview.connection', connection], ['preview.project', project],
    ['preview.mainFile', main]] as const) {
    const row = document.createElement('div'); row.className = 'paper-preview-source-field';
    const label = document.createElement('label'); label.textContent = tr(key);
    field.id = `${identity}-${key}`; label.htmlFor = field.id;
    row.append(label, field); form.append(row);
  }
  const submit = document.createElement('button'); submit.type = 'submit'; submit.textContent = tr('preview.useProject'); form.append(submit);
  project.oninput = () => project.setCustomValidity('');
  form.onsubmit = event => {
    event.preventDefault();
    const id = projectId(project.value);
    if (!id) { project.setCustomValidity(tr('preview.invalidProject')); project.reportValidity(); return; }
    if (!servers.includes(connection.value)) return;
    select({ source: 'overleaf', project_id: id, mcp_server: connection.value,
      main_file: main.value.trim() || null, label: 'Overleaf' });
  };
  return form;
}
