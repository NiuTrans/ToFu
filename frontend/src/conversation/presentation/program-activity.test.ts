/** No source evaluation, fabricated counts, or name-only adapter hiding. */
import { describe, expect, it } from 'vitest';
import { boundedProgramDisplay, programActivityMembers, programGatewayOwner, programOwnsGateway } from './program-activity';
import { programExecutionCardHtml } from '../ui/program-execution-card';

describe('program lexical activity', () => {
  it('recognizes direct member calls across comments without claiming execution', () => {
    expect(programActivityMembers('const x=tools /* note */ . inputs({name:"mcp__llm__experiment_get"}); if(false)tools.result(x);return []'))
      .toEqual(['inputs', 'result']);
    expect(programActivityMembers('// tools.invoke()\n"tools.keep()"; `tools.observe()`; /tools.result()/; obj.tools.inputs(); tools["result"]();'))
      .toEqual([]);
    expect(programActivityMembers('tools.invoke();'.repeat(20_000))).toEqual(['invoke']);
  });
  it('shows prior-result lookup and the honest empty result at a glance', () => {
    const html = programExecutionCardHtml({programStatus:'completed',
      programCode:'return await tools.inputs({name:"mcp__llm__experiment_get"});',
      programStats:{tool_calls:0,successful_tool_calls:0},programResult:[]});
    expect(html).toContain('Script: look up prior results');
    expect(html).toContain('No source tools called again');
    expect(html).toContain('Result: []');
    expect(html).not.toContain('intent');
  });
});

describe('gateway ownership', () => {
  const gateway={toolName:'execute_program',toolCallId:'call',llmRound:15,attemptId:'a',toolArgs:{code:'return []'}};
  const parent={_programSynthetic:true,_programCallId:'call',llmRound:15,attemptId:'a',programCode:'return []'};
  it('suppresses only a uniquely owned same-occurrence adapter', () => {
    expect(programOwnsGateway(gateway,[gateway,parent])).toBe(true);

    expect(programGatewayOwner(gateway,[gateway,parent])).toBe(parent);
    expect(programGatewayOwner(gateway,[parent,{...parent}])).toBeUndefined();
    expect(programOwnsGateway({...gateway,toolCallId:'recycled'},[parent])).toBe(false);

    expect(programOwnsGateway(gateway,[{...parent,_programCallId:'call__tofu_local_2',programBackend:'local_javascript'}])).toBe(true);
    expect(programOwnsGateway(gateway,[{...parent,_programCallId:'call__tofu_local_2',programBackend:'external'}])).toBe(false);
    for (const context of [[],[parent,parent],[{...parent,llmRound:16}],[{...parent,attemptId:'b'}]])
      expect(programOwnsGateway(gateway,context)).toBe(false);
    expect(programOwnsGateway({...gateway,toolName:'read'},[parent])).toBe(false);
  });
  it('preserves an unowned admission failure', () => {
    expect(programOwnsGateway({...gateway,toolCallId:'unknown',toolArgs:{code:'broken'}},[parent])).toBe(false);
  });
});

it('bounds deep, wide, huge and circular return values explicitly', () => {
  expect(boundedProgramDisplay({value:'x'.repeat(100_000)}).length).toBeLessThan(17_000);
  expect(boundedProgramDisplay(Array.from({length:10_000},(_,i)=>i))).toContain('display truncated');
  const circular:Record<string,unknown>={}; circular.self=circular;
  expect(boundedProgramDisplay(circular)).toContain('circular');
  expect(boundedProgramDisplay({x:1})).toBe('{\n  "x": 1\n}');
});


import type { TurnRecord } from '../../api/conversation-sync.generated';
import { createTurnState } from '../domain/turn-store';
import { selectConversationViewModel } from './conversation-view-model';

it('projects one parent on reload and never hides an unowned gateway failure', () => {
  const parent={_programSynthetic:true,_programCallId:'gateway',llmRound:15,
    roundNum:2,programStatus:'completed',programCode:'return []',programResult:[]};
  const turn={turnId:'t',laneId:'main',actor:'assistant',status:'completed',projectionRevision:1,
    projection:{segments:[{type:'tool_use',id:'gateway',name:'execute_program',input:{code:'return []'},llmRound:15}],
      toolRounds:[parent]}} as unknown as TurnRecord;
  const state=createTurnState('unification-contract');
  state.turnsById.t=turn; state.laneOrder.main=['t'];
  const blocks=selectConversationViewModel(state).mainLane.turns[0].blocks;
  expect(blocks.filter(block=>block.kind==='program')).toHaveLength(1);
  expect(blocks.filter(block=>block.kind==='tool')).toHaveLength(0);
  state.turnsById.t={...turn,projectionRevision:2,projection:{...turn.projection,
    segments:[{type:'tool_use',id:'failure',name:'execute_program',input:{code:'bad'},llmRound:15}]}} as unknown as TurnRecord;
  expect(selectConversationViewModel(state).mainLane.turns[0].blocks.filter(block=>block.kind==='tool')).toHaveLength(1);
});


it('labels SDK directory counts as retained inputs and preserves raw result evidence', () => {
  const directory = { inputs: [], nextOffset: null, total: 0 };
  const html = programExecutionCardHtml({ programStatus: 'completed',
    programCode: 'return await tools.inputs({name:"mcp__llm__experiment_eval_jobs"});',
    programStats: { tool_calls: 0, successful_tool_calls: 0 }, programResult: directory });
  const header = html.slice(0, html.indexOf('</summary>'));
  expect(header).toContain('Retained inputs: 0 returned / 0 total');
  expect(header).not.toContain('experiment jobs');
  expect(html).toContain('nextOffset');
  const paginated = programExecutionCardHtml({ programStatus: 'completed', programCode: 'tools.inputs({});',
    programResult: { inputs: [{ ref: 'input_saved' }], nextOffset: 'private-cursor', total: 8 } });
  expect(paginated.slice(0, paginated.indexOf('</summary>'))).toContain('Retained inputs: 1 returned / 8 total');
  expect(paginated.slice(0, paginated.indexOf('</summary>'))).not.toContain('private-cursor');
  for (const result of [{ inputs: [], total: 0 }, { inputs: [], nextOffset: null, total: -1 },
      { inputs: [], nextOffset: null, total: NaN }, { jobs: [], total: 0 }]) {
    const unknown = programExecutionCardHtml({ programStatus: 'completed', programCode: 'tools.inputs({});', programResult: result });
    expect(unknown.slice(0, unknown.indexOf('</summary>'))).not.toContain('Retained inputs:');
  }
  const unrelated = programExecutionCardHtml({ programStatus: 'completed', programCode: 'return localValue;', programResult: directory });
  expect(unrelated.slice(0, unrelated.indexOf('</summary>'))).not.toContain('Retained inputs:');
});
