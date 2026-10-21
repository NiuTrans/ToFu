/** Request-name editor panel: the wire id PLUS the headers that select its route.
 *
 * WHY a panel instead of a text prompt
 * ===================================
 * A gateway can serve one wire spelling only when a selector header names its
 * serving provider (aigc's ``x-context-provider: kunlun`` answers 200, the same
 * id without it answers 400 ``配置不存在``), while another spelling of the SAME
 * Offering must NOT carry it. The selector therefore belongs to the request
 * name, not the account or the Connection — and a prompt that only accepts a
 * string cannot express it. Adding an alias through the old prompt produced a
 * cell that failed forever with no way to say why.
 *
 * The panel is the same form for ADD and EDIT so an existing request name can
 * gain (or lose) headers without delete-and-re-add, which would drop its probe
 * evidence and its position in the pool.
 *
 * Authority: the owner's staged model-routing draft. This module only mutates
 * that draft; the existing save/CAS path persists it.
 */
import type { ModelRoutingDocument } from '../../api/api-v1-model-routing.generated';
import { createButton } from '../../ui';
import { t } from '../../i18n';

/** Header names that would impersonate Tofu's own outbound auth. Mirrors
 *  ``lib/provider/provider_headers.py`` so the panel refuses what the server
 *  refuses, instead of letting the operator discover it on save. */
const RESERVED_HEADERS = new Set([
  'authorization', 'x-api-key', 'cookie', 'set-cookie', 'host',
  'content-length', 'transfer-encoding', 'proxy-authorization',
]);
const CREDENTIAL_MARKERS = [
  'auth', 'key', 'token', 'secret', 'password', 'cookie', 'credential',
];
const MAX_HEADERS = 16;
const MAX_VALUE_LENGTH = 2048;

export interface RequestNameDraft {
  wireModelId: string;
  headers: Array<{ name: string; value: string }>;
}

function isCredentialHeader(name: string): boolean {
  const lowered = String(name || '').trim().toLowerCase();
  return CREDENTIAL_MARKERS.some(marker => lowered.includes(marker));
}

/** Whether the panel may own a row for ``name``.
 *
 * A Connection may legally carry a credential-shaped header (only transport
 * names are reserved server-side), but it belongs to the encrypted credential:
 * pre-filling it here would open a form that can never be submitted.
 */
function isEditableHeader(name: string): boolean {
  const lowered = String(name || '').trim().toLowerCase();
  return !!lowered && !RESERVED_HEADERS.has(lowered) && !isCredentialHeader(lowered);
}

/** The Connection headers a request name may show and edit. */
export function editableInheritedHeaders(
  headers: Record<string, string> | undefined,
): Record<string, string> {
  const editable: Record<string, string> = {};
  for (const [name, value] of Object.entries(headers ?? {})) {
    const headerName = String(name || '').trim();
    if (isEditableHeader(headerName)) editable[headerName] = String(value ?? '');
  }
  return editable;
}

/** Rows for one request name: every inherited header plus its own, an own
 *  value winning on a shared name (dispatch merges the Connection UNDER the
 *  deployment).
 *
 * A name whose selector lives on the Connection used to open an EMPTY form,
 * which read as "the header was lost" even though every request carried it.
 * ``inherited`` marks the rows sourced from the Connection so an edit can be
 * written to the layer that actually owns the value.
 */
export function requestNameHeaderRows(
  own: Array<{ name: string; value: string }> | undefined,
  inherited: Record<string, string> | undefined,
): Array<{ name: string; value: string; inherited: boolean }> {
  type Row = { name: string; value: string; inherited: boolean };
  const rows: Row[] = [];
  const byName = new Map<string, Row>();
  for (const [name, value] of Object.entries(editableInheritedHeaders(inherited))) {
    const inheritedRow: Row = { name, value, inherited: true };
    rows.push(inheritedRow);
    byName.set(name.toLowerCase(), inheritedRow);
  }
  for (const item of own ?? []) {
    const headerName = String(item?.name || '').trim();
    const value = String(item?.value ?? '');
    if (!headerName) {
      rows.push({ name: '', value, inherited: false });
      continue;
    }
    const inheritedRow = byName.get(headerName.toLowerCase());
    if (inheritedRow) {
      // Same wire name on both layers: the deployment value is what ships.
      inheritedRow.value = value;
      inheritedRow.inherited = false;
      continue;
    }
    const row: Row = { name: headerName, value, inherited: false };
    rows.push(row);
    byName.set(headerName.toLowerCase(), row);
  }
  return rows;
}

