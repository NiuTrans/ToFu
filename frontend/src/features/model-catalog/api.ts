/** Typed transport for Model-owned Artificial Analysis enrichment. */

import { request } from '../../api/transport';
import type { AaBlock } from './types';

const AA_PATH = '/api/v1/model-intelligence/aa';

export function fetchAaScores(): Promise<{ ok: boolean; aa?: AaBlock }> {
  return request<{ ok: boolean; aa?: AaBlock }>(AA_PATH);
}

export function refreshAaScores(): Promise<{ ok: boolean; aa?: AaBlock }> {
  return request<{ ok: boolean; aa?: AaBlock }>(`${AA_PATH}/refresh`, {
    method: 'POST',
  });
}

export function saveAaKey(apiKey: string): Promise<{ ok: boolean; aa?: AaBlock }> {
  return request<{ ok: boolean; aa?: AaBlock }>(`${AA_PATH}/key`, {
    method: 'PUT',
    json: { api_key: apiKey },
  });
}
