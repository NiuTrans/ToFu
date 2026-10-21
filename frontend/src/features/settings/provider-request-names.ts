/** Provider wire-name planning and editing. Model identity never changes here.
 * Entry points are mounted by Settings; the existing owner draft/CAS owns writes. */
import type { ModelRoutingDocument } from '../../api/api-v1-model-routing.generated';
import type { ProviderTemplate } from './provider-template-picker';
import {
  applyRequestNameDraft,
  editableInheritedHeaders,
  requestNameDraftFrom,
  resolveRequestNameHeaders,
  type RequestNameDraft,
} from './provider-request-name-panel';
import { createButton } from '../../ui';
import { t } from '../../i18n';

/** Where one request name must be sent: its template face's Connection. */
export interface RequestNameFacePlan {
  base_url: string;
  protocol: string;
  face?: string;
  extra_headers: Record<string, string>;
  responses_profile?: string;
  inference_profile?: unknown;
}

function normalisedUrl(value?: string): string {
  return String(value ?? '').replace(/\/+$/, '');
}

/** Server ``migration._protocol`` spelling, so a staged Connection carries the
 *  value the template compiler would have written. */
export function requestNameProtocol(value?: string): string {
  const text = String(value ?? '').trim().toLowerCase();
  if (text === 'responses' || text === 'openai-responses' || text === 'openai_responses') {
    return 'openai_responses';
  }
  return ['anthropic', 'local', 'custom', 'openai'].includes(text) ? text : 'openai';
}

/** Map request names to the template face that serves each one.
 *
 * A wire id pinned by ``request_faces`` must travel over THAT face: aigc's
 * ``kunlunbedrock.*`` alias answers 200 only on ``/v1`` with
 * ``x-context-provider: kunlun`` and 400 ``配置不存在`` everywhere else. Adding
 * it onto the card's default Connection is exactly why an added request name
 * looked present but never worked. Unpinned names resolve to the template's
 * default face; names no recipe mentions are omitted so callers keep their
 * existing fallback.
 */
export function planRequestNameFaces(
  names: string[], urls: string[], templates: ProviderTemplate[],
): Record<string, RequestNameFacePlan> {
  const hosted = new Set(urls.map(normalisedUrl).filter(Boolean));
  const plans: Record<string, RequestNameFacePlan> = {};
  for (const name of names) {
    for (const template of templates) {
      const recipe = (template.offering_recipes ?? []).find(row =>
        row.model_id === name || (row.request_ids ?? []).includes(name));
      if (!recipe) continue;
      const faceName = (recipe.request_faces ?? {})[name];
      const face = faceName ? (template.faces ?? {})[faceName] : {
        base_url: template.base_url, protocol: template.protocol,
      };
      if (!face?.base_url) continue;
      // A card installed before the face existed hosts only the default URL;
      // it must still resolve its alias onto the missing face, which the
      // staging code then creates.
      if (!hosted.has(normalisedUrl(face.base_url))
          && !hosted.has(normalisedUrl(template.base_url))) continue;
      plans[name] = {
        base_url: normalisedUrl(face.base_url),
        protocol: requestNameProtocol(face.protocol),
        ...(faceName ? { face: faceName } : {}),
        extra_headers: { ...(face.extra_headers ?? {}) },
        ...(face.responses_profile ? { responses_profile: face.responses_profile } : {}),
        ...(face.inference_profile ? { inference_profile: face.inference_profile } : {}),
      };
      break;
    }
  }
  return plans;
}

export function suggestedRequestNames(modelId: string, urls: string[], templates: ProviderTemplate[]): string[] {
  const endpoints = new Set(urls.map(url => url.replace(/\/+$/, '')));
  const matches = templates.filter(template => endpoints.has((template.base_url ?? '').replace(/\/+$/, '')));
  const names = matches.flatMap(template => (template.offering_recipes ?? [])
    .filter(recipe => recipe.model_id === modelId)
    .flatMap(recipe => recipe.request_ids?.length ? recipe.request_ids : [modelId]));
  return [...new Set(names.length ? names : [modelId])];
}

