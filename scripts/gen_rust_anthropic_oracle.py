#!/usr/bin/env python3
"""Capture retained Anthropic translation/replay behavior for native parity.

Development-only: fixtures derive from the canonical Python translator, not
from the native implementation. No providers, credentials or paid calls.
"""
import argparse
import json
from pathlib import Path
from lib.llm.anthropic_outbound._sse import AnthropicSSETranslator
ROOT=Path(__file__).resolve().parents[1]
TARGET=ROOT/'packages/tofu-runtime/tests/fixtures/python_anthropic_stream.json'

def start(i,block):return {'type':'content_block_start','index':i,'content_block':block}
def delta(i,kind,**values):return {'type':'content_block_delta','index':i,'delta':{'type':kind,**values}}
def stop(i):return {'type':'content_block_stop','index':i}
def finish(reason='end_turn',usage=None):return [{'type':'message_delta','delta':{'stop_reason':reason},'usage':usage},{'type':'message_stop'}]
def generate():
 text=start(0,{'type':'text','text':'你好'})
 tool=start(1,{'type':'tool_use','id':'call','name':'WriteFile','input':{}})
 cases=[
  ('text',[text,delta(0,'text_delta',text='😀'),stop(0)]+finish(),{}),
  ('signed_thinking',[start(0,{'type':'thinking','thinking':'plan','signature':'s'}),delta(0,'thinking_delta',thinking=' more'),delta(0,'signature_delta',signature='ig'),stop(0)]+finish(),{}),
  ('tool_fragments',[tool,delta(1,'input_json_delta',partial_json='{"path":'),delta(1,'input_json_delta',partial_json='"file"}'),stop(1)]+finish('tool_use'),{'WriteFile':'write_file'}),
  ('tool_initial',[start(0,{'type':'tool_use','id':'call','name':'write_file','input':{'path':'file','content':'中文'}}),stop(0)]+finish('tool_use'),{}),
  ('duplicate_start',[tool,tool,delta(1,'input_json_delta',partial_json='{}'),stop(1)]+finish('tool_use'),{}),
  ('distinct_same_id',[tool,start(2,tool['content_block']),stop(1),stop(2)]+finish('tool_use'),{}),
  ('server_replay',[start(0,{'type':'server_tool_use','id':'host','name':'search','input':{}}),delta(0,'input_json_delta',partial_json='{"q":"source"}'),stop(0),start(1,{'type':'tool_search_tool_result','tool_use_id':'host','content':[{'type':'tool_reference','tool_name':'write_file'}]}),stop(1),start(2,{'type':'text','text':'answer'})]+finish(),{}),
  ('compaction',[start(0,{'type':'compaction','content':None}),delta(0,'compaction_delta',content='summary'),stop(0),start(1,{'type':'text','text':'answer'})]+finish('pause_turn'),{}),
  ('null_compaction',[start(0,{'type':'compaction','content':None}),stop(0)]+finish(),{}),
  ('redacted',[start(0,{'type':'redacted_thinking','data':'opaque'}),stop(0),start(1,{'type':'thinking','thinking':None,'signature':None})]+finish(),{}),
  ('missing_text_start',[delta(0,'text_delta',text='text'),delta(1,'signature_delta',signature='sig')]+finish(),{}),
  ('usage_cache',[{'type':'message_start','message':{'usage':{'input_tokens':10,'output_tokens':1,'cache_read_input_tokens':100,'cache_creation_input_tokens':20}}}]+finish('max_tokens',{'output_tokens':8,'cache_read_input_tokens':0}),{}),
  ('usage_iterations',[{'type':'message_start','message':{'usage':{'input_tokens':10,'output_tokens':3,'cache_read_input_tokens':100,'thinking_tokens':2,'iterations':[{'type':'compaction','input_tokens':40,'output_tokens':4},{'type':'message','input_tokens':50,'output_tokens':6}]}}}]+finish(),{}),
  ('unknown_finish',finish('future_reason'),{}),
  ('ping',[{'type':'ping'},{'type':'future_event'}]+finish(),{}),
 ]
 bad=[('negative_index',start(-1,{'type':'text'})),('bool_index',start(True,{'type':'text'})),('large_index',start(4096,{'type':'text'})),('bad_block',start(0,{'type':'text','text':5})),('bad_type',{'type':5}),('nonobject',[]),('tool_no_start',delta(0,'input_json_delta',partial_json='{}')),('bad_stop',stop(0)),('bad_usage',{'type':'message_delta','delta':{},'usage':[]})]
 cases += [(name,[event],{}) for name,event in bad]
 cases += [('changed_start',[tool,start(1,dict(tool['content_block'],id='other'))],{}),('late_delta',[text,stop(0),delta(0,'text_delta',text='late')],{}),('wrong_delta',[text,delta(0,'thinking_delta',thinking='wrong')],{}),('incomplete_input',[tool,delta(1,'input_json_delta',partial_json='{"path":'),{'type':'message_stop'}],{}),('scalar_input',[tool,delta(1,'input_json_delta',partial_json='[]'),stop(1)],{}),('initial_then_delta',[start(0,{'type':'tool_use','id':'id','name':'name','input':{'x':1}}),delta(0,'input_json_delta',partial_json='{}')],{})]
 result=[]
 for name,events,reverse in cases:
  owner=AnthropicSSETranslator();owner.tool_name_reverse=reverse
  outputs=[];failed=False
  for event in events:
   chunks=owner.translate(json.dumps(event,ensure_ascii=False))
   if any(isinstance(row,dict) and 'error' in row for row in chunks):failed=True;break
   outputs.extend(chunks)
  result.append(dict(name=name,events=events,reverse=reverse,outputs=outputs,failed=failed,blocks=owner.anthropic_content_blocks if not failed else None,replayed=owner._replayed_tool_starts))
 return json.dumps(result,ensure_ascii=False,indent=2)+'\n'