/** What a submitted form means for the two layers that outbound headers have.
 *
 * Dispatch merges Connection headers UNDER the request name's, so a deployment
 * can add or override but never subtract. A value that still matches the
 * Connection therefore stays there (no redundant override), and a removed
 * inherited name is a request to stop sending it from the Connection — which
 * only its owner can decide when other request names share that Connection.
 */
export function resolveRequestNameHeaders(
  headers: Array<{ name: string; value: string }> | Record<string, string>,
  inherited: Record<string, string> | undefined,
): { deployment: Record<string, string>; removed: string[] } {
  const base = editableInheritedHeaders(inherited);
  // Dispatch merges the two dicts by EXACT key, so an override spelled with
  // different casing would emit both headers. HTTP names are case-insensitive,
  // so an override reuses the Connection's spelling and collapses to one.
  const baseByLower = new Map(
    Object.entries(base).map(([name, value]) => [name.toLowerCase(), { name, value }]));
  const own: Record<string, string> = {};
  const rows = Array.isArray(headers)
    ? headers
    : Object.entries(headers).map(([name, value]) => ({ name, value: String(value) }));
  for (const row of rows) {
    const headerName = String(row?.name || '').trim();
    if (!headerName) continue;
    own[headerName] = String(row?.value ?? '');
  }
  const deployment: Record<string, string> = {};
  for (const [name, value] of Object.entries(own)) {
    const shadowed = baseByLower.get(name.toLowerCase());
    if (shadowed && shadowed.value === value) continue;
    deployment[shadowed ? shadowed.name : name] = value;
  }
  const submitted = new Set(Object.keys(own).map(name => name.toLowerCase()));
  const removed = Object.keys(base).filter(name => !submitted.has(name.toLowerCase()));
  return { deployment, removed };
}

/** Validate a draft against the server's own header rules.
 *
 * Returns a localized message, or '' when the draft is acceptable. Rejecting
 * here is not duplicate validation for its own sake: a credential-named header
 * is refused by ``sanitise_extra_headers`` at save time, and a save-time
 * refusal after a long form is the failure this panel exists to prevent.
 */
export function validateRequestNameDraft(draft: RequestNameDraft): string {
  const name = String(draft.wireModelId || '').trim();
  if (!name) return t('settings.requestNameInvalid');
  if (name.length > 256 || /[\u0000-\u001f]/.test(name)) {
    return t('settings.requestNameInvalid');
  }
  if (draft.headers.length > MAX_HEADERS) {
    return t('settings.requestNameHeadersTooMany');
  }
  const seen = new Set<string>();
  for (const row of draft.headers) {
    const headerName = String(row.name || '').trim();
    const value = String(row.value || '');
    if (!headerName && !value) continue;
    if (!headerName) return t('settings.requestNameHeaderNameRequired');
    if (headerName.length > 256 || /[\u0000-\u001f]/.test(headerName)) {
      return t('settings.requestNameHeaderInvalid');
    }
    const lowered = headerName.toLowerCase();
    if (RESERVED_HEADERS.has(lowered)) {
      return t('settings.requestNameHeaderReserved', { name: headerName });
    }
    if (isCredentialHeader(headerName)) {
      return t('settings.requestNameHeaderCredential', { name: headerName });
    }
    if (value.length > MAX_VALUE_LENGTH || /[\u0000-\u001f]/.test(value)) {
      return t('settings.requestNameHeaderInvalid');
    }
    if (seen.has(lowered)) {
      return t('settings.requestNameHeaderDuplicate', { name: headerName });
    }
    seen.add(lowered);
  }
  return '';
}

/** Normalize a validated draft into what the aggregate stores.
 *
 * Throws on an invalid draft or a wire id already used by a sibling Offering
 * in the same ProviderAccess — the server rejects both, and a staged draft
 * that cannot be saved is worse than a refused edit.
 */
export function applyRequestNameDraft(
  document: ModelRoutingDocument,
  draft: RequestNameDraft,
  target: { deploymentId: string | null; offeringId: string },
): { wireModelId: string; headers: Record<string, string> } {
  const error = validateRequestNameDraft(draft);
  if (error) throw new Error(error);
  const name = draft.wireModelId.trim();
  const headers: Record<string, string> = {};
  for (const row of draft.headers) {
    const headerName = String(row.name || '').trim();
    if (!headerName) continue;
    headers[headerName] = String(row.value || '');
  }
  const offering = document.offerings.find(
    item => item.offering_id === target.offeringId);
  const siblings = new Set(
    document.offerings
      .filter(item => item.provider_access_id === offering?.provider_access_id)
      .map(item => item.offering_id));
  const duplicate = document.deployments.some(item =>
    item.deployment_id !== target.deploymentId
    && siblings.has(item.offering_id)
    && item.wire_model_id === name);
  if (duplicate) throw new Error(t('settings.requestNameDuplicate'));
  return { wireModelId: name, headers };
}