export function renameRequestName(document: ModelRoutingDocument, deploymentId: string, name: string): void {
  const row = document.deployments.find(row => row.deployment_id === deploymentId);
  if (!row) return;
  name = name.trim();
  if (!name || name.length > 256 || /[\u0000-\u001f]/.test(name)) throw new Error(t('settings.requestNameInvalid'));
  const offering = document.offerings.find(item => item.offering_id === row.offering_id);
  const siblings = new Set(document.offerings.filter(item => item.provider_access_id === offering?.provider_access_id)
    .map(item => item.offering_id));
  if (document.deployments.some(item => item !== row && siblings.has(item.offering_id) && item.wire_model_id === name)) {
    throw new Error(t('settings.requestNameDuplicate'));
  }
  const old = row.wire_model_id;
  if (old === name) return;
  for (const credential of document.credentials.filter(item => item.provider_access_id === offering?.provider_access_id)) {
    for (const grant of credential.authorization.models) {
      if (grant.creator_id === offering?.model?.creator_id && grant.model_id === offering?.model?.model_id
          && grant.excluded_wire_model_ids?.includes(old)) {
        grant.excluded_wire_model_ids = [...new Set(grant.excluded_wire_model_ids.map(id => id === old ? name : id))];
      }
    }
  }
  row.wire_model_id = name;
  row.probe_status = 'unprobed';
}

/** Headers a request name sends because its Connection sends them.
 *
 * A Connection header reaches every wire routed through it, so this is the
 * truthful answer to "what does this request name send". */
export function connectionHeadersFor(
  document: ModelRoutingDocument, connectionId: string | undefined,
): Record<string, string> {
  const connection = (document.connections ?? [])
    .find(item => item.connection_id === connectionId);
  return { ...((connection?.extra_headers ?? {}) as Record<string, string>) };
}

/** Request names other than ``deployment`` routed through its Connection. */
export function otherRequestNamesOnConnection(
  document: ModelRoutingDocument, deployment: { deployment_id: string; connection_id: string },
): number {
  return (document.deployments ?? []).filter(item =>
    item.connection_id === deployment.connection_id
    && item.deployment_id !== deployment.deployment_id).length;
}

