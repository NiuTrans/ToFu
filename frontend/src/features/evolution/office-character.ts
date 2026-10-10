/** Resolve live roles to one of eight authored character silhouettes.
 * The main office stations keep stable identities across task snapshots.
 */
export type CharacterModel = 'executor'|'reviewer'|'coordinator'|'researcher'|'engineer'|'writer'|'analyst'|'general';

export function modelForRole(role:string,station?:number):CharacterModel {
  if(station===0)return 'executor';
  if(station===1)return 'reviewer';
  if(station===2)return 'coordinator';
  const value=role.toLowerCase();
  if(/research|browse|search|资料|研究|检索/.test(value))return 'researcher';
  if(/cod|develop|engineer|program|开发|编程|代码|工程/.test(value))return 'engineer';
  if(/review|test|verif|audit|qa|审查|测试|验证|质检/.test(value))return 'reviewer';
  if(/writ|edit|document|文案|写作|编辑|文档/.test(value))return 'writer';
  if(/analy|data|数据|分析/.test(value))return 'analyst';
  return 'general';
}
