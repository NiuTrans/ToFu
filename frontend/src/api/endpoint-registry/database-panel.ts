/** databasePanel endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createDatabasePanelEndpoints(ports: EndpointPorts) {
  const { get, post } = ports;
  const databasePanel = {
    status: () => get('/api/v4/database/status', { onError: 'null' }),
    paths: (refresh: boolean) => get(
      '/api/v4/database/paths' + (refresh ? '?refresh=1' : ''), { onError: 'null' }),
    pathsApply: (payload: JsonObject) => post('/api/v4/database/paths', payload, { onError: 'throw' }),
    preferences: (payload: JsonObject) => post('/api/v4/database/preferences', payload, { onError: 'throw' }),
    backendSwitch: (payload: JsonObject) => post('/api/v4/database/backend', payload, { onError: 'throw' }),
    migration: () => get('/api/v4/database/migration', { onError: 'null' }),
    backupNow: (payload: JsonObject) => post('/api/v4/database/backup', payload || {}, { onError: 'throw' }),
  };
  return databasePanel;
}