function headerRowsFrom(
  deployment: { extra_headers?: Record<string, string> } | undefined,
) {
  return Object.entries(deployment?.extra_headers ?? {})
    .map(([name, value]) => ({ name, value: String(value) }));
}

/** Open the panel. Resolves the submitted draft, or null when cancelled.
 *
 * Returning a promise rather than taking an ``onSubmit`` callback keeps ONE
 * close path: cancel, Escape, backdrop click and confirm all settle the same
 * value, so a caller can never leave a stuck button behind.
 */
export function openRequestNamePanel(options: {
  host: HTMLElement;
  title: string;
  initial?: RequestNameDraft;
  /** Headers this request name inherits from its Connection. They render as
   *  the effective route and are editable here; an untouched value stays on
   *  the Connection. */
  inherited?: Record<string, string>;
  /** Other request names routed through the same Connection. That Connection
   *  is shared, so one name cannot stop sending an inherited header alone. */
  sharedWith?: number;
  /** Return a message to keep the panel open (e.g. duplicate request name). */
  apply?: (draft: RequestNameDraft) => string | void;
}): Promise<RequestNameDraft | null> {
  const doc = options.host.ownerDocument;
  const sharedWith = Math.max(0, Math.floor(Number(options.sharedWith) || 0));
  const inherited = editableInheritedHeaders(options.inherited);
  const initial: RequestNameDraft = {
    wireModelId: options.initial?.wireModelId ?? '',
    headers: (options.initial?.headers ?? []).map(row => ({ ...row })),
  };
  const shownRows = requestNameHeaderRows(initial.headers, inherited);
  if (!shownRows.length) shownRows.push({ name: '', value: '', inherited: false });

  // Settings-owned overlay. The application dialog stylesheet is deliberately
  // demand-loaded with the dialog controller, so reusing `.app-dialog-*` here
  // left this panel unstyled at the end of <body> until some other dialog had
  // been opened — the reported "form hangs off the bottom of the page".
  const overlay = doc.createElement('div');
  overlay.className = 'stg-rnp-overlay';
  overlay.setAttribute('data-tofu-request-name-panel', '');
  const card = doc.createElement('div');
  card.className = 'stg-rnp-card';
  card.setAttribute('role', 'dialog');
  card.setAttribute('aria-modal', 'true');
  card.setAttribute('aria-label', options.title);

  const title = doc.createElement('div');
  title.className = 'stg-rnp-title';
  title.textContent = options.title;
  card.append(title);

  const body = doc.createElement('div');
  body.className = 'stg-rnp-body';

  const nameLabel = doc.createElement('label');
  nameLabel.className = 'stg-rnp-label';
  nameLabel.textContent = t('settings.requestNamePanelName');
  const nameInput = doc.createElement('input');
  nameInput.className = 'stg-rnp-input';
  nameInput.type = 'text';
  nameInput.value = initial.wireModelId;
  nameInput.placeholder = t('settings.requestNamePanelNamePlaceholder');
  nameLabel.append(nameInput);
  body.append(nameLabel);

  const headersLabel = doc.createElement('div');
  headersLabel.className = 'stg-rnp-label';
  headersLabel.textContent = t('settings.requestNamePanelHeaders');
  const headersHint = doc.createElement('div');
  headersHint.className = 'stg-rnp-hint';
  headersHint.textContent = t('settings.requestNamePanelHeadersHint');
  body.append(headersLabel, headersHint);
  if (Object.keys(inherited).length) {
    const inheritedHint = doc.createElement('div');
    inheritedHint.className = 'stg-rnp-hint';
    inheritedHint.setAttribute('data-rnp-inherited-hint', '');
    inheritedHint.textContent = sharedWith > 0
      ? t('settings.requestNameInheritedHintShared', { count: sharedWith })
      : t('settings.requestNameInheritedHint');
    body.append(inheritedHint);
  }

  const rowsHost = doc.createElement('div');
  rowsHost.className = 'stg-rnp-headers';
  body.append(rowsHost);

  const errorLine = doc.createElement('div');
  errorLine.className = 'stg-rnp-error';
  errorLine.setAttribute('role', 'alert');

  const rows: Array<{
    name: HTMLInputElement;
    value: HTMLInputElement;
    node: HTMLElement;
  }> = [];

  function addRow(name = '', value = '', inheritedRow = false): void {
    const node = doc.createElement('div');
    node.className = 'stg-rnp-header-row' + (inheritedRow ? ' is-inherited' : '');
    const nameField = doc.createElement('input');
    nameField.type = 'text';
    nameField.className = 'stg-rnp-input stg-rnp-header-name';
    nameField.placeholder = t('settings.requestNameHeaderNamePlaceholder');
    nameField.value = name;
    const valueField = doc.createElement('input');
    valueField.type = 'text';
    valueField.className = 'stg-rnp-input stg-rnp-header-value';
    valueField.placeholder = t('settings.requestNameHeaderValuePlaceholder');
    valueField.value = value;
    let chip: HTMLElement | null = null;
    if (inheritedRow) {
      const mark = doc.createElement('span');
      mark.className = 'stg-rnp-inherited';
      mark.textContent = t('settings.requestNameHeaderInherited');
      mark.title = t('settings.requestNameInheritedChipTitle');
      chip = mark;
      // Editing the row makes it this request name's own header; the mark
      // must not keep claiming a value the Connection does not carry.
      const unmark = () => {
        mark.remove();
        node.classList.remove('is-inherited');
        nameField.removeEventListener('input', unmark);
        valueField.removeEventListener('input', unmark);
      };
      nameField.addEventListener('input', unmark);
      valueField.addEventListener('input', unmark);
    }
    const remove = createButton({
      label: '×',
      title: t('settings.requestNameHeaderRemove'),
      variant: 'ghost',
      size: 'sm',
      onClick: () => {
        const index = rows.findIndex(row => row.node === node);
        if (index >= 0) rows.splice(index, 1);
        node.remove();
      },
    });
    // The grid tracks are name / value / origin / remove: the chip must follow
    // the fields, or it reads as a label for the name input and the two inputs
    // collide in one track.
    node.append(nameField, valueField);
    if (chip) node.append(chip);
    node.append(remove);
    rowsHost.append(node);
    rows.push({ name: nameField, value: valueField, node });
  }

  for (const row of shownRows) addRow(row.name, row.value, row.inherited);

  const addHeader = createButton({
    label: t('settings.requestNameHeaderAdd'),
    title: t('settings.requestNameHeaderAdd'),
    variant: 'ghost',
    size: 'sm',
    onClick: () => addRow(),
  });
  body.append(addHeader, errorLine);
  card.append(body);

  const actions = doc.createElement('div');
  actions.className = 'stg-rnp-actions';

  return new Promise<RequestNameDraft | null>(resolve => {
    let settled = false;
    function settle(value: RequestNameDraft | null): void {
      if (settled) return;
      settled = true;
      overlay.remove();
      doc.removeEventListener('keydown', onKey);
      resolve(value);
    }
    function cancel(): void {
      settle(null);
    }
    function submit(): void {
      const draft: RequestNameDraft = {
        wireModelId: nameInput.value,
        headers: rows.map(row => ({ name: row.name.value, value: row.value.value })),
      };
      const invalid = validateRequestNameDraft(draft);
      if (invalid) {
        errorLine.textContent = invalid;
        return;
      }
      const { removed } = resolveRequestNameHeaders(draft.headers, inherited);
      if (removed.length && sharedWith > 0) {
        errorLine.textContent = t('settings.requestNameInheritedShared', {
          count: sharedWith, names: removed.join(', '),
        });
        return;
      }
      if (options.apply) {
        const rejection = options.apply(draft);
        if (rejection) {
          errorLine.textContent = rejection;
          return;
        }
      }
      settle(draft);
    }
    function onKey(event: KeyboardEvent): void {
      if (event.key === 'Escape') cancel();
      // Enter submits from the fields, the usual expectation for a single
      // short form; header rows keep it so multi-row editing stays fast.
      else if (event.key === 'Enter' && !event.shiftKey) {
        const target = event.target as HTMLElement | null;
        if (target && target.tagName === 'INPUT') {
          event.preventDefault();
          submit();
        }
      }
    }

    actions.append(
      createButton({ label: t('dialog.cancel'), variant: 'ghost', size: 'sm', onClick: cancel }),
      createButton({ label: t('dialog.ok'), variant: 'primary', size: 'sm', onClick: submit }),
    );
    card.append(actions);

    overlay.append(card);
    overlay.addEventListener('click', event => {
      if (event.target === overlay) cancel();
    });
    doc.addEventListener('keydown', onKey);
    doc.body.append(overlay);
    nameInput.focus();
    nameInput.select();
  });
}

/** Read a deployment's headers as panel rows (empty dict when none). */
export function requestNameDraftFrom(
  deployment: { wire_model_id: string; extra_headers?: Record<string, string> } | undefined,
): RequestNameDraft {
  return {
    wireModelId: deployment?.wire_model_id ?? '',
    headers: headerRowsFrom(deployment),
  };
}
