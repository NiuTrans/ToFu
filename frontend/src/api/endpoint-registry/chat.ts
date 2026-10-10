/** chat endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createChatEndpoints(ports: EndpointPorts) {
  const { request, get, post, del, taskStartAffinityOptions, _bindTaskAffinity } = ports;
  const chat = {
    // Abort + queue management — fire-and-forget, swallow errors.
    abortTask:    (taskId: string)      => post<null>(`/api/v4/chat/abort/${encodeURIComponent(taskId)}`, undefined, { onError: 'null', parse: 'none', taskId }),
    abortConv:    (convId: string)      => post<null>(`/api/v4/chat/abort-conv/${encodeURIComponent(convId)}`, undefined, { onError: 'null', parse: 'none', convId, timeout: 15000 }),
    // Per-command interrupt (): kill ONLY the task's running
    // run_command subprocess — the turn CONTINUES with the partial output fed
    // back to the model. NOT fire-and-forget: the caller needs the verdict
    // ({interrupted:true} or {interrupted:false, reason}) to paint the button.
    interruptCommand: (taskId: string)  => post(`/api/v4/chat/interrupt-command/${encodeURIComponent(taskId)}`, undefined, { onError: 'null', taskId }),
  
    // The receipt only acknowledges the request; the authoritative tool round
    // later contains the background handle or the foreground result.
    backgroundCommand: (taskId: string, roundNum: number, toolCallId: string) => post(
      `/api/v4/chat/background-command/${encodeURIComponent(taskId)}`,
      { roundNum, toolCallId }, { onError: 'null', taskId }),
    // Queue callers consume items and retain the existing empty fallback.
    queueGet:     async (convId: string) => {
      const d = await get(`/api/v4/chat/queue/${encodeURIComponent(convId)}`, { convId });
      if (d && Array.isArray(d.items)) return d.items;
      return [];
    },
    queueRemove:  (convId: string, qId: string) => del<Response | null>(`/api/v4/chat/queue/${encodeURIComponent(convId)}/${encodeURIComponent(qId)}`,
                                       { parse: 'response', onError: 'null', convId }),
    queueClear:   (convId: string)      => del<Response | null>(`/api/v4/chat/queue/${encodeURIComponent(convId)}`,
                                       { parse: 'response', onError: 'null', convId }),
    // Arm Goal Mode mid-stream. The backend queues one explicit, durable
    // continuation behind the accepted turn; it never changes that turn's
    // already-selected interpreter.
    armAutopilot: (convId: string)      => post('/api/v4/chat/autopilot/arm', { convId },
                                        { onError: 'null', convId }),
    // Disarm Goal Mode — cancels live GoalRuns and any queued Goal
    // continuation, plus legacy compatibility state.
    disarmAutopilot: (convId: string)   => post('/api/v4/chat/autopilot/disarm', { convId },
                                        { onError: 'null', convId }),
    // Continue Goal Mode on a FINISHED conversation. Creates a turn-native
    // command and launches the same GoalRun + Flow path as an ordinary send.
    // Returns {taskId} or throws ApiError (409 when continuation is unavailable).
    kickAutopilot: (convId: string, config: JsonObject) => request(
      '/api/v4/chat/autopilot/kick', Object.assign(
        { method: 'POST', json: { convId, config }, parse: 'json', timeout: 0 },
        taskStartAffinityOptions({ convId, config }, null))),
    // Full terminal stop report for one concluded GoalRun. The fold record
    // only carries the bounded excerpt; this fetches the complete text
    // persisted with the durable GoalRun transition. {runId, report}.
    autopilotReport: (convId: string, runId: string) =>
      get(`/api/v4/chat/autopilot/report?convId=${encodeURIComponent(convId)}&runId=${encodeURIComponent(runId)}`,
          { onError: 'null', convId }),
    // Per-node orchestration-flow run trace (resolved brief + bounded I/O per
    // node) for the Studio canvas/inspector overlay. {ok, taskId, flowLabel, trace}.
    flowTrace: (taskId: string) =>
      get(`/api/v4/chat/flow-trace/${encodeURIComponent(taskId)}`, { onError: 'null', taskId }),
    // A server-created child task that has no direct start response inherits
    // its conversation's ingress affinity key.
    bindTaskAffinity: (taskId: string, convId: string, key: string) =>
      _bindTaskAffinity(taskId, convId, key),
    // Interactive responses for stdin / human-guidance prompts opened by
    // tasks. Both routes return {ok, error?} JSON.
    stdinResponse:  (stdinId: string, input: string, eof: boolean, convId: string) =>
      post('/api/v4/chat/stdin-response',
           { stdinId, input: input || '', ...(eof ? { eof: true } : {}) },
           { convId }),
    humanResponse:  (guidanceId: string, response: string, convId: string) =>
      post('/api/v4/chat/human-response', { guidanceId, response }, { convId }),
  };
  return chat;
}