export function mountRequestNameEditors(root: HTMLElement, ports: {
  document: () => ModelRoutingDocument;
  /** Opens the request-name panel and resolves the submitted draft, or null.
   *  ``context`` carries the Connection's headers and how many other request
   *  names share that Connection, so the panel can show the effective route
   *  and refuse a removal it cannot honor. */
  edit: (
    initial: RequestNameDraft,
    context: { inherited: Record<string, string>; sharedWith: number },
  ) => Promise<RequestNameDraft | null>;
  /** Bind the row to the Connection that serves its (possibly renamed) wire. */
  route?: (deployment: { deployment_id: string; offering_id: string; wire_model_id: string }, wireId: string) => Promise<void> | void;
  alert: (message: string) => void;
  render: () => void;
}): void {
  const document = ports.document();
  for (const header of root.querySelectorAll<HTMLElement>('.is-logical[data-offering]')) {
    const offering = document.offerings[Number(header.dataset.offering)];
    const model = document.models.find(row => row.creator_id === offering?.model?.creator_id && row.model_id === offering?.model?.model_id);
    if (model?.registration_state && model.registration_state !== 'ready') {
      const chip = root.ownerDocument.createElement('span');
      chip.className = 'stg-mx-chip pending';
      chip.textContent = t(model.registration_state === 'disabled' ? 'modelCatalog.registrationDisabled' : 'modelCatalog.registrationPending');
      chip.title = (model.registration_issues ?? []).join('; ');
      header.querySelector('.stg-mx-model')?.append(chip);
    }
  }
  for (const host of root.querySelectorAll<HTMLElement>('[data-request-deployment]')) {
    const id = host.dataset.requestDeployment!;
    const row = ports.document().deployments.find(row => row.deployment_id === id);
    if (!row) continue;
    const button = createButton({ label: row.wire_model_id, title: t('settings.requestNameEditHeaders'), variant: 'ghost', size: 'sm',
      onClick: async () => {
        const draft = ports.document();
        const old = row.wire_model_id;
        const sourceConnectionId = row.connection_id;
        const next = await ports.edit(requestNameDraftFrom(row), {
          inherited: editableInheritedHeaders(
            connectionHeadersFor(draft, sourceConnectionId)),
          sharedWith: otherRequestNamesOnConnection(draft, row),
        });
        if (next === null || draft !== ports.document() || row.wire_model_id !== old) return;
        try {
          const applied = applyRequestNameDraft(draft, next, {
            deploymentId: id, offeringId: row.offering_id,
          });
          // Removing a header the request name inherits can only mean "stop
          // sending it from the Connection"; a value that still matches the
          // Connection stays there rather than being duplicated as an
          // override. The panel refuses the removal on a shared Connection,
          // and this re-checks because routing runs between the two.
          const write = resolveRequestNameHeaders(
            applied.headers,
            connectionHeadersFor(draft, sourceConnectionId));
          if (write.removed.length) {
            const source = (draft.connections ?? [])
              .find(item => item.connection_id === sourceConnectionId);
            const others = otherRequestNamesOnConnection(draft, row);
            if (!source) throw new Error(t('settings.requestNameInheritedOrphan', {
              names: write.removed.join(', '),
            }));
            if (others > 0) throw new Error(t('settings.requestNameInheritedShared', {
              count: others, names: write.removed.join(', '),
            }));
            const headers: Record<string, string> = {
              ...(source.extra_headers ?? {}),
            };
            for (const name of write.removed) delete headers[name];
            source.extra_headers = headers;
          }
          // Headers are edited in place: renameRequestName owns the id (grants,
          // duplicates, evidence reset), this owns the selectors.
          renameRequestName(draft, id, applied.wireModelId);
          // Renaming onto a face-pinned wire must also move the row there;
          // otherwise the renamed id stays on a Connection that cannot serve
          // it, and the id change looks like a broken save.
          if (ports.route) await ports.route(row, applied.wireModelId);
          const finalWrite = resolveRequestNameHeaders(
            applied.headers, connectionHeadersFor(draft, row.connection_id));
          if (Object.keys(finalWrite.deployment).length) {
            row.extra_headers = finalWrite.deployment;
          } else delete row.extra_headers;
          ports.render();
        } catch (error) { ports.alert(String((error as Error).message)); }
      } });
    host.replaceChildren(button);
    // A request name carrying selectors looks identical to one that does not,
    // yet only the former can reach a face behind a provider selector. Count
    // the EFFECTIVE set (Connection plus request name): an alias whose
    // selector lives on its Connection was marked header-less, which reads
    // as "the header was lost" while every request carried it.
    const effectiveHeaders = {
      ...connectionHeadersFor(ports.document(), row.connection_id),
      ...((row.extra_headers ?? {}) as Record<string, string>),
    };
    if (Object.keys(effectiveHeaders).length) {
      const badge = root.ownerDocument.createElement('span');
      badge.className = 'stg-mx-chip selector';
      badge.textContent = t('settings.requestNameHeaderBadge');
      badge.title = Object.keys(effectiveHeaders).join(', ');
      host.append(badge);
    }
    if (row.registration_issues?.length) {
      const chip = root.ownerDocument.createElement('span');
      chip.className = 'stg-mx-chip pending';
      chip.textContent = t('modelCatalog.registrationPending');
      chip.title = row.registration_issues.join('; ');
      host.append(chip);
    }
  }
}
