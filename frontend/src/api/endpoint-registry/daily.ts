/** daily endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createDailyEndpoints(ports: EndpointPorts) {
  const { request, get } = ports;
  const daily = {
    calendar:   (year: number, month1Based: number) =>
      get(`/api/v4/daily-report/calendar/${encodeURIComponent(year)}/${encodeURIComponent(month1Based)}`,
          { onError: 'null' }),
    status:     (dateStr: string) =>
      get(`/api/v4/daily-report/status/${encodeURIComponent(dateStr)}`,
          { onError: 'null' }),
    convCount:  (dateStr: string) =>
      get(`/api/v4/daily-report/conv-count/${encodeURIComponent(dateStr)}`,
          { onError: 'null' }),
    // Triggers an async generation — body is the parsed JSON or null.
    generate:   (dateStr: string, force: boolean) =>
      request<Response | null>('/api/v4/daily-report/generate',
              { method: 'POST', json: { date: dateStr, force: !!force }, parse: 'response' }),
    // CRUD on this-day's task list. body is route-specific JSON.
    taskCreate: (body: JsonObject) =>
      request<Response | null>('/api/v4/daily-report/task',
              { method: 'POST', json: body, parse: 'response', onError: 'null' }),
    taskDelete: (body: JsonObject) =>
      request<Response | null>('/api/v4/daily-report/task',
              { method: 'DELETE', json: body, parse: 'response', onError: 'null' }),
    taskStatus: (body: JsonObject) =>
      request<Response | null>('/api/v4/daily-report/task-status',
              { method: 'PATCH', json: body, parse: 'response', onError: 'null' }),
    todoToggle: (body: JsonObject) =>
      request<Response | null>('/api/v4/daily-report/todo-toggle',
              { method: 'PATCH', json: body, parse: 'response', onError: 'null' }),
    inheritedTodoToggle: (body: JsonObject) =>
      request<Response | null>('/api/v4/daily-report/inherited-todo-toggle',
              { method: 'PATCH', json: body, parse: 'response', onError: 'null' }),
    inheritedTodoDelete: (body: JsonObject) =>
      request<Response | null>('/api/v4/daily-report/inherited-todo',
              { method: 'DELETE', json: body, parse: 'response', onError: 'null' }),
  };
  return daily;
}
