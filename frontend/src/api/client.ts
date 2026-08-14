import { apiFailure, type ApiFailure } from './errors';
import {
  pageRequestId,
  request,
  type RequestOptions,
} from './transport';

export {
  ApiError,
  apiTransport,
  bindTaskAffinity,
  pageRequestId,
  request,
  resolvePath,
  taskStartAffinityOptions,
} from './transport';
export type { ApiTransport, ParseMode, RequestOptions } from './transport';

export async function result<T>(path: string, options: RequestOptions = {}): Promise<
  { ok: true; value: T } | { ok: false; error: ApiFailure }
> {
  try {
    return { ok: true, value: await request<T>(path, options) };
  } catch (error) {
    return { ok: false, error: apiFailure(error) };
  }
}

export const currentPageRequestId = pageRequestId;
