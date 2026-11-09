/** channels endpoint facade (docs/CHANNELS.md). Wire DTOs come from api/channels.generated.ts. */
import type { EndpointPorts } from './contracts';
import type { AuditPage } from '../channels.generated';
import type {
  BotAccountUpdate, BotAccountView, Channel, ChannelAccountUpdate, ChannelAccountView, ChannelThreadPage,
  ChannelThreadUpdate, ChannelThreadView, GitHubAccountUpdate, GitHubAccountView, OutboxItemView, OutboxPage,
} from '../channels.generated';

/** The bot platforms (docs/CHANNELS.md C12), one account each. */
export type BotChannel = BotAccountView['channel'];

export function createChannelsEndpoints(ports: EndpointPorts) {
  const { get, put, post, _resolve } = ports;
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
    botAccount: (channel: BotChannel, signal?: AbortSignal) =>
      get<BotAccountView>(`/api/v4/channels/${channel}`, { signal }),
    updateBotAccount: (channel: BotChannel, update: BotAccountUpdate, signal?: AbortSignal) =>
      put<BotAccountView>(`/api/v4/channels/${channel}`, update, { signal }),
    botThreads: (channel: BotChannel, cursor = '', signal?: AbortSignal) =>
      get<ChannelThreadPage>(`/api/v4/channels/${channel}/threads`, { query: cursor ? { cursor } : {}, signal }),
    updateBotThread: (channel: BotChannel, key: string, update: ChannelThreadUpdate, signal?: AbortSignal) =>
      put<ChannelThreadView>(`/api/v4/channels/${channel}/threads/${encodeURIComponent(key)}`, update, { signal }),
    outbox: (channel: Channel, signal?: AbortSignal) =>
      get<OutboxPage>('/api/v4/channels/outbox', { query: { channel }, signal }),
    approveOutbox: (id: string, signal?: AbortSignal) =>
      post<OutboxItemView>(`/api/v4/channels/outbox/${encodeURIComponent(id)}/approve`, {}, { signal }),
    discardOutbox: (id: string, signal?: AbortSignal) =>
      post<OutboxItemView>(`/api/v4/channels/outbox/${encodeURIComponent(id)}/discard`, {}, { signal }),
    /** What Tofu recorded for other people on the owner's behalf, newest first; `before` is a page's nextBefore. */
    audit: (channel: Channel, before = '', signal?: AbortSignal) =>
      get<AuditPage>('/api/v4/channels/audit', { query: before ? { channel, before } : { channel }, signal }),
    /** Browser URL of an original image attached to an outbox item (a draft's thumbnail). */
    outboxAttachmentUrl: (id: string, index: number) =>
      _resolve(`/api/v4/channels/outbox/${encodeURIComponent(id)}/attachments/${encodeURIComponent(index)}`),
  };
}

export type ChannelsEndpoints = ReturnType<typeof createChannelsEndpoints>;
