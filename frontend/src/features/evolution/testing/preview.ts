import '../../../styles/application/00-design-tokens.css';
import '../ui-components';
/** Vite-only design entry. Synthetic data, no production API imports or writes. */
import '../../../styles/application/01-shell-navigation.css';
import '../../../styles/application/10-light-foundation.css';
import '../../../styles/application/11-tofu-theme-foundation.css';
import '../../../styles/application/26-evolution.css';

import '../../../styles/application/30-ui-kit.css';
import { ready } from '../../../i18n';
import { mountEvolutionWorkspace as mountEvolution } from '../workspace';
import { mountAgentPod } from '../agent-pod';
import type { EvolutionApi } from '../controller';

import type { EvolutionRun } from '../../../api/evolution-runs';
// Fixture selection is a test URL parameter, never product chrome.
const scenario = new URLSearchParams(location.search).get('scenario') || 'populated';
const goal = {goal_id:'preview-goal',statement:'修复论文引用检查',domain:'code',status:'admitted',exploratory:false,
  criteria:[{criterion_id:'c1',kind:'test_command',params:{command:'pytest -q'}}],budget_envelope:{max_tokens:200000,max_wall_clock_seconds:7200,max_tool_calls:200}};
const plan = {plan_id:'preview-plan',goal_id:goal.goal_id,status:'proposed',selected_path_id:'measured',candidates:[{path_id:'measured',title:'使用现有测试定位并修复',cost_estimate:{expected_tokens:24000,priors_source:'measured_history'}}]};
const mutation={mutation_id:'preview-mutation',kind:'skill_candidate',status:'proposed',version:1,parent_version:0,payload:{name:'提升文献解析能力'},evidence_episode_ids:['preview-episode']};
const previewRuns:EvolutionRun[]=[
 {run_id:'memory-review',objective:'梳理 Agent 长期记忆方案，形成可复现的研究报告',status:'executing',task_id:'demo-execution',attempt:1,created_at:3,updated_at:3,actuals:{tokens:0,tool_calls:0,wall_clock_seconds:0},receipts:[],result:'',review:null,error:''},
 {run_id:'csv-export',objective:'修复 CSV 导出的中文编码问题',status:'completed',task_id:'demo-review',attempt:1,created_at:2,updated_at:2,actuals:{tokens:0,tool_calls:0,wall_clock_seconds:0},receipts:[],result:'设计预览中的交付示例，不代表真实执行结果。',review:{outcome:'success',criterion_results:[]},error:''},
];
const observedPreview=previewRuns[0]!;
observedPreview.actuals={tokens:18420,tool_calls:12,wall_clock_seconds:84};
observedPreview.budget={max_tokens:0,max_tool_calls:0,max_wall_clock_seconds:0};
if(scenario==='legacy-failed'){
  Object.assign(observedPreview,{status:'failed',error:'budget_exceeded: max_tokens',receipts:[{task_id:observedPreview.task_id,stage:'executing',status:'failed'}],budget:{max_tokens:10000,max_tool_calls:30,max_wall_clock_seconds:600}});
}
if(scenario==='terminal'){
  Object.assign(observedPreview,{status:'completed',result:'设计预览：已整理研究报告与可复现脚本。\n验收证据：引用核验测试 12 项通过。',receipts:[{task_id:observedPreview.task_id,stage:'executing',status:'completed'}],review:{outcome:'success',criterion_results:[{criterion_id:'delivery',outcome:'pass',evidence_ref:'设计预览：12 tests passed；参考来源已记录。'}]}});
}
if(scenario==='terminal') observedPreview.result += '\n\n```tofu-ui\n'+JSON.stringify({version:1,title:'设计预览 · 实验对比',blocks:[{type:'data_chart',id:'demo-latency',title:'设计预览：相同输入下的耗时对比',kind:'bar',yLabel:'耗时',yUnit:'ms',xLabel:'方案',series:[{id:'preview',label:'合成演示数据',points:[{label:'基线',value:240},{label:'方案 A',value:165},{label:'方案 B',value:128}]}]}]})+'\n```';
if(scenario==='needs-attention')Object.assign(observedPreview,{status:'needs_attention',error:'stage_handoff_interrupted'});
if(scenario==='missing-limits')delete observedPreview.budget;
const previewMaterials=[
 {material_id:'paper',kind:'paper',title:'Reflexion: language agents with verbal reinforcement learning',url:'https://arxiv.org/abs/2303.11366',note:'示例：比较基于反馈的记忆与改进方式。'},
 {material_id:'repo',kind:'repository',title:'openai / openai-agents-python',url:'https://github.com/openai/openai-agents-python',note:'示例：记录 Agent 工程实现的参考入口。'},
 {material_id:'web',kind:'web',title:'Agent memory — evaluation notes',url:'https://docs.python.org/3/library/sqlite3.html',note:'示例：为本地记忆存储方案保留技术资料。'},
].map(m=>({...m,run_id:'memory-review',origin:'agent',task_id:'demo-execution',created_at:1,updated_at:1}));
const records: Record<string,unknown> = {
  getCoordination:{updates:[{seq:1,event_id:'demo',kind:'reply',origin:'agent',target:'coordinator',task_id:'',created_at:1,references:[],content:'设计预览：正在比较记忆方案的评测方法。这里会解释选择依据，也会记录你的补充和执行方的确认。'}],revision:1,through_seq:1,has_more:false,has_earlier:false,before_seq:1,delivery:{},coordinator:{status:'idle',enabled:true,task_id:'',last_response:'',error:''}},
  listRuns:{runs:previewRuns,floor:{items:[
    {kind:'evolution',run_id:previewRuns[0]?.run_id||'demo',title:previewRuns[0]?.objective||'',status:'executing',updated_at:3,conversation_id:''},
    {kind:'flow',run_id:'demo-flow',title:'周报汇总流程',status:'running',updated_at:2,conversation_id:''},
    {kind:'goal',run_id:'demo-goal',title:'Goal Mode',status:'running',updated_at:1,conversation_id:'demo-conv'},
  ],degraded:false},proposals:{items:[
    {mutation_id:'demo-mutation',kind:'skill_candidate',status:'proposed',version:1,title:'引用格式校验技能',evidence_count:2,updated_at:2},
    {mutation_id:'demo-budget',kind:'budget_policy',status:'confirmed',version:2,title:'评审阶段预算收紧 20%',evidence_count:1,updated_at:1},
  ],degraded:false}},listRunModels:{models:[]},
  listGoals:{goals:[goal]},listPlans:{plans:[plan]},listEpisodes:{episodes:[{episode_id:'preview-episode',goal_id:goal.goal_id,task_id:'preview-task',status:'success',actuals:{tokens:22000,wall_clock_seconds:90,tool_calls:12},verdict:{outcome:'success',assessed_by:'checker',criterion_results:[{criterion_id:'c1',outcome:'pass',evidence_ref:'12 tests passed'}]}}]},
  listMutations:{mutations:[mutation]},listCapabilityGaps:{capability_gaps:[{gap_id:'preview-gap',summary:'引用格式校验缺少统一工具',samples:3,status:'proposed'}]},listSkills:{skills:[]},
  snapshot:{snapshot:{strategies:1,anti_patterns:0}},
};
// Program notebook uses the same read surface, with unmistakably synthetic evidence.
const programId='preview-program';
const notebookStep={kind:'experiment',title:'比较持久化与恢复策略',instruction:'设计预览：实际执行会采用已有任务引擎。',checks:['重启后保留原始目标、实验记录和失败证据。'],
 hypothesis:{problem:'设计预览：调查被中断后，团队重新整理证据的成本可能过高。',audience:'设计预览：进行长周期调查的团队；尚无需求访谈。',baseline:'设计预览：人工从日志还原执行状态。',scenario:'未来场景假设：持续数周、可交接的无人值守调查。',mechanism:'机制假设 A：保留同一个待执行任务与原始上下文。',expected_value:'预期减少人工对账时间；尚未测量净收益。',assumptions:['真实采用意愿未知。','运行和交接成本仍待比较。'],falsifier:'若人工对账成本没有下降，或重复执行增加，则否定。',evidence_refs:['设计预览中的合成事件，不能验证需求。']}};
