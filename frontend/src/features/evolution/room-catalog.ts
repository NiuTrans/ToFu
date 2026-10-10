/** Always-visitable studio rooms. This catalog owns role-to-room routing and
 * scenery URLs; occupancy is supplied by the live roster, never manufactured. */
import office from './assets/office-clear.webp';
import research from './assets/pod-research-clear.webp';
import engineering from './assets/pod-engineering-clear.webp';
import review from './assets/pod-review-clear.webp';
import writing from './assets/pod-writing-clear.webp';
import general from './assets/pod-office-clear.webp';
import officeThumb from './assets/room-thumb-main.webp';
import researchThumb from './assets/room-thumb-research.webp';
import engineeringThumb from './assets/room-thumb-engineering.webp';
import reviewThumb from './assets/room-thumb-review.webp';
import writingThumb from './assets/room-thumb-writing.webp';
import generalThumb from './assets/room-thumb-general.webp';
export type PodTheme = 'research'|'engineering'|'review'|'writing'|'analysis'|'general';
export const ROOM_ART:Record<PodTheme,string>={research,engineering,review,writing,analysis:general,general};
export const STUDIO_ROOMS = [
  {id:'',label:'mainRoom',image:office,thumbnail:officeThumb},
  {id:'research',label:'roomResearch',image:research,thumbnail:researchThumb},
  {id:'engineering',label:'roomEngineering',image:engineering,thumbnail:engineeringThumb},
  {id:'review',label:'roomReview',image:review,thumbnail:reviewThumb},
  {id:'writing',label:'roomWriting',image:writing,thumbnail:writingThumb},
  {id:'general',label:'roomGeneral',image:general,thumbnail:generalThumb},
] as const;
export function themeForRole(role:string):PodTheme {
  const value=role.toLowerCase();
  if(/research|browse|search|资料|研究|检索/.test(value))return 'research';
  if(/cod|develop|engineer|program|开发|编程|代码|工程/.test(value))return 'engineering';
  if(/review|test|verif|audit|qa|审查|测试|验证|质检/.test(value))return 'review';
  if(/writ|edit|document|文案|写作|编辑|文档/.test(value))return 'writing';
  if(/analy|data|数据|分析/.test(value))return 'analysis';
  return 'general';
}
export function roomForRole(role:string):string {
  const theme=themeForRole(role);return theme==='analysis'?'general':theme;
}
