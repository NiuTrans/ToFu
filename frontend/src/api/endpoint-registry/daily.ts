/** daily endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createDailyEndpoints(ports: EndpointPorts) {
  const { request, get } = ports;
  const goal = (goalId: string) => `/api/v4/myday/goals/${encodeURIComponent(goalId)}`;
  const item = (goalId: string, itemId: string) => `${goal(goalId)}/items/${encodeURIComponent(itemId)}`;
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
    // Archived generated report of one past day (read-only); null when none exists.
    report:     (dateStr: string) =>
      get(`/api/v4/daily-report/${encodeURIComponent(dateStr)}`, { onError: 'null' }),
    // My Day goals (contracts/myday_goals_v1.schema.json). Each write answers with the goal's fresh view.
    goals:          () => get('/api/v4/myday/goals'),
    goalCreate:     (text: string) =>
      request<JsonObject>('/api/v4/myday/goals', { method: 'POST', json: { text } }),
    goalUpdate:     (goalId: string, body: JsonObject) =>
      request<JsonObject>(goal(goalId), { method: 'PATCH', json: body }),
    goalItemAdd:    (goalId: string, text: string) =>
      request<JsonObject>(`${goal(goalId)}/items`, { method: 'POST', json: { text } }),
    goalItemUpdate: (goalId: string, itemId: string, body: JsonObject) =>
      request<JsonObject>(item(goalId, itemId), { method: 'PATCH', json: body }),
    goalItemRemove: (goalId: string, itemId: string) =>
      request<JsonObject>(item(goalId, itemId), { method: 'DELETE' }),
    goalSuggestions: (goalId: string, decision: 'accept' | 'dismiss') =>
      request<JsonObject>(`${goal(goalId)}/suggestions`, { method: 'POST', json: { decision } }),
    goalDecompose:  (goalId: string) =>
      request<JsonObject>(`${goal(goalId)}/decompose`, { method: 'POST' }),
    goalLink:       (goalId: string, body: JsonObject) =>
      request<JsonObject>(`${goal(goalId)}/link`, { method: 'POST', json: body }),
    goalUnlink:     (goalId: string) =>
      request<JsonObject>(`${goal(goalId)}/link`, { method: 'DELETE' }),
    goalRequests:   () => get('/api/v4/myday/requests'),
  };
  return daily;
}
