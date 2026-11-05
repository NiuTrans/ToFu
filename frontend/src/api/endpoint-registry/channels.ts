/** channels endpoint facade (docs/CHANNELS.md). Wire DTOs come from api/channels.generated.ts. */
import type { EndpointPorts } from './contracts';
import type {
  ChannelAccountUpdate, ChannelAccountView, ChannelThreadPage, ChannelThreadUpdate,
  ChannelThreadView, GitHubAccountUpdate, GitHubAccountView, OutboxItemView, OutboxPage,
} from '../channels.generated';

export function createChannelsEndpoints(ports: EndpointPorts) {
  const { get, put, post } = ports;
  return {
    dxAccount: (signal?: AbortSignal) => get<ChannelAccountView>('/api/v4/channels/dx', { signal }),
    updateDxAccount: (update: ChannelAccountUpdate, signal?: AbortSignal) =>
      put<ChannelAccountView>('/api/v4/channels/dx', update, { signal }),
    createDxCommandChat: (signal?: AbortSignal) =>
      post<ChannelAccountView>('/api/v4/channels/dx/command-chat', {}, { signal }),
    dxThreads: (cursor = '', signal?: AbortSignal) => get<ChannelThreadPage>('/api/v4/channels/dx/threads',
      { query: cursor ? { cursor } : {}, signal }),
    updateDxThread: (key: string, update: ChannelThreadUpdate, signal?: AbortSignal) =>
      put<ChannelThreadView>(`/api/v4/channels/dx/threads/${encodeURIComponent(key)}`, update, { signal }),
    githubAccount: (signal?: AbortSignal) => get<GitHubAccountView>('/api/v4/channels/github', { signal }),
    updateGitHubAccount: (update: GitHubAccountUpdate, signal?: AbortSignal) =>
      put<GitHubAccountView>('/api/v4/channels/github', update, { signal }),
    githubThreads: (cursor = '', signal?: AbortSignal) => get<ChannelThreadPage>('/api/v4/channels/github/threads',
      { query: cursor ? { cursor } : {}, signal }),
    updateGitHubThread: (key: string, update: ChannelThreadUpdate, signal?: AbortSignal) =>
      put<ChannelThreadView>(`/api/v4/channels/github/threads/${encodeURIComponent(key)}`, update, { signal }),
    outbox: (signal?: AbortSignal) => get<OutboxPage>('/api/v4/channels/outbox', { signal }),
    approveOutbox: (id: string, signal?: AbortSignal) =>
      post<OutboxItemView>(`/api/v4/channels/outbox/${encodeURIComponent(id)}/approve`, {}, { signal }),
    discardOutbox: (id: string, signal?: AbortSignal) =>
      post<OutboxItemView>(`/api/v4/channels/outbox/${encodeURIComponent(id)}/discard`, {}, { signal }),
  };
}

export type ChannelsEndpoints = ReturnType<typeof createChannelsEndpoints>;
