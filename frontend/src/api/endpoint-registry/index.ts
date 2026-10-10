/** Compose page-scoped endpoint facades. No browser global, request or timer runs during construction. */
import type { ApiTransport } from '../transport';
import { createEndpointPorts } from './ports';
import { createFoldersEndpoints } from './folders';
import { createUsersEndpoints } from './users';
import { createPaperFoldersEndpoints } from './paper-folders';
import { createOrchestrationsEndpoints } from './orchestrations';
import { createMemoryEndpoints } from './memory';
import { createSkillsEndpoints } from './skills';
import { createToolsEndpoints } from './tools';
import { createKnowledgeEndpoints } from './knowledge';
import { createProfileEndpoints } from './profile';
import { createUserContextEndpoints } from './user-context';
import { createTimerEndpoints } from './timer';
import { createSchedulerEndpoints } from './scheduler';
import { createTasksEndpoints } from './tasks';
import { createResearchEndpoints } from './research';
import { createOptimizerEndpoints } from './optimizer';
import { createCompactionsEndpoints } from './compactions';
import { createConversationsEndpoints } from './conversations';
import { createTextEndpoints } from './text';
import { createTranslateEndpoints } from './translate';
import { createChatEndpoints } from './chat';
import { createLogsEndpoints } from './logs';
import { createUpdateEndpoints } from './update';
import { createDatabasePanelEndpoints } from './database-panel';
import { createSwarmEndpoints } from './swarm';
import { createActivityEndpoints } from './activity';
import { createDesktopEndpoints } from './desktop';
import { createHealthEndpoints } from './health';
import { createPricingEndpoints } from './pricing';
import { createClientErrorEndpoints } from './client-error';
import { createNetworkEndpoints } from './network';
import { createServerConfigEndpoints } from './server-config';
import { createCostExperimentsEndpoints } from './cost-experiments';
import { createFeaturesEndpoints } from './features';
import { createProvidersEndpoints } from './providers';
import { createLocalServeEndpoints } from './local-serve';
import { createModelRoutingEndpoints } from './model-routing';
import { createDispatchEndpoints } from './dispatch';
import { createOauthEndpoints } from './oauth';
import { createMcpEndpoints } from './mcp';
import { createBrowserEndpoints } from './browser';
import { createAuthSourcesEndpoints } from './auth-sources';
import { createPrivateHostsEndpoints } from './private-hosts';
import { createSitePacksEndpoints } from './site-packs';
import { createCredentialsEndpoints } from './credentials';
import { createTradingEndpoints } from './trading';
import { createProjectEndpoints } from './project';
import { createPaperEndpoints } from './paper';
import { createSlidesEndpoints } from './slides';
import { createMotionEndpoints } from './motion';
import { createDailyEndpoints } from './daily';
import { createImagesEndpoints } from './images';
import { createPdfEndpoints } from './pdf';
import { createDocEndpoints } from './doc';
import { createMediaEndpoints } from './media';
import { createAudioEndpoints } from './audio';
import { createVideosEndpoints } from './videos';
import { createArtifactsEndpoints } from './artifacts';
export function createEndpointRegistry(transport: ApiTransport, readLanguage: () => string) {
  const ports = createEndpointPorts(transport, readLanguage);
  return { request: ports.request, get: ports.get, post: ports.post, put: ports.put, patch: ports.patch, del: ports.del, stream: ports.stream, ApiError: transport.ApiError, _resolve: transport.resolvePath, pageRequestId: transport.pageRequestId,
    folders: createFoldersEndpoints(ports),
    users: createUsersEndpoints(ports),
    paperFolders: createPaperFoldersEndpoints(ports),
    orchestrations: createOrchestrationsEndpoints(ports),
    memory: createMemoryEndpoints(ports),
    skills: createSkillsEndpoints(ports),
    tools: createToolsEndpoints(ports),
    knowledge: createKnowledgeEndpoints(ports),
    profile: createProfileEndpoints(ports),
    userContext: createUserContextEndpoints(ports),
    timer: createTimerEndpoints(ports),
    scheduler: createSchedulerEndpoints(ports),
    tasks: createTasksEndpoints(ports),
    research: createResearchEndpoints(ports),
    optimizer: createOptimizerEndpoints(ports),
    compactions: createCompactionsEndpoints(ports),
    conversations: createConversationsEndpoints(ports),
    text: createTextEndpoints(ports),
    translate: createTranslateEndpoints(ports),
    chat: createChatEndpoints(ports),
    logs: createLogsEndpoints(ports),
    update: createUpdateEndpoints(ports),
    databasePanel: createDatabasePanelEndpoints(ports),
    swarm: createSwarmEndpoints(ports),
    activity: createActivityEndpoints(ports),
    desktop: createDesktopEndpoints(ports),
    health: createHealthEndpoints(ports),
    pricing: createPricingEndpoints(ports),
    clientError: createClientErrorEndpoints(ports),
    network: createNetworkEndpoints(ports),
    serverConfig: createServerConfigEndpoints(ports),
    costExperiments: createCostExperimentsEndpoints(ports),
    features: createFeaturesEndpoints(ports),
    providers: createProvidersEndpoints(ports),
    localServe: createLocalServeEndpoints(ports),
    modelRouting: createModelRoutingEndpoints(ports),
    dispatch: createDispatchEndpoints(ports),
    oauth: createOauthEndpoints(ports),
    mcp: createMcpEndpoints(ports),
    browser: createBrowserEndpoints(ports),
    authSources: createAuthSourcesEndpoints(ports),
    privateHosts: createPrivateHostsEndpoints(ports),
    sitePacks: createSitePacksEndpoints(ports),
    credentials: createCredentialsEndpoints(ports),
    trading: createTradingEndpoints(ports),
    project: createProjectEndpoints(ports),
    paper: createPaperEndpoints(ports),
    slides: createSlidesEndpoints(ports),
    motion: createMotionEndpoints(ports),
    daily: createDailyEndpoints(ports),
    images: createImagesEndpoints(ports),
    pdf: createPdfEndpoints(ports),
    doc: createDocEndpoints(ports),
    media: createMediaEndpoints(ports),
    audio: createAudioEndpoints(ports),
    videos: createVideosEndpoints(ports),
    artifacts: createArtifactsEndpoints(ports),
  };
}
