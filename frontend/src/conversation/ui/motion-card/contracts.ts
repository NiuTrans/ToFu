/** Presentation inputs, not a second handler schema. Scalar values remain unknown until displayed.
 * Transport/authorization and deterministic patch admission remain in backend tool owners.
 */
import type { I18nKey } from '../../../i18n';
export type MotionText = (key: I18nKey, fallback: string) => string;
export interface MotionCardPorts { text: MotionText; scanWorkUnits?(): void }
export interface MotionRound {
  toolName?: string; query?: string; roundNum?: string | number; toolContent?: unknown;
  results?: { snippet?: unknown; workUnitRef?: { taskId?: unknown; kind?: unknown } }[];
}
export interface MotionPayload {
  alignment?: unknown;
  available_themes?: unknown;
  category?: unknown;
  chrome?: unknown;
  deduped?: unknown;
  degraded?: unknown;
  depth?: unknown;
  detail?: unknown;
  direction?: unknown;
  duration?: unknown;
  edit_of?: unknown;
  elapsed?: unknown;
  errors?: unknown;
  ffmpeg?: unknown;
  ffprobe?: unknown;
  fix_hints?: unknown;
  gates?: unknown;
  hyperframes?: unknown;
  issues?: unknown;
  node?: unknown;
  note?: unknown;
  ok?: unknown;
  output?: unknown;
  overflow_total?: unknown;
  quality_hint?: unknown;
  render_time_s?: unknown;
  scene_id?: unknown;
  span_s?: unknown;
  task_id?: unknown;
  topic?: unknown;
  version?: unknown;
  visual_quality?: unknown;
  warnings?: unknown;
  probe?: { width?: unknown; height?: unknown; codec?: unknown; fps?: unknown; duration?: unknown; has_audio?: unknown };
  scenes?: { scene_id?: unknown; audio_duration?: unknown; target_duration?: unknown; srt_duration?: unknown; overflow?: unknown }[];
  changes?: { op?: unknown; target?: unknown; page_target?: unknown; before?: unknown; after?: unknown }[];
}
