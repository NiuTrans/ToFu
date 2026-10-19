// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderChoices, type ChoiceInteraction, type ChoicePorts } from './choices';
import { createChoiceInteractionPorts } from '../conversation/application/choice-interaction';
const pending: ChoiceInteraction = { interactionId:'hg_server', question:'Choose scope', options:['Client','Server'], state:'pending' };
const flush = async () => { for(let i=0;i<8;i++) await Promise.resolve(); };
function mount(overrides: Partial<ChoicePorts> = {}) {
  const ports: ChoicePorts = {load:vi.fn(async()=>({...pending})),submit:vi.fn(async(_id,response)=>({...pending,state:'answered' as const,response})),commandId:vi.fn(()=> 'stable-command'),...overrides};
  const view=renderChoices({interactionId:pending.interactionId},ports); document.body.append(view.node); return {view,ports};
}
function select(node: HTMLElement, index=0) {const input=node.querySelectorAll<HTMLInputElement>('input')[index];input.checked=true;input.dispatchEvent(new Event('change',{bubbles:true}));}
function send(node:HTMLElement){node.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));}
afterEach(()=>document.body.replaceChildren());
describe('native durable choices lifecycle',()=>{
  it('loads trusted choices; native radio keyboard semantics and disabled state',async()=>{
    const {view,ports}=mount();expect(view.node.getAttribute('aria-busy')).toBe('true');await flush();
    expect(view.node.querySelectorAll('input[type=radio]')).toHaveLength(2);
    const button=view.node.querySelector<HTMLButtonElement>('[type=submit]')!;expect(button.disabled).toBe(true);
    select(view.node);expect(button.disabled).toBe(false);send(view.node);expect(button.disabled).toBe(true);await flush();
    expect(ports.submit).toHaveBeenCalledWith('hg_server','Client','stable-command',expect.any(AbortSignal));
    expect(view.node.textContent).toContain('Answer saved: Client');expect(view.node.querySelector('fieldset')?.disabled).toBe(true);view.dispose();
  });
  it('rejects safely, preserves selection and retries with the SAME command identity',async()=>{
    let calls=0;const {view,ports}=mount({submit:vi.fn(async(_id,response)=>{if(!calls++)throw Error('lost ACK');return {...pending,state:'answered' as const,response};})});
    await flush();select(view.node);send(view.node);await flush();expect(view.node.textContent).toContain('rejected');
    send(view.node);await flush();expect(ports.commandId).toHaveBeenCalledOnce();expect(ports.submit).toHaveBeenCalledTimes(2);view.dispose();
  });
  it('reload projects stale or previously answered state and preserves unchanged focused controls',async()=>{
    let value={...pending};const {view}=mount({load:async()=>value});await flush();
    const input=view.node.querySelector<HTMLInputElement>('input')!;input.focus();
    value={...pending,state:'stale'};view.node.querySelector<HTMLButtonElement>('[data-rich-control=choices-reload]')!.click();await flush();
    expect(document.activeElement).toBe(input);expect(view.node.querySelector('fieldset')?.disabled).toBe(true);
    value={...pending,state:'answered' as const,response:'Server'} as typeof value;
    view.node.querySelector<HTMLButtonElement>('[data-rich-control=choices-reload]')!.click();await flush();expect(view.node.textContent).toContain('Answer saved: Server');view.dispose();
  });
  it('bounds single flight and aborts disposal without a late DOM delivery',async()=>{
    let resolve!:(value:ChoiceInteraction)=>void;let signal!:AbortSignal;
    const {view}=mount({load:(_id,s)=>{signal=s;return new Promise(r=>{resolve=r;});}});
    const before=view.node.textContent;view.dispose();expect(signal.aborted).toBe(true);resolve(pending);await flush();expect(view.node.textContent).toBe(before);expect(view.node.querySelectorAll('input')).toHaveLength(0);
  });
  it('never offers unbound model interactions, scripts or arbitrary endpoints',async()=>{
    const view=renderChoices({interactionId:'https://evil.invalid/submit'},undefined);document.body.append(view.node);
    expect(view.node.querySelector('button')?.disabled).toBe(true);expect(view.node.querySelector('a')).toBeNull();view.dispose();
    const {view:bound}=mount({load:async()=>({...pending,question:'<script>bad</script>',options:['<img src=x>','safe']})});await flush();expect(bound.node.querySelector('script,img')).toBeNull();bound.dispose();
  });
});
function turn(state:'pending'|'answered'|'live'='pending'):any{return {conversationId:'owned',turnId:'turn',projectionRevision:7,projection:{toolRounds:[{toolName:'ask_human',guidanceId:'hg_server',guidanceType:'choice',guidanceQuestion:'Choose scope',guidanceOptions:[{label:'Client'},{label:'Server'}],...(state==='answered'?{toolMeta:[{guidanceId:'hg_server',userResponse:'Client'}]}:{})}]},settlement:state==='pending'?{resumeOptions:[{operation:'answer_guidance',anchor:{guidanceId:'hg_server'}}]}:null};}
describe('existing authenticated conversation command adapter',()=>{
  it('denies foreign turn/interaction and never submits to model endpoints',async()=>{
    const command=vi.fn();const ports=createChoiceInteractionPorts({conversationId:'owned',turnId:'turn',readTurn:async()=>({...turn(),conversationId:'foreign'}),createAttempt:command,commandId:()=> 'cmd'});
    await expect(ports.load('hg_server',new AbortController().signal)).rejects.toThrow('Foreign');expect(command).not.toHaveBeenCalled();
    const owner=createChoiceInteractionPorts({conversationId:'owned',turnId:'turn',readTurn:async()=>turn(),createAttempt:command,commandId:()=> 'cmd'});
    await expect(owner.submit('model-arbitrary-id','Client','cmd',new AbortController().signal)).rejects.toThrow('not owned');expect(command).not.toHaveBeenCalled();
    await expect(owner.submit('hg_server','undeclared','cmd',new AbortController().signal)).rejects.toThrow('undeclared');expect(command).not.toHaveBeenCalled();
  });
  it('retries the exact durable command after ACK loss removes the settlement',async()=>{
    let current=turn(),calls=0;
    const command=vi.fn(async(_conv,_turn,body)=>{current=turn('live');if(!calls++)throw Error('lost ACK');return {turn:current,attempt:{commandId:body.commandId,operation:'answer_guidance',turnId:'turn'}};});
    const ports=createChoiceInteractionPorts({conversationId:'owned',turnId:'turn',readTurn:async()=>current,createAttempt:command,commandId:()=> 'cmd'}),signal=new AbortController().signal;
    await expect(ports.submit('hg_server','Client','cmd',signal)).rejects.toThrow('lost ACK');
    expect((await ports.submit('hg_server','Client','cmd',signal)).response).toBe('Client');
    expect(command).toHaveBeenCalledTimes(2);expect(command.mock.calls[1][2]).toEqual(command.mock.calls[0][2]);
    await expect(ports.submit('hg_server','Server','new-command',signal)).rejects.toThrow('stale');
  });
  it('submits only settled durable answer_guidance and replays stored answer without another command',async()=>{
    let current=turn();const command=vi.fn(async(_conv,_turn,body)=>{current=turn('answered');return {turn:current,attempt:{commandId:body.commandId,operation:'answer_guidance',turnId:'turn'}};});
    const ports=createChoiceInteractionPorts({conversationId:'owned',turnId:'turn',readTurn:async()=>current,createAttempt:command,commandId:()=> 'cmd'}),signal=new AbortController().signal;
    expect((await ports.submit('hg_server','Client','cmd',signal)).response).toBe('Client');
    expect(command).toHaveBeenCalledWith('owned','turn',{commandId:'cmd',operation:'answer_guidance',expectedProjectionRevision:7,humanResponse:'Client',config:{}},signal);
    expect((await ports.submit('hg_server','Server','cmd',signal)).response).toBe('Client');expect(command).toHaveBeenCalledOnce();
    current=turn('live');expect((await ports.load('hg_server',signal)).state).toBe('stale');await expect(ports.submit('hg_server','Client','another-command',signal)).rejects.toThrow('stale');expect(command).toHaveBeenCalledOnce();
  });
});

it('preserves one-option questions and safely describes their controls', async () => {
  const { view } = mount({ load: async () => ({ ...pending, options: ['Continue'], descriptions: ['<img src=x> explanation'] }) });
  await flush();
  const input = view.node.querySelector<HTMLInputElement>('input')!;
  expect(input).not.toBeNull(); expect(input.value).toBe('Continue');
  const explanation = view.node.querySelector('.rich-choice-description')!;
  expect(input.getAttribute('aria-describedby')).toBe(explanation.id);
  expect(explanation.textContent).toContain('<img src=x>'); expect(view.node.querySelector('img')).toBeNull();
  view.dispose();
});
