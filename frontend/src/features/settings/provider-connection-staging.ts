/** Pure connection/grant preparation for Provider request names and model adds.
 * Generated routing rows own the schema; template planning owns face selection.
 * Callers publish these results only after their complete operation validates.
 */
import type {
  ModelRoutingConnection,
  ModelRoutingCredentialAuthorization,
} from '../../api/api-v1-model-routing.generated';
import type { RequestNameFacePlan } from './provider-request-names';

export interface ProviderConnectionStagingPorts {
  newId(prefix: string): string;
  disabledConnection(connection: ModelRoutingConnection): Error;
}

export interface ProviderFaceConnectionResolution {
  readonly previous: ModelRoutingConnection | null;
  readonly connection: ModelRoutingConnection;
}

export function resolveProviderFaceConnection(
  connections: ReadonlyArray<ModelRoutingConnection>, accessId: string,
  plan: RequestNameFacePlan | undefined, ports: ProviderConnectionStagingPorts,
): ProviderFaceConnectionResolution | null {
  if (!plan?.base_url) return null;
  const url = String(plan.base_url).replace(/\/$/, '');
  const previous = connections.find(row => row.provider_access_id === accessId &&
    String(row.base_url || '').replace(/\/$/, '') === url &&
    (!plan.face || !row.gateway_namespace || row.gateway_namespace === plan.face));
  if (previous?.enabled === false) throw ports.disabledConnection(previous);
  // Face values come from the existing template planner's normalized protocol
  // and generated provider recipe. This owner does not admit new catalog facts.
  const connection: ModelRoutingConnection = previous ? {...previous} : {
    connection_id: ports.newId('conn'), provider_access_id: accessId, base_url: url,
    protocol: (plan.protocol || 'openai') as ModelRoutingConnection['protocol'],
    enabled: true, priority: 100, extra_headers: {...plan.extra_headers},
  };
  if (!connection.protocol && plan.protocol) {
    connection.protocol = plan.protocol as ModelRoutingConnection['protocol'];
  }
  if (plan.face && !connection.gateway_namespace) connection.gateway_namespace = plan.face;
  if (plan.responses_profile && !connection.responses_profile) {
    connection.responses_profile = plan.responses_profile as ModelRoutingConnection['responses_profile'];
  }
  if (plan.inference_profile && !connection.inference_profile) {
    connection.inference_profile = plan.inference_profile as ModelRoutingConnection['inference_profile'];
  }
  const headers = connection.extra_headers || {};
  const missing = Object.entries(plan.extra_headers || {}).filter(([key]) => !(key in headers));
  connection.extra_headers = missing.length ? {...headers, ...Object.fromEntries(missing)} : headers;
  return {previous: previous || null, connection};
}

/** Add only connection access, keeping model denials and other policy intact. */
export function authorizeProviderConnection(
  authorization: ModelRoutingCredentialAuthorization | undefined, connectionId: string,
): ModelRoutingCredentialAuthorization {
  const current = authorization || {connection_ids: [], models: []};
  const ids = current.connection_ids || [];
  return ids.includes(connectionId) ? current : {...current, connection_ids: [...ids, connectionId]};
}