const notebookAlternative={...notebookStep,title:'验证外部执行证据是否足够',hypothesis:{...notebookStep.hypothesis,mechanism:'机制假设 B：由服务方提供可核验的发送与完成证明。',assumptions:['服务方可能尚未提供这种证明。','即使技术可行，也不代表存在付费需求。'],falsifier:'若证明无法区分未发送与已完成，就不能据此恢复。'}};
const notebookFinal={...notebookStep,kind:'final',title:'逐项审查完整目标'};
const notebookAssessment={hypothesis_sha256:'12101ad41e73332258c2796141a5e99dc168130289404ebdb30fe2b073e31246',problem:{outcome:'unknown',evidence_ref:'设计预览：尚无需求访谈或观察记录。'},feasibility:{outcome:'pass',evidence_ref:'设计预览：受控原型可运行；不能据此证明实际采用。'},novelty:{outcome:'unknown',evidence_ref:'设计预览：尚未检查替代方案。'},net_value:{outcome:'unknown',evidence_ref:'设计预览：采用成本和净收益均未测量。'}};
const notebook={schema_version:1,program_id:programId,objective:'实现可重启、可验证的长期自主探索工作台',
 acceptance:['实验过程可暂停、恢复和重试。','历史记录可追溯到实际执行与逐项验收证据。','桌面和移动端都能完成基本操作。'],
 status:'active',queue:[notebookStep,notebookAlternative,notebookFinal],current:{sequence:8,step:notebookStep,run_id:'memory-review'},
 settled_through:7,actuals:{tokens:62410,tool_calls:48,wall_clock_seconds:540},error:'',created_at:1,updated_at:3};
