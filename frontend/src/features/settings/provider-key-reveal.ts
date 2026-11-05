/** Temporary Provider credential plaintext and edit presentation.
 * The routing authority owns pending replacements and the audited reveal API
 * owns secret access. This view uses the shared lifecycle scope; retiring it
 * masks detached inputs and releases DOM/plaintext before pending reads settle.
 */
import {createLifecycleScope, type LifecycleScope} from '../../lifecycle';
import type {ModelRoutingCredentialMetadata, ModelRoutingDocument} from '../../api/api-v1-model-routing.generated';

export interface ProviderKeyBinding {
  card: HTMLElement;
  input: HTMLInputElement;
  button: HTMLButtonElement;
}

interface RevealPorts {
  document(): Partial<ModelRoutingDocument> | null;
  credential(providerId: string, credentialId: string): ModelRoutingCredentialMetadata | null;
  pendingSecret(credentialId: string): string;
  stageSecret(credentialId: string, value: string | null): void;
  reveal(credentialId: string): Promise<{secret?: unknown} | null>;
  paint(binding: ProviderKeyBinding, revealed: boolean): void;
  failed(): void;
}

interface Entry {
  providerId: string;
  credentialId: string;
  credential: ModelRoutingCredentialMetadata | null;
  binding: ProviderKeyBinding | null;
  committedValue: string | null;
  revealed: boolean;
  pending: boolean;
}

interface Session {
  root: HTMLElement | null;
  document: Partial<ModelRoutingDocument> | null;
  scope: LifecycleScope | null;
  entries: Map<HTMLElement, Entry>;
  focusRevision: number;
}

/** Client hint for an unsaved replacement; persisted hints remain server-owned. */
export function maskProviderCredentialSecret(value: string): string {
  const normalized = String(value || '').trim();
  if (!normalized) return '';
  if (normalized.length <= 8) return '****';
  return normalized.slice(0, 4) + '*'.repeat(Math.min(normalized.length - 8, 40)) + normalized.slice(-4);
}

function displaySecret(secret: string): string {
  try {
    const value = JSON.parse(secret);
    if (value && value.format === 'tofu.credential-secret/v1') {
      const key = String(value.api_key || '').trim();
      if (key) return key;
    }
  } catch { /* Raw keys are not JSON envelopes. */ }
  return secret;
}

class ProviderKeyReveal {
  private current: Session | null = null;

  constructor(private readonly ports: RevealPorts) {}

  toggle(button: HTMLButtonElement): Promise<void> | void {
    if (!button || typeof button.closest !== 'function') return;
    const entry = this.entry(button);
    const session = this.current;
    if (!entry || !session || !this.active(session, entry)) return;
    if (entry.pending) return;
    if (entry.revealed) {
      this.releaseEntry(session, entry);
      if (!session.entries.size) this.dispose();
      return;
    }
    const staged = this.ports.pendingSecret(entry.credentialId);
    if (staged) {
      // A staged replacement is not evidence of the committed server value.
      this.show(session, entry, staged, null, session.focusRevision);
      return;
    }
    entry.pending = true;
    entry.binding!.button.disabled = true;
    return this.read(session, entry, session.focusRevision);
  }

  edit(input: HTMLInputElement): void {
    if (!input || typeof input.closest !== 'function') return;
    const session = this.current;
    const card = input.closest<HTMLElement>('.stg-key-card');
    const entry = card && session?.entries.get(card);
    if (!session || !entry || !this.active(session, entry) || input.readOnly) return;
    const value = input.value;
    const staged = !!value.trim() && (entry.committedValue === null || value !== entry.committedValue);
    this.ports.stageSecret(entry.credentialId, staged ? value : null);
    input.setAttribute('data-masked', staged
      ? maskProviderCredentialSecret(value) : (input.getAttribute('data-stored') || '••••••••'));
    card!.classList.toggle('stg-key-card--staged', staged);
  }

  /** Only the routing owner can transfer this view after adopting a complete
   * committed document. Keep focus and newer edits; a changed secret reference
   * invalidates the old plaintext baseline and any in-flight reveal.
   */
  adoptDocument(previous: object): void {
    const session = this.current;
    const document = this.ports.document();
    if (!session || session.document !== previous || !document) return;
    session.document = document;
    for (const entry of session.entries.values()) {
      const credential = this.ports.credential(entry.providerId, entry.credentialId);
      const binding = entry.binding;
      if (!credential || credential.kind !== 'api_key' || !binding) {
        this.releaseEntry(session, entry);
        continue;
      }
      const pending = this.ports.pendingSecret(entry.credentialId);
      binding.input.setAttribute('data-stored', credential.key_hint || '••••••••');
      binding.input.setAttribute('data-masked', pending
        ? maskProviderCredentialSecret(pending) : (credential.key_hint || '••••••••'));
      binding.card.classList.toggle('stg-key-card--staged', !!pending);
      if (entry.pending) {
        this.releaseEntry(session, entry);
        continue;
      }
      if (entry.credential?.secret_reference !== credential.secret_reference) entry.committedValue = null;
      entry.credential = credential;
    }
    if (!session.entries.size) this.dispose();
  }

