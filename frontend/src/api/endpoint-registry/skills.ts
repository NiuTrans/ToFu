/** Skills wire facade. Every management command carries its selected source. */
import type { EndpointPorts } from './contracts';
export interface InstalledSkillOption {id: string; name: string; scope: string; enabled: boolean; eligible: boolean; ineligible_reasons?: string[]}
/** Runtime selection shares the installed catalog, with project precedence. */
export function installedSkillOptions(get: EndpointPorts['get'], projectPath = '', signal?: AbortSignal) {
  return get<{skills: InstalledSkillOption[]}>('/api/v4/skills', {
    query: {scope: 'all', project_path: projectPath}, signal,
  });
}
export interface SkillContext {
  project_path?: string | null;
  skill_ref?: string;
}
export function createSkillsEndpoints(ports: EndpointPorts) {
  const { request, get, post, put, del } = ports;
  const query = (context?: SkillContext) => ({
    project_path: context?.project_path || '',
    ...(context?.skill_ref ? { skill_ref: context.skill_ref } : {}),
  });
  return {
    available: (projectPath = '', signal?: AbortSignal) => installedSkillOptions(get, projectPath, signal),
    list: (scope: string, projectPath = '') => get('/api/v4/skills', {
      query: { scope: scope || 'all', project_path: projectPath, include_shadowed: '1' },
    }),
    uninstall: (id: string, context?: SkillContext) => del<Response | null>(`/api/v4/skills/${encodeURIComponent(id)}`, { query: query(context), parse: 'response' }),
    toggle: (id: string, context?: SkillContext) => post<Response | null>(`/api/v4/skills/${encodeURIComponent(id)}/toggle`, undefined, { query: query(context), parse: 'response' }),
    files: (id: string, context?: SkillContext) => get(`/api/v4/skills/${encodeURIComponent(id)}/files`, { query: query(context) }),
    install: (formData: FormData, projectPath = '') => request<Response | null>('/api/v4/skills/install', { method: 'POST', body: formData, query: { project_path: projectPath }, parse: 'response' }),
    catalog: (projectPath = '') => get('/api/v4/skills/catalog', { query: { project_path: projectPath } }),
    catalogSearch: (text: string, limit: number, projectPath = '') => get('/api/v4/skills/catalog/search', { query: { q: text, limit: limit || 8, project_path: projectPath } }),
    catalogInstall: (skillId: string, scope: string, sourceRevision: string, overwrite: boolean, projectPath = '') => post<Response | null>('/api/v4/skills/catalog/install', { skill_id: skillId, scope: scope || 'global', source_revision: sourceRevision || '', overwrite: Boolean(overwrite) }, { query: { project_path: projectPath }, parse: 'response' }),
    envStatus: (id: string, context?: SkillContext) => get(`/api/v4/skills/${encodeURIComponent(id)}/env`, { query: query(context) }),
    envSet: (id: string, name: string, value: string, context?: SkillContext) => put<Response | null>(`/api/v4/skills/${encodeURIComponent(id)}/env`, { name, value }, { query: query(context), parse: 'response' }),
    envDelete: (id: string, name: string, context?: SkillContext) => del<Response | null>(`/api/v4/skills/${encodeURIComponent(id)}/env/${encodeURIComponent(name)}`, { query: query(context), parse: 'response' }),
    setScope: (id: string, scope: string, context?: SkillContext, targetProjectPath?: string) => post<Response | null>(`/api/v4/skills/${encodeURIComponent(id)}/scope`, { scope, ...(targetProjectPath ? { target_project_path: targetProjectPath } : {}) }, { query: query(context), parse: 'response' }),
  };
}
