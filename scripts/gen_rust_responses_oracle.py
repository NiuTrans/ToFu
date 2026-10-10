#!/usr/bin/env python3
"""Capture canonical Responses translation/replay receipts for native parity.

Development-only fixtures; no provider, credential, billing or network effects.
"""
import argparse
import json
from pathlib import Path
from lib.llm.responses_outbound._sse import ResponsesSSETranslator
ROOT=Path(__file__).resolve().parents[1]
TARGET=ROOT/'packages/tofu-runtime/tests/fixtures/python_responses_stream.json'
def added(index,item):return {'type':'response.output_item.added','output_index':index,'item':item}
def call(identity='item',call_id='call',name='read',arguments=''):
 return {'type':'function_call','id':identity,'call_id':call_id,'name':name,'arguments':arguments}
def arguments(kind='delta',**values):return {'type':'response.function_call_arguments.'+kind,**values}
def message(text,**values):return {'type':'message','role':'assistant','content':[{'type':'output_text','text':text}],**values}
def terminal(items=None,**values):return {'type':'response.completed','response':{'id':'response-id','output':items or [],**values}}
def normalize(value):
 if isinstance(value,str):return value
 if 'error' in value:return value
 out={'choices':[{'delta':value['choices'][0].get('delta',{}),'finish_reason':value['choices'][0].get('finish_reason')}]}
 if 'usage' in value:out['usage']=value['usage']
 return out
def generate():
 first=call();start=added(0,first)
 cases=[
  ('text_suffix',[{'type':'response.output_text.delta','delta':'你好'},terminal([message('你好😀')])]),
  ('snapshot_only',[terminal([message('answer')])]),
  ('metadata_terminal',[{'type':'response.output_text.delta','delta':'answer'},terminal([{'type':'message','content':[]}])]),
  ('refusal',[{'type':'response.refusal.delta','delta':'refused'},terminal([{'type':'message','content':[{'type':'refusal','refusal':'refused'}]}])]),
  ('reasoning_boundaries',[{'type':'response.reasoning_summary_text.delta','delta':'one'},{'type':'response.reasoning_summary_part.added'},{'type':'response.reasoning_text.delta','delta':'two'},added(0,{'type':'reasoning','id':'reason','encrypted_content':'opaque'}),{'type':'response.output_item.done','output_index':0,'item':{'type':'reasoning','id':'reason','encrypted_content':'opaque'}},terminal([{'type':'reasoning','id':'reason','encrypted_content':'final'},message('answer')])]),
  ('function_complete',[start,arguments(item_id='item',delta='{"path":'),arguments('done',item_id='item',arguments='{"path":"file"}'),terminal([call(arguments='{"path":"file"}')])]),
  ('function_terminal_suffix',[start,arguments(item_id='item',delta='{"path":'),terminal([call(arguments='{"path":"file"}')])]),
  ('parallel',[start,added(1,call('second','call-two','write')),arguments(item_id='second',delta='{}'),arguments(item_id='item',delta='{}'),terminal([call(arguments='{}'),call('second','call-two','write','{}')])]),
  ('replayed_start',[start,start,arguments(item_id='item',delta='{}'),terminal([call(arguments='{}')])]),
  ('recycled_id',[start,added(1,call('item','call-two','read')),arguments(item_id='item',output_index=0,delta='{}'),arguments(item_id='item',output_index=1,delta='{}'),terminal([call(arguments='{}'),call('item','call-two','read','{}')])]),
  ('missing_routing_ids',[{'type':'response.output_item.added','item':call('',arguments='{}')},terminal()]),
  ('tool_reverse',[added(0,call(name='short',arguments='{}')),terminal([call(name='short',arguments='{}')])]),
  ('output_done',[start,{'type':'response.output_item.done','output_index':0,'item':call(arguments='{}')},terminal([call(arguments='{}')])]),
  ('usage',[terminal([message('answer')],usage={'input_tokens':10,'output_tokens':20,'input_tokens_details':{'cached_tokens':3,'cache_write_tokens':4},'output_tokens_details':{'reasoning_tokens':5}})]),
  ('usage_null',[terminal(usage=None)]),
  ('phase_replay',[terminal([message('comment',phase='commentary'),message('answer',phase='final_answer')])]),
  ('opaque_positions',[{'type':'response.output_item.done','output_index':0,'item':{'type':'reasoning','id':'same','encrypted_content':'old'}},terminal([{'type':'reasoning','id':'same','encrypted_content':'first'},{'type':'reasoning','id':'same','encrypted_content':'second'},message('answer')])]),
  ('root_visibility',[added(0,message('hidden',agent={'agent_name':'/child'},phase='final_answer')),{'type':'response.output_text.delta','output_index':0,'delta':'hidden'},{'type':'response.output_text.delta','agent':{'agent_name':'/root'},'delta':'root'},terminal([message('hidden',agent={'agent_name':'/child'},phase='final_answer'),message('root',agent={'agent_name':'/root'},phase='final_answer')])]),
  ('caller',[added(0,dict(first,caller={'type':'direct'})),terminal([dict(call(arguments='{}'),caller={'type':'direct'})])]),
  ('agent_caller',[added(0,dict(first,agent={'agent_name':'/root'})),terminal([call(arguments='{}')])]),
  ('program_pending',[terminal([{'type':'program_output','id':'program','output':'result'}])]),
  ('incomplete_length',[{'type':'response.incomplete','response':{'output':[message('partial')],'incomplete_details':{'reason':'max_output_tokens'}}}]),
  ('provider_error',[{'type':'response.failed','response':{'error':{'code':'rate_limit_exceeded','message':'slow down','http_status':429}}}]),
  ('progress',[{'type':'response.created'},{'type':'response.future_event'},terminal([message('answer')])]),
  ('text_conflict',[{'type':'response.output_text.delta','delta':'before'},terminal([message('changed')])]),
  ('argument_conflict',[start,arguments(item_id='item',delta='{"path":'),arguments('done',item_id='item',arguments='{}')]),
  ('unknown_identity',[start,arguments(item_id='unknown',delta='{}')]),
  ('identity_conflict',[start,added(1,call('second','other','read')),arguments(item_id='item',output_index=1,delta='{}')]),
  ('ambiguous_id',[start,added(1,call('item','other','read')),arguments(item_id='item',delta='{}')]),
  ('changed_start',[start,added(0,call(name='changed'))]),
  ('terminal_without_start',[terminal([first])]),
  ('changed_terminal_name',[start,terminal([call(name='changed',arguments='{}')])]),
  ('invalid_index',[added(True,first)]),
  ('invalid_terminal_output',[{'type':'response.completed','response':{'output':{}}}]),
  ('invalid_type',[{'type':7}]),
 ]
 result=[]
 for name,events in cases:
  owner=ResponsesSSETranslator();owner.tool_name_reverse={'short':'long_tool_name'}
  outputs=[];failed=False
  for event in events:
   chunks=owner.translate(json.dumps(event,ensure_ascii=False))
   if any(isinstance(row,dict) and 'error' in row for row in chunks) and name!='provider_error':failed=True;break
   outputs.extend(normalize(row) for row in chunks)
  result.append({'name':name,'events':events,'outputs':outputs,'failed':failed,'replay':owner.response_items if not failed else None,'response_id':owner.response_id if not failed else None})
 return json.dumps(result,ensure_ascii=False,indent=2)+'\n'
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args();expected=generate()
 if args.check:
  if TARGET.read_text()!=expected:raise SystemExit('Responses oracle drift')
 else:TARGET.write_text(expected)
if __name__=='__main__':main()