  dispose(): void {
    const session = this.current;
    if (!session) return;
    this.current = null;
    const scope = session.scope;
    session.scope = null;
    session.root = null;
    session.document = null;
    scope?.destroy();
    for (const entry of session.entries.values()) this.releaseEntry(session, entry);
  }

  private entry(button: HTMLButtonElement): Entry | null {
    const card = button.closest<HTMLElement>('.stg-key-card');
    const input = card?.querySelector<HTMLInputElement>('.stg-keys-input');
    const root = card?.closest<HTMLElement>('#stgProviderList');
    const document = this.ports.document();
    if (!card || !input || !root?.isConnected || !document) return null;
    const providerId = card.dataset.providerId || '';
    const credentialId = card.dataset.credentialId || '';
    const credential = this.ports.credential(providerId, credentialId);
    if (!credential || credential.kind !== 'api_key') return null;
    if (this.current?.root !== root || this.current.document !== document) {
      this.dispose();
      this.current = this.session(root, document);
    }
    const entries = this.current!.entries;
    let entry = entries.get(card);
    if (entry && entry.credential !== credential) {
      this.releaseEntry(this.current!, entry);
      entry = undefined;
    }
    if (!entry) {
      entry = {providerId, credentialId, credential, binding: {card, input, button},
        committedValue: null, revealed: false, pending: false};
      entries.set(card, entry);
    }
    return entry;
  }

  private session(root: HTMLElement, document: Partial<ModelRoutingDocument>): Session {
    const ownerWindow = root.ownerDocument.defaultView!;
    const scope = createLifecycleScope(ownerWindow);
    const session: Session = {root, document, scope, entries: new Map(), focusRevision: 0};
    scope.listen(root.ownerDocument, 'focusin', () => { session.focusRevision++; });
    const observer = new ownerWindow.MutationObserver(() => {
      if (this.current !== session) return;
      if (!session.root?.isConnected) { this.dispose(); return; }
      for (const entry of session.entries.values()) {
        if (!entry.binding?.card.isConnected || !session.root.contains(entry.binding.card)) {
          this.releaseEntry(session, entry);
        }
      }
      if (!session.entries.size) this.dispose();
    });
    observer.observe(root.ownerDocument.body, {childList: true, subtree: true});
    scope.add(() => observer.disconnect());
    return session;
  }

  private active(session: Session, entry: Entry): boolean {
    if (this.current !== session || !entry.binding) return false;
    if (session.document !== this.ports.document() || !session.root?.isConnected) {
      this.dispose();
      return false;
    }
    if (!entry.binding.card.isConnected || !session.root.contains(entry.binding.card) ||
        entry.credential !== this.ports.credential(entry.providerId, entry.credentialId)) {
      this.releaseEntry(session, entry);
      if (!session.entries.size) this.dispose();
      return false;
    }
    return true;
  }

  private async read(session: Session, entry: Entry, focusRevision: number): Promise<void> {
    // Only the releasable session/entry cross this await, never an input/card.
    let secret: unknown;
    try { secret = (await this.ports.reveal(entry.credentialId))?.secret; }
    catch { secret = null; }
    if (!this.active(session, entry)) return;
    entry.pending = false;
    entry.binding!.button.disabled = false;
    if (typeof secret !== 'string' || !secret) { this.ports.failed(); return; }
    const committed = displaySecret(secret);
    this.show(session, entry, this.ports.pendingSecret(entry.credentialId) || committed, committed, focusRevision);
  }

  private show(session: Session, entry: Entry, value: string, committed: string | null, focusRevision: number): void {
    if (!this.active(session, entry)) return;
    const binding = entry.binding!;
    const document = binding.input.ownerDocument;
    const active = document.activeElement;
    const focus = session.focusRevision === focusRevision &&
      (active === binding.button || active === binding.input || active === document.body);
    entry.revealed = true;
    entry.committedValue = committed;
    binding.input.value = value;
    binding.input.readOnly = false;
    this.ports.paint(binding, true);
    if (focus) { binding.input.focus(); binding.input.select(); }
  }

  private releaseEntry(session: Session, entry: Entry): void {
    const binding = entry.binding;
    entry.binding = null;
    entry.credential = null;
    entry.committedValue = null;
    entry.pending = false;
    entry.revealed = false;
    if (!binding) return;
    session.entries.delete(binding.card);
    binding.input.value = binding.input.getAttribute('data-masked') || '';
    binding.input.readOnly = true;
    // Erase the legacy property too when retiring an already-mounted card.
    delete (binding.input as HTMLInputElement & {__revealedSecret?: string}).__revealedSecret;
    binding.button.disabled = false;
    this.ports.paint(binding, false);
  }
}

export function createProviderKeyReveal(ports: RevealPorts): ProviderKeyReveal {
  return new ProviderKeyReveal(ports);
}
