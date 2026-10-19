/** Data-only tool projection. Remote UI resource URIs never grant execution. */
import { validateRichResponse } from './validation';
import type { RichResponse } from './contract.generated';

export function projectRichToolResult(values: readonly unknown[], toolName = ''): RichResponse | null {
  const queue = values.slice();
  const visited = new WeakSet<object>();
  for (let cursor = 0; cursor < queue.length; cursor += 1) {
    let value = queue[cursor];
    if (typeof value === 'string') {
      try { value = JSON.parse(value); } catch { continue; }
    }
    if (!value || typeof value !== 'object' || Array.isArray(value)) continue;
    if (visited.has(value)) continue;
    visited.add(value);
    const record = value as Record<string, unknown>;
    const queueConnection=/^mcp__(.+)__get_queue_resource$/.exec(toolName)?.[1];
    if(queueConnection&&typeof record.queue==='string'&&record.summary&&typeof record.summary==='object'){
      const response=validateRichResponse({version:1,title:record.queue,blocks:[{type:'resource_panel',id:'queue-resources',
        title:record.queue,provider:'queue',connection:queueConnection,queue:record.queue}]});if(response)return response;
    }
    const connection = /^mcp__(.+)__compute_catalog$/.exec(toolName)?.[1];
    if ((toolName === 'compute_catalog' || connection) && record.version === 1
      && record.availability_is_admission_guarantee === false && Array.isArray(record.queues)
      && typeof record.provider === 'string' && record.ok !== false) {
      // The original tool result remains the historical evidence. This live view
      // performs its own owned read, so old output never receives today's timestamp.
      const response=validateRichResponse({version:1,title:record.provider,blocks:[{
        type:'resource_panel',id:'compute-resources',title:record.provider,provider:record.provider,
        ...(connection?{connection}:{}),
      }]});
      if(response)return response;
    }
    if (Object.hasOwn(record, 'richResponse')) {
      const response = validateRichResponse(record.richResponse);
      if (response) return response;
    }
    for (const key of ['meta', 'content', 'structuredContent', 'structured_content', 'result']) {
      if (Object.hasOwn(record, key)) {
        const nested=record[key];
        if(Array.isArray(nested))nested.forEach(item=>{if(item?.type==='text'&&typeof item.text==='string')queue.push(item.text);});
        else queue.push(nested);
      }
    }
  }
  return null;
}
