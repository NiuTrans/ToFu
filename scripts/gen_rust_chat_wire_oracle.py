#!/usr/bin/env python3
"""Freeze Python's shared Chat normalization stages for borrowed Rust parity.

This excludes model-specific image/reasoning transforms and orphan repair.
Value and canonical field-order bytes are both retained; no live model/store.
"""
import argparse
import copy
import json
from pathlib import Path
import random

from lib.llm_sanitize._fields import _strip_non_api_fields
from lib.llm_sanitize._messages import (
    _strip_empty_text_blocks, _drop_empty_assistant_messages,
    _merge_consecutive_same_role, _fix_empty_user_messages,
)
from lib.llm.body._canonical_wire import canonicalize_messages_inplace

TARGET = Path(__file__).resolve().parents[1] / 'packages/tofu-runtime/tests/fixtures/python_chat_wire.json'


def project(messages, preserve):
    clean = _strip_non_api_fields(messages, carry_same_role_seam_hints=True)
    for source, message in zip(messages, clean):
        if source.get('_responses_items'):
            message['_responses_items'] = copy.deepcopy(source['_responses_items'])
    _strip_empty_text_blocks(clean)
    clean = _drop_empty_assistant_messages(clean)
    clean = _merge_consecutive_same_role(clean)
    _fix_empty_user_messages(clean)
    canonicalize_messages_inplace(clean)
    for message in clean:
        message.pop('_responses_items', None)
        content = message.get('content')
        if (not preserve and isinstance(content, list) and content and all(
                isinstance(part, dict) and part.get('type') == 'text'
                and set(part) <= {'type','text'} for part in content)):
            message['content'] = ''.join(part['text'] for part in content)
    return clean


def cases(random_cases=32):
    plain = lambda text: {'type':'text','text':text}
    image = {'type':'image_url','image_url':{'url':'data:image/png;base64,cHJvYmU='}}
    sources = [[], [{'role':'user','content':'question','_isMeta':True,'carrier':'private'}],
        [{'role':'user','content':'a'},{'role':'assistant','content':''},{'role':'user','content':'b'}],
        [{'role':'user','content':''},{'role':'user','content':'tail'}],
        [{'role':'assistant','content':None,'reasoning_content':'proof'}, {'role':'assistant','content':'answer'}],
        [{'role':'system','content':[plain('instructions')]}, {'role':'user','content':'q'},
         {'role':'user','content':[plain('ambient')],'_contextComposer':True}],
        [{'role':'assistant','content':'opaque','_responses_items':[{'type':'reasoning'}]},
         {'role':'assistant','content':'answer'}],
        [{'role':'user','content':[plain(' '),plain('head'),plain('\t'),image,plain('  '),plain('tail')]}],
        [{'role':'user','content':[plain('head'),{'type':'text','text':' ','cache_control':{'type':'ephemeral'}},plain('tail')]}],
        [{'role':'assistant','content':[],'tool_calls':[{'function':{'arguments':'{}','name':'echo'},'type':'function','id':'call'}]},
         {'role':'tool','tool_call_id':'call','content':None}],
        [{'role':'assistant','reasoning_details':[{'signature':'sig','thinking':'proof','type':'thinking'}],
          'tool_calls':[{'z':7,'function':{'z':3,'arguments':'{}','name':'echo'},'type':'function','id':'call'}]}],
    ]
    sources.extend([{'role':'user','content':[plain(chr(i))]}]
                   for i in range(0x110000) if chr(i).isspace())
    rng = random.Random(674321)
    values = ['', 'question', '汉字 🦀', None, [], [plain('head'),plain(' '),plain('tail')],
              [plain('\u001c'),image,plain('tail')], [plain('cached'),{'type':'text','text':'keep','cache_control':{'type':'ephemeral'}}]]
    for _ in range(random_cases):
        messages=[]
        for _ in range(rng.randrange(1,9)):
            message={'role':rng.choice(['user','assistant','system','tool']), 'content':copy.deepcopy(rng.choice(values))}
            if message['role']=='tool': message['tool_call_id']='call'
            if rng.randrange(4)==0: message['_contextComposer']=True
            if message['role']=='assistant' and rng.randrange(5)==0: message['reasoning_content']='proof'
            messages.append(message)
        sources.append(messages)
    result=[]
    for index,messages in enumerate(sources):
        before=copy.deepcopy(messages)
        for preserve in (True,False):
            output=project(messages,preserve)
            result.append(dict(name=str(index),preserve=preserve,messages=messages,output=output,
                wire=json.dumps(output,ensure_ascii=False,separators=(',',':'))))
        assert messages==before
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--random-cases',type=int,default=32)
    parser.add_argument('--output',type=Path,default=TARGET)
    args=parser.parse_args()
    if not 0<=args.random_cases<=10000: parser.error('random cases must be 0..10000')
    output=json.dumps(cases(args.random_cases),ensure_ascii=False,indent=2)+'\n'
    if args.check:
        if not args.output.exists() or args.output.read_text()!=output: raise SystemExit('Chat normalization oracle drift')
    else: args.output.write_text(output)


if __name__=='__main__':main()