def requests():
 from lib.llm.anthropic_outbound._to_anthropic import openai_body_to_anthropic
 cases=[
  {'model':'fixture','max_tokens':100,'stream':True,'messages':[{'role':'user','content':'hello'}]},
  {'model':'fixture','max_tokens':100,'stream':True,'messages':[{'role':'system','content':'policy'},{'role':'developer','content':'instruction'},{'role':'user','content':'hello'}]},
  {'model':'fixture','max_tokens':100,'stream':True,'messages':[{'role':'user','content':'hello'},{'role':'assistant','content':'answer','reasoning_content':'plan','thinking_signature':'sig','tool_calls':[{'id':'call','type':'function','function':{'name':'read','arguments':'{"path":"file"}'}}]},{'role':'tool','tool_call_id':'call','content':'result'},{'role':'tool','tool_call_id':'second','content':'another'}]},
  {'model':'fixture','stream':True,'messages':[{'role':'assistant','content':[{'type':'text','text':'text','cache_control':{'type':'ephemeral'}}],'tool_calls':[{'id':'call','function':{'name':'read','arguments':'{}'}}]},{'role':'tool','tool_call_id':'call','content':[{'type':'text','text':'result','cache_control':{'type':'ephemeral'}}]}]},
  {'model':'fixture','stream':True,'messages':[{'role':'user','content':''},{'role':'assistant','content':None}]},
  {'model':'fixture','stream':True,'effort':'high','reasoning_effort':'low','output_config':{'other':'kept'},'parallel_tool_calls':False,'tool_choice':'required','stop':['END'],'temperature':0.3,'top_p':0.9,'messages':[{'role':'user','content':'hello'}]},
  {'model':'fixture','stream':True,'tool_choice':{'type':'function','function':{'name':'read'}},'tools':[{'type':'function','function':{'name':'read','description':'Read file','parameters':{'type':'object','properties':{'path':{'type':'string'}}},'strict':True,'cache_control':{'type':'ephemeral'}}}],'messages':[{'role':'user','content':'hello'}]},
  {'model':'fixture','stream':True,'messages':[{'role':'assistant','content':'projection','_anthropic_content_blocks':[{'type':'thinking','thinking':'plan','signature':'opaque'},{'type':'server_tool_use','id':'host','name':'search','input':{'q':'source'}},{'type':'tool_use','id':'call','name':'read','input':{}}]},{'role':'tool','tool_call_id':'call','content':'result'}]},
 ]
 import base64
 for image in [b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff',b'GIF89a',b'RIFF0000WEBP',b'unknown']:
  url='data:image/jpeg;base64,'+base64.b64encode(image).decode()
  cases.append({'model':'fixture','stream':True,'messages':[{'role':'user','content':[{'type':'text','text':'before'},{'type':'image_url','image_url':{'url':url,'detail':'high'},'cache_control':{'type':'ephemeral'}},{'type':'text','text':'after'}]},{'role':'tool','tool_call_id':'call','content':[{'type':'image_url','image_url':{'url':'https://example.invalid/image'}},{'type':'text','text':'tool image','cache_control':{'type':'ephemeral'}}]}]})
 schema={'type':'object','properties':{'answer':{'type':'string'}},'required':['answer'],'additionalProperties':False}
 for strict in [None,True]:
  descriptor={'name':'answer','schema':schema}
  if strict is not None:descriptor['strict']=strict
  cases.append({'model':'fixture','stream':True,'response_format':{'type':'json_schema','json_schema':descriptor},'output_config':{'effort':'low'},'effort':'high','messages':[{'role':'user','content':'question'}]})
 cases.append({'model':'fixture','stream':True,'response_format':{'type':'json_schema','json_schema':{'schema':schema,'strict':True}},'output_config':{'format':{'type':'json_schema','schema':schema},'effort':'high'},'messages':[{'role':'user','content':'question'}]})
 result=[]
 for body in cases:
  output=openai_body_to_anthropic(body);output.pop('_conversion_report',None)
  result.append({'body':body,'output':output})
 return json.dumps(result,ensure_ascii=False,indent=2)+'\n'
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args();output=generate()
 if args.check:
  if TARGET.read_text()!=output:raise SystemExit('Anthropic oracle drift')
 else:TARGET.write_text(output)
 request_target=TARGET.with_name('python_anthropic_request.json');request_output=requests()
 if args.check:
  if request_target.read_text()!=request_output:raise SystemExit('Anthropic request oracle drift')
 else:request_target.write_text(request_output)
if __name__=='__main__':main()