Object.assign(records,{listPrograms:{programs:[{...notebook,objective:notebook.objective}]},getProgram:{program:notebook},
 getProgramMemories:{memory:{entries:[{key:'preview-ranking',query:'设计预览：过去是否有并发恢复失败的反例？',created_at:1,model:'synthetic-preview',
 actuals:{tokens:1200,tool_calls:0,wall_clock_seconds:2},actuals_complete:false,source_chars:1200,scanned_sources:1,
 source_chunks:[{candidate_id:'0',sequence:3,offset:0,end:1200,source_digest:'0'.repeat(64)}],
 matches:[{candidate_id:'0',relevance:.9,quote:'设计预览：发现并发写入缺少冲突恢复。',reason:'设计预览中的相关性判断，不代表真实模型结果。'}],
 task_id:'preview-ranking',inspector_task_id:'preview-ranking'}],retained_limit:32,evidence_status:'Synthetic design fixture.'}},
 getProgramExperiments:{experiments:Array.from({length:7},(_,i)=>({schema_version:1,program_id:programId,sequence:i+1,
 step:{...notebookStep,title:['检查真实问题和依赖','建立可复现基线','探索顺序存储方案','故障注入与并发比较','验证恢复边界','保留失败与修复证据','检查前端操作链路'][i]},
 run_id:`preview-history-${i+1}`,outcome:i===2?'failed':'completed',result:'设计预览中的证据示例，不代表真实执行结果。\n实际记录会保存执行结果与评审依据。',
 review:{hypothesis_assessment:{...notebookAssessment,feasibility:i===2?{outcome:'fail',evidence_ref:'设计预览：并发原型失败。'}:notebookAssessment.feasibility},outcome:i===2?'failure':'success',requirement_results:[{requirement_id:'requirement-1',outcome:i===2?'fail':'pass',evidence_ref:'设计预览：重启故障测试结果和复现命令。'}]},
 actuals:{tokens:[5400,7800,14200,10600,8400,6200,5100][i]!,tool_calls:[4,7,12,9,7,5,4][i]!,wall_clock_seconds:[45,66,120,92,78,54,48][i]!},actuals_complete:i!==2,error:i===2?'设计预览：发现并发写入缺少冲突恢复。':'',created_at:i+1})),settled_through:7,next_cursor:null}});
let previewRoster=scenario==='swarm'?[{id:'demo-scout',role:'researcher',status:'running',objective:'比较长期记忆方案的评测方法'},{id:'demo-coder',role:'coder',status:'running',objective:'实现可复现原型'}]:[];
let previewRosterKnown=scenario==='swarm';
const api = new Proxy({} as EvolutionApi,{get:(_target,key)=>async(...args:unknown[])=>{
  if(scenario==='failure')throw new Error('预览：模拟连接中断');
  if(key==='getRun'){
    const run=previewRuns.find(r=>r.run_id===args[0])||previewRuns[0];
    return {run:structuredClone(run),pending_questions:[],workspace:{materials:run.run_id==='memory-review'?previewMaterials:[],stages:[{kind:'界定问题',detail:'明确长期记忆的评测边界与验收要求。'},{kind:'比较与实现',detail:'整理参考资料，实现可复现原型并运行测试。'},{kind:'验证交付',detail:'核验引用和结论，记录测试证据与适用边界。'}],criteria:[{criterion_id:'delivery',requirement:'结论附可追溯来源，并说明适用边界。'}],...(scenario==='missing-limits'?{}:{budget:run.budget||null})}};
  }
  const result=records[String(key)];
  if(key==='getSwarmStatus'){
    // Only the swarm scenario fills the office door's visitor roster; other
    // scenarios keep an empty floor so movement tests stay deterministic.
    return {active:previewRoster.length>0,known:previewRosterKnown,agents:structuredClone(previewRoster)};
  }
  if(result)return scenario==='empty'?Object.fromEntries(Object.entries(result).map(([name,value])=>[name,Array.isArray(value)?[]:value])):structuredClone(result);
  if(key==='goalReport')return {report:'# 验证报告\n\n12 tests passed\n\n预览数据，不代表真实执行。'};
  throw new Error('设计预览不执行命令。请在真实工作区测试提交操作。');
}});
await ready();
// Browser-only fixture seam for status-driven pod choreography checks.
Object.assign(window,{__mountEvolutionPreviewPod:mountAgentPod,
  __setEvolutionPreviewRoster:(agents:typeof previewRoster,known=true)=>{
    previewRoster=agents;previewRosterKnown=known;
    document.querySelector<HTMLButtonElement>('[data-studio-action=refresh]')?.click();
  },
});
let page: ReturnType<typeof mountEvolution>;
function mount(){page?.dispose();page=mountEvolution(document.getElementById('preview-root')!,{api,path:location.hash.slice(2)||(scenario==='program'?'evolution/programs/'+programId:'evolution/runs'),navigate:path=>{location.hash=`/${path}`;},openTask:()=>{}});}
mount();
window.addEventListener('hashchange',()=>page.updateRoute(location.hash.slice(2)||(scenario==='program'?'evolution/programs/'+programId:'evolution/runs')));
window.addEventListener('pagehide',()=>page.dispose(),{once:true});
