/** skills endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createSkillsEndpoints(ports: EndpointPorts) {
  const { request, get, post, put, del } = ports;
  const skills = {
    list:           (scope: string)     => get('/api/v4/skills', { query: { scope: scope || 'all' } }),
    uninstall:      (id: string)        => del<Response | null>(`/api/v4/skills/${encodeURIComponent(id)}`, { parse: 'response' }),
    toggle:         (id: string)        => post<Response | null>(`/api/v4/skills/${encodeURIComponent(id)}/toggle`, undefined, { parse: 'response' }),
    files:          (id: string)        => get(`/api/v4/skills/${encodeURIComponent(id)}/files`),
    install:        (formData: FormData)  => request<Response | null>('/api/v4/skills/install', { method: 'POST', body: formData, parse: 'response' }),
    catalog:        ()          => get('/api/v4/skills/catalog'),
    catalogSearch:  (query: string, limit: number) => get('/api/v4/skills/catalog/search', { query: { q: query, limit: limit || 8 } }),
    catalogInstall: (skillId: string, scope: string, sourceRevision: number, overwrite: boolean) => post<Response | null>('/api/v4/skills/catalog/install', { skill_id: skillId, scope: scope || 'global', source_revision: sourceRevision || '', overwrite: Boolean(overwrite) }, { parse: 'response' }),
    // Per-skill env/key bindings (credential-vault backed; redacted status only)
    envStatus:      (id: string)        => get(`/api/v4/skills/${encodeURIComponent(id)}/env`),
    envSet:         (id: string, name: string, value: string) => put<Response | null>(`/api/v4/skills/${encodeURIComponent(id)}/env`, { name, value }, { parse: 'response' }),
    envDelete:      (id: string, name: string)  => del<Response | null>(`/api/v4/skills/${encodeURIComponent(id)}/env/${encodeURIComponent(name)}`, { parse: 'response' }),
    setScope:       (id: string, scope: string) => post<Response | null>(`/api/v4/skills/${encodeURIComponent(id)}/scope`, { scope }, { parse: 'response' }),
  };
  return skills;
}
