/** Public request/result types. Wire authority: api-v4.generated.ts; no runtime dependencies. */


export interface TofuOptions {
  baseUrl: string;
  /** Optional for a loopback-only headless server; required for remote use. */
  apiKey?: string;
  timeoutMs?: number;
  userAgent?: string;
  fetchImpl?: typeof fetch;
}

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content: string | Array<Record<string, unknown>> | null;
  name?: string;
  tool_calls?: Array<Record<string, unknown>>;
  tool_call_id?: string;
}

export type NativeModelSelection =
  | { creator_id: string; model_id: string }
  | { provider_id: string; offering_id: string };

export interface RoutingPolicy {
  preferred_provider_id?: string;
  required_context?: number;
  price_budget?: {
    max_input?: number;
    max_output?: number;
    currency?: 'USD' | 'CNY';
  };
  cache_affinity_connection_id?: string;
}

export interface ChatRequest {
  messages: ChatMessage[];
  model: NativeModelSelection;
  routing?: RoutingPolicy;
  config?: Record<string, unknown>;
  temperature?: number;
  max_tokens?: number;
  top_p?: number;
  stop?: string | string[];
  tools?: Array<Record<string, unknown>>;
  tool_choice?: string | Record<string, unknown>;
  response_format?: Record<string, unknown>;
  conversation_id?: string;
  idempotency_key?: string;
  timeout_s?: number;
}

export interface AgentRunRequest {
  messages: ChatMessage[];
  /** Optional only when a standalone runtime advertises a configured default. */
  model?: NativeModelSelection;
  routing?: RoutingPolicy;
  /** One-request standalone v2 access envelope; never an inline provider. */
  model_routing?: Record<string, unknown>;
  config?: Record<string, unknown>;
  capabilities?: Record<string, unknown>;
  /** Request-local OpenAI function schemas. */
  tools?: Array<Record<string, unknown>>;
  trajectory?: 'sharegpt' | 'openai-finetune' | 'anthropic' | 'tofu-native';
  timeout_s?: number;
  conversation_id?: string;
  id?: string;
}

export interface AgentRunResult {
  ok: boolean;
  id: string;
  object: 'agent.run';
  task_id: string;
  status: 'pending' | 'running' | 'done' | 'error' | 'aborted';
  model: string;
  provider_id?: string;
  finish_reason: string;
  content: string;
  thinking: string;
  usage: Record<string, number>;
  n_tool_rounds: number;
  error?: Record<string, unknown>;
  tool_calls?: Array<Record<string, unknown>>;
  trajectory_format?: string;
  trajectory?: unknown;
  [key: string]: unknown;
}

export interface ChatCompletion {
  ok: boolean;
  id: string;
  object: 'chat.completion';
  created: number;
  model: string;
  choices: Array<{
    index: number;
    message: ChatMessage & { reasoning_content?: string };
    finish_reason: string;
  }>;
  usage: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
  task_id: string;
}

export interface TaskState {
  id: string;
  kind: string;
  status: 'pending' | 'running' | 'done' | 'error' | 'aborted';
  created_at: number;
  finished_at: number | null;
  result?: unknown;
  meta?: Record<string, unknown>;
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  rate_limit_rpm: number;
  rate_limit_tpd: number;
  created_at: number;
  last_used_at: number | null;
  expires_at: number | null;
  disabled: boolean;
}
