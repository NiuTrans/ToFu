/** Automation management facade over the existing authenticated, abortable transport.
 * DTO authority: automation_management_v1.schema.json; mutation IDs are supplied
 * by the caller and remain stable across an explicit retry. No automatic retry.
 */
import { apiTransport, type ApiTransport, type RequestOptions } from './transport';
import type {
  AutomationManagementSkillSource, AutomationManagementSkillSourceUpdateRequest,
  AutomationManagementProject, AutomationManagementProjectPage, AutomationManagementProjectUpdateRequest,
  AutomationManagementActionPage, AutomationManagementDefinition,
  AutomationManagementDefinitionPage, AutomationManagementPublishRequest,
  AutomationManagementRevisionRequest, AutomationManagementStartRequest,
  AutomationManagementCancelRequest, AutomationManagementRun,
  AutomationManagementRunPage,
  AutomationManagementApproveRequest, AutomationManagementPresentationPage,
} from './automation.generated';

type Options = Pick<RequestOptions, 'signal' | 'timeout'>;
type PageOptions = Options & { limit?: number };
const base = '/api/v4/automations';
const definitionPath = (id: string) => `${base}/definitions/${encodeURIComponent(id)}`;
const runPath = (id: string) => `${base}/runs/${encodeURIComponent(id)}`;

export function createAutomationManagementClient(transport: ApiTransport = apiTransport) {
  return {
    projects: ({limit,after,...options}: PageOptions & {after?: string} = {}) =>
      transport.request<AutomationManagementProjectPage>(`${base}/projects`,
        {...options,method:'GET',query:{limit,after}}),
    project: (id: string, options: Options = {}) =>
      transport.request<AutomationManagementProject>(`${base}/projects/${encodeURIComponent(id)}`,
        {...options,method:'GET'}),
    updateProject: (body: AutomationManagementProjectUpdateRequest, options: Options = {}) =>
      transport.request<AutomationManagementProject>(`${base}/projects`,{...options,method:'PUT',json:body}),
    presentations: (conversationId: string, attemptId: string, { limit, after, ...options }: PageOptions & { after?: string } = {}) =>
      transport.request<AutomationManagementPresentationPage>(
        `${base}/conversations/${encodeURIComponent(conversationId)}/attempts/${encodeURIComponent(attemptId)}/presentations`,
        { ...options, method: 'GET', query: { limit, after } }),
    skillSource: (options: Options = {}) => transport.request<AutomationManagementSkillSource>(
      `${base}/skill-source`, { ...options, method: 'GET' }),
    updateSkillSource: (body: AutomationManagementSkillSourceUpdateRequest, options: Options = {}) =>
      transport.request<AutomationManagementSkillSource>(`${base}/skill-source`,
        { ...options, method: 'PUT', json: body }),
    actions: (options: Options = {}) => transport.request<AutomationManagementActionPage>(
      `${base}/actions`, { ...options, method: 'GET' }),
    definitions: ({ limit, after, ...options }: PageOptions & { after?: string } = {}) =>
      transport.request<AutomationManagementDefinitionPage>(`${base}/definitions`,
        { ...options, method: 'GET', query: { limit, after } }),
    definition: (id: string, options: Options = {}) => transport.request<AutomationManagementDefinition>(
      definitionPath(id), { ...options, method: 'GET' }),
    publish: (id: string, body: AutomationManagementPublishRequest, options: Options = {}) =>
      transport.request<AutomationManagementDefinition>(definitionPath(id), { ...options, method: 'PUT', json: body }),
    pause: (id: string, body: AutomationManagementRevisionRequest, options: Options = {}) =>
      transport.request<AutomationManagementDefinition>(`${definitionPath(id)}/pause`,
        { ...options, method: 'POST', json: body }),
    retire: (id: string, body: AutomationManagementRevisionRequest, options: Options = {}) =>
      transport.request<AutomationManagementDefinition>(definitionPath(id), { ...options, method: 'DELETE', json: body }),
    start: (id: string, body: AutomationManagementStartRequest, options: Options = {}) =>
      transport.request<AutomationManagementRun>(`${definitionPath(id)}/runs`,
        { ...options, method: 'POST', json: body }),
    history: (id: string, { limit, before, ...options }: PageOptions & { before?: string } = {}) =>
      transport.request<AutomationManagementRunPage>(`${definitionPath(id)}/runs`,
        { ...options, method: 'GET', query: { limit, before } }),
    run: (id: string, options: Options = {}) => transport.request<AutomationManagementRun>(
      runPath(id), { ...options, method: 'GET' }),
    cancel: (id: string, body: AutomationManagementCancelRequest, options: Options = {}) =>
      transport.request<AutomationManagementRun>(`${runPath(id)}/cancel`, { ...options, method: 'POST', json: body }),
    approve: (id: string, body: AutomationManagementApproveRequest, options: Options = {}) =>
      transport.request<AutomationManagementRun>(`${runPath(id)}/approve`, { ...options, method: 'POST', json: body }),
  };
}
