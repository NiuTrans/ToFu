/** motion endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createMotionEndpoints(ports: EndpointPorts) {
  const { request, post, _resolve } = ports;
  const motion = {
    status:    ()                     =>
      request('/api/v4/motion/status', { method: 'GET', onError: 'null' }),
    shotRecipes: ()                   =>
      request('/api/v4/motion/shot-recipes', { method: 'GET', onError: 'null' }),
    audioContract: ()                 =>
      request('/api/v4/motion/audio-contract', { method: 'GET', onError: 'null' }),
    start:     (body: JsonObject)                 => post('/api/v4/motion/videos', body, { timeout: 0 }),
    poll:      (taskId: string, cursor: number)       =>
      request<Response | null>(`/api/v4/motion/videos/poll/${encodeURIComponent(taskId)}`,
              { method: 'GET', query: { cursor }, parse: 'response', onError: 'null' }),
    abort:     (taskId: string)               =>
      post<null>(`/api/v4/motion/videos/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null', parse: 'none' }),
    scenes:    (taskId: string)               =>
      request(`/api/v4/motion/videos/${encodeURIComponent(taskId)}/scenes`,
              { method: 'GET', onError: 'null' }),
    regenScene: (taskId: string, sceneId: string)     =>
      post(`/api/v4/motion/videos/${encodeURIComponent(taskId)}/scenes/${encodeURIComponent(sceneId)}/regen`,
           {}, { onError: 'null' }),
    // URL builders for <video> src / download anchors (no fetch involved).
    fileUrl:   (taskId: string, part: string)         =>
      _resolve(`/api/v4/motion/videos/${encodeURIComponent(taskId)}/file` +
               (part ? '?part=' + encodeURIComponent(part) : '')),
    sceneFileUrl: (taskId: string, sceneId: string)   =>
      _resolve(`/api/v4/motion/videos/${encodeURIComponent(taskId)}/scenes/${encodeURIComponent(sceneId)}/file`),
  };
  return motion;
}
