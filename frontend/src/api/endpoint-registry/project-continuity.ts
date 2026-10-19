/** Coordination continuity facade; authentication and errors belong to transport.
 * Wire fields derive from sibling schemas. No construction network or retries.
 */
import type { EndpointPorts } from './contracts';
import type { ProjectScopeChangeRequest, ProjectScopeChangeRetry, ProjectScopeChangeView,
  ProjectResponsibilityCreate, ProjectResponsibilityCommand, ProjectResponsibilityView,
  ProjectSharedFilesPage, ProjectSharedFilesRead, ProjectSharedFilesContent, ProjectSharedFilesRegister, ProjectSharedFilesRefresh, ProjectSharedFilesEntry } from '../project-continuity.generated';
const base = '/api/v4/project/coordination';
export function buildProjectContinuityEndpoints(ports: EndpointPorts) {
  const req = <T>(path: string) => ports.get<T>(path, { onError: 'throw' });
  const post = <T>(path: string, body: object, key: string) => ports.post<T>(base + path, body,
    { headers: { 'Idempotency-Key': key }, onError: 'throw' });
  return {
    coordinationScopeChanges: (path: string) => req<{ scopeChanges: ProjectScopeChangeView[] }>(`${base}/scope-changes?path=${encodeURIComponent(path)}`),
    applyScopeChange: (body: ProjectScopeChangeRequest, key: string) => post<ProjectScopeChangeView>('/scope-changes', body, key),
    retryScopeChange: (body: ProjectScopeChangeRetry, key: string) => post<ProjectScopeChangeView>('/scope-changes/retry', body, key),
    coordinationResponsibilities: (path: string) => req<{ responsibilities: ProjectResponsibilityView[] }>(`${base}/responsibilities?path=${encodeURIComponent(path)}`),
    createResponsibility: (body: ProjectResponsibilityCreate, key: string) => post<ProjectResponsibilityView>('/responsibilities', body, key),
    pauseResponsibility: (body: ProjectResponsibilityCommand, key: string) => post<ProjectResponsibilityView>('/responsibilities/pause', body, key),
    cancelResponsibility: (body: ProjectResponsibilityCommand, key: string) => post<ProjectResponsibilityView>('/responsibilities/cancel', body, key),
    reconcileResponsibility: (body: ProjectResponsibilityCommand, key: string) => post<ProjectResponsibilityView>('/responsibilities/reconcile', body, key),
    coordinationSharedFiles: (path: string, cursor?: string) => req<ProjectSharedFilesPage>(`${base}/shared-files?path=${encodeURIComponent(path)}${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`),
    readSharedFile: (body: ProjectSharedFilesRead, key: string) => post<ProjectSharedFilesContent>('/shared-files/read', body, key),

    registerSharedFile: (body: ProjectSharedFilesRegister, key: string) => post<ProjectSharedFilesEntry>('/shared-files/register', body, key),
    refreshSharedFile: (body: ProjectSharedFilesRefresh, key: string) => post<ProjectSharedFilesEntry>('/shared-files/refresh', body, key),
  };
}
