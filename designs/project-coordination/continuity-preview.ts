/** STATIC MOCK HTTP fixture only. Playwright intercepts all /api requests. */
import { createButton } from '../../frontend/src/ui';
import { ready, setLanguage } from '../../frontend/src/i18n';
import { openProjectCoordination } from '../../frontend/src/features/project-coordination';
import { createProjectEndpoints } from '../../frontend/src/api/endpoint-registry/project';
import { createEndpointPorts } from '../../frontend/src/api/endpoint-registry/ports';
import * as transport from '../../frontend/src/api/transport';
await ready(); await setLanguage('en');
const api = createProjectEndpoints(createEndpointPorts(transport, () => 'en'));
const path = '/mock/project', conversationId = 'mock-coord';
const open = () => openProjectCoordination({ path, currentConversationId: conversationId,
  currentPath: () => path, currentConversation: () => conversationId,
  binding: api.coordination, bind: api.bindCoordinator, board: async () => ({ active: [], recentOutcomes: [] }), navigate: () => {}, openEvidence: () => {},
  dispatch: {
    recipients: () => [{ id: 'mock-worker', title: 'Worker · not a model suggestion' }],
    humanTurns: () => [{ actor: 'human', turnId: 'mock-human', projectionRevision: 7, promptPreview: 'Explicit finite authorization' }],
    commandId: api.newAssignmentCommandId, list: api.coordinationAssignments, grant: api.grantAssignment,
    dispatch: api.dispatchAssignment, revoke: api.revokeAssignment, review: api.reviewAssignment, publish: api.publishAssignment,
  },
  scopeChanges: { list: async path => (await api.coordinationScopeChanges(path))!.scopeChanges,
    apply: api.applyScopeChange, retry: api.retryScopeChange },
  responsibilities: { list: async path => (await api.coordinationResponsibilities(path))!.responsibilities,
    create: api.createResponsibility, pause: api.pauseResponsibility, cancel: api.cancelResponsibility, reconcile: api.reconcileResponsibility },
  sharedFiles: { list: api.coordinationSharedFiles,
    read: (_path, body) => api.readSharedFile(body, api.newAssignmentCommandId()), register: api.registerSharedFile, refresh: api.refreshSharedFile },
});
document.getElementById('launch')!.append(createButton({ label: 'Open coordination drawer · static mocks', variant: 'primary', wrap: true, onClick: () => { void open(); } }));
