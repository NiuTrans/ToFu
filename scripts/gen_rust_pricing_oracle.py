#!/usr/bin/env python3
"""Capture resolved native arithmetic against unchanged lib.cost Python bodies.

Entry main; AST extracts canonical pure normalization/split/compute functions.
Synthetic resolved prices replace only lookup/refresh ports. The declared
outputs are the JSON oracle, pricing_unicode.rs and billing_contract.rs. No provider, wallet,
application import, database, network or production configuration is accessed.
"""
import argparse
import ast
import hashlib
import json
import logging
from pathlib import Path
import random
import unicodedata
from typing import Optional
ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'packages/tofu-runtime/tests/fixtures/python_pricing.json'
UNICODE_TARGET = ROOT / 'packages/tofu-runtime/src/pricing_unicode.rs'
BILLING_TARGET = ROOT / 'packages/tofu-runtime/src/billing_contract.rs'


def canonical():
    source = ROOT / 'lib/cost.py'
    names = {'_USAGE_KEY_ALIASES', '_USAGE_NESTED_ALIASES', '_ANTHROPIC_RESIDUAL_KEYS',
             'normalize_usage', 'usage_cache_convention', '_nested_cached',
             '_anthropic_residual_input', 'split_input_tokens', '_legacy_preset_to_model',
             '_round', 'compute_cost'}
    tree = ast.parse(source.read_text())
    nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in names)
             or (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in names)]
    assert len(nodes) == len(names)
    namespace = dict(Optional=Optional, logger=logging.getLogger('pricing-oracle'), DEFAULT_USD_CNY_RATE=7.24)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


def capture_cost_cases():
    ns = canonical()
    rng = random.Random(20261010)
    usages = [{}, {'prompt_tokens': 0, 'completion_tokens': 0},
              {'prompt_tokens': 100, 'cache_read_tokens': 100},
              {'prompt_tokens': 100, 'cache_read_tokens': 101},
              {'input_tokens': 0, 'cache_read_input_tokens': 120000},
              {'cached_tokens': 39000}, {'reasoning_tokens': 50},
              {'prompt_tokens': 2**53+1, 'completion_tokens': 1},
              {'prompt_tokens': 1, 'completion_tokens': 0},
              {'prompt_tokens': 3, 'completion_tokens': 0},
              {'prompt_tokens': 1200, 'input_tokens': 100, 'cache_read_input_tokens': 1000, 'cache_creation_input_tokens': 100},
              {'prompt_tokens': 1200, 'input_tokens': 0, 'cache_read_input_tokens': 1200},
              {'prompt_tokens': ' +1_000 ', 'completion_tokens': '0', 'cache_read_tokens': '0', 'prompt_tokens_details': {'cached_tokens': ' 200 '}},
              {'prompt_tokens': 100, 'input_tokens': 42, 'cache_read_input_tokens': None, 'cache_read_tokens': 99},
              {'prompt_tokens': 100, 'input_tokens': 0, 'cache_read_input_tokens': None, 'cached_tokens': 120},
              {'prompt_tokens': 0.9, 'completion_tokens': 1.99},
              {'prompt_tokens': True, 'completion_tokens': False},
              {'prompt_tokens': '١٢٣', 'completion_tokens': '４５'},
              {'prompt_tokens': '\u00a01_٠２\u2003', 'completion_tokens': 1},
              {'prompt_tokens': 100, 'cached_tokens': '0', 'prompt_tokens_details': {'cached_tokens': 150}},
              {'prompt_tokens': 100, 'cache_read_tokens': '0', 'cached_tokens': 200, 'prompt_tokens_details': {'cached_tokens': 300}},
              {'prompt_tokens': 250, 'cached_tokens': '0', 'effectiveCachedTokens': '500', 'prompt_tokens_details': {'cached_tokens': 300}}]
    for i in range(320):
        inp, out, write, read = [rng.randrange(0, 400000) for _ in range(4)]
        if i % 3 == 0:
            usages.append(dict(input_tokens=inp, output_tokens=out,
                cache_creation_input_tokens=write, cache_read_input_tokens=read))
        elif i % 3 == 1:
            usages.append(dict(prompt_tokens=inp+write+read, completion_tokens=out,
                cache_write_tokens=write, prompt_tokens_details={'cached_tokens': read}))
        else:
            usages.append(dict(input_tokens=inp, output_tokens=out,
                input_tokens_details={'cached_tokens': read}, reasoning_tokens=write))
    rows = [dict(input=2.5, output=10., cacheWriteMul=1.25, cacheReadMul=.1, currency='USD'),
            dict(input=6., output=24., cacheWriteMul=1., cacheReadMul=0., currency='CNY'),
            dict(input=5., output=20., cacheWriteMul=2.5, cacheReadMul=.25, currency='USD'),
            dict(input=0., output=0., cacheWriteMul=0., cacheReadMul=0., currency='USD'),
            dict(input=50., output=150., cacheWriteMul=1., cacheReadMul=1., currency='USD')]
    cases = []
    for usage in usages:
        norm = ns['normalize_usage'](usage)
        uncached, total = ns['split_input_tokens'](usage)
        for row in rows:
            ns['lookup_pricing'] = lambda *_args, **_kw: row
            ns['get_pricing_data'] = lambda: {'usdToCny': 7.24}
            result = ns['compute_cost'](usage, 'synthetic', 'synthetic-provider', at=1)
            if result is not None:
                result = {k: v for k, v in result.items() if k not in {'pricingSource', 'pricingSnapshot'}}
            cases.append(dict(usage=usage, tokens=dict(norm, uncached=uncached, total_input=total),
                price=dict(row, selected_prompt_tokens=total, usd_to_cny=7.24), expected=result))
    return cases

def capture():
    cases=capture_cost_cases()
    numeric_text_cases=[]
    for code in range(0x110000):
        if unicodedata.decimal(chr(code),None)==0:
            for digit in range(10):
                text='+'+chr(code+digit)+'_'+chr(code+digit)
                numeric_text_cases.append({'text':text,'expected':int(text)})
    for code in range(0x110000):
        char=chr(code)
        if char.isspace():
            try: expected=int(char+'123'+char)
            except ValueError: continue
            numeric_text_cases.append({'text':char+'123'+char,'expected':expected})
    return {'format':'tofu.native-pricing-oracle/v1','cases':cases,'numeric_text_cases':numeric_text_cases,'tier_cases':tier_cases(),'billing_cases':billing_cases(cases)}


def tier_cases():
    ns=canonical()
    source=ROOT/'lib/pricing/_provider.py'
    tree=ast.parse(source.read_text())
    names={'normalize_pricing','_choose_context_tier'}
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    assert len(nodes)==len(names)
    ns['_legacy_context_tiers']=lambda _model: []
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source),'exec'),ns)
    base={'input':2.5,'output':10.,'cacheWriteMul':1.25,'cacheReadMul':.1,'currency':'USD'}
    cards=[dict(base),dict(base,contextTiers=[
        {'id':'large','maxPromptTokens':128000,'input':5.,'output':20.},
        {'id':'small','maxPromptTokens':32000,'input':2.5,'output':10.},
        {'id':'middle','maxPromptTokens':64000,'input':3.,'output':12.,'currency':'CNY','cacheReadMul':0.}]),
        dict(base,contextTiers=[
            {'id':'first','maxPromptTokens':100,'input':1.,'output':2.},
            {'id':'second','maxPromptTokens':100,'input':3.,'output':4.}])]
    cases=[]
    for raw in cards:
        card=ns['normalize_pricing']('synthetic',raw)
        for prompt in [0,1,99,100,101,31999,32000,32001,63999,64000,64001,127999,128000,128001]:
            for residual in [False,True]:
                usage=({'input_tokens':prompt//10,'cache_read_input_tokens':prompt-prompt//10,'output_tokens':9} if residual else {'prompt_tokens':prompt,'cached_tokens':prompt//2,'completion_tokens':9})
                total=ns['split_input_tokens'](usage)[1]
                selected=ns['_choose_context_tier'](card.get('contextTiers'),total)
                for peak in [1.,2.]:
                    resolved=dict(card)
                    if selected:resolved.update(selected)
                    resolved['input']*=peak;resolved['output']*=peak
                    ns['lookup_pricing']=lambda *_args,**_kw: resolved
                    ns['get_pricing_data']=lambda: {'usdToCny':7.24}
                    result=ns['compute_cost'](usage,'synthetic','synthetic-provider',at=1)
                    numeric={k:v for k,v in result.items() if k not in {'pricingSource','pricingSnapshot'}}
                    cases.append(dict(card=card,usage=usage,peak=peak,expected=numeric,
                        tier_id=selected['id'] if selected else None,max_prompt_tokens=selected['maxPromptTokens'] if selected else None,
                        resolved_input=resolved['input'],resolved_output=resolved['output']))
    return cases


def billing_unit():
    tree=ast.parse((ROOT/'lib/billing/cost.py').read_text())
    node=next(n for n in tree.body if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='MICRO_PER_USD')
    amount=ast.literal_eval(node.value)
    assert type(amount) is int and 0<amount<2**64
    return amount

def billing_contract():
    return '// Generated by scripts/gen_rust_pricing_oracle.py; do not edit.\n// Authority: lib/billing/cost.py MICRO_PER_USD.\npub const MICRO_PER_USD: u64 = '+str(billing_unit())+';\n'


def billing_cases(source_cases):
    tree=ast.parse((ROOT/'lib/billing/cost.py').read_text())
    names={'_usd_to_micro','_apply_margin'}
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    assert len(nodes)==len(names)
    ns={'MICRO_PER_USD':billing_unit()}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'lib/billing/cost.py','exec'),ns)
    numeric=[case['expected'] for case in source_cases]
    numeric+=[None,{'inputCostUsd':0.0000005,'outputCostUsd':0.0000015,'cacheReadCostUsd':0.0000025,'cacheWriteCostUsd':0.0000035},
              {'inputCostUsd':0.00045,'outputCostUsd':0.,'cacheReadCostUsd':0.,'cacheWriteCostUsd':0.}]
    cases=[]
    for source_index,cost in enumerate(numeric):
        components={key:ns['_usd_to_micro']((cost or {}).get(field)) for key,field in [('input','inputCostUsd'),('output','outputCostUsd'),('cache_read','cacheReadCostUsd'),('cache_write','cacheWriteCostUsd')]}
        base=sum(components.values())
        if base>=2**64: continue
        for margin in [0.,.125,.2,1.,1.000001]:
            extra=ns['_apply_margin'](base,margin)
            if base+extra>=2**64: continue
            cases.append(dict(source_index=source_index if source_index<len(source_cases) else None,cost=None if cost is None else {key:cost[key] for key in ('inputCostUsd','outputCostUsd','cacheReadCostUsd','cacheWriteCostUsd')},margin=margin,expected=dict(components,base=base,margin=extra,total=base+extra)))
    return cases


def unicode_contract():
    zeros = [code for code in range(0x110000) if unicodedata.decimal(chr(code), None) == 0]
    spaces = []
    for code in range(0x110000):
        char = chr(code)
        if not char.isspace(): continue
        try: int(char+'1'+char)
        except ValueError: continue
        spaces.append(code)
    return ('// Generated by scripts/gen_rust_pricing_oracle.py; do not edit.\n'
            '// Authority: canonical Python int and Unicode '+unicodedata.unidata_version+'.\n'
            '#[rustfmt::skip]\npub const DECIMAL_ZEROS: &[u32] = &'+str(zeros)+';\n'
            '#[rustfmt::skip]\npub const INTEGER_WHITESPACE: &[u32] = &'+str(spaces)+';\n')


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    data = json.dumps(capture(), ensure_ascii=False, separators=(',', ':'))+'\n'
    unicode_data=unicode_contract()
    billing_data=billing_contract()
    if args.check:
        if TARGET.stat().st_size > 8 * 1024 * 1024: raise SystemExit('native pricing oracle exceeds byte bound')
        if TARGET.read_text() != data: raise SystemExit('native pricing oracle drift')
        if UNICODE_TARGET.read_text() != unicode_data: raise SystemExit('native pricing Unicode drift')
        if BILLING_TARGET.read_text() != billing_data: raise SystemExit('native billing unit drift')
    else:
        TARGET.write_text(data)
        UNICODE_TARGET.write_text(unicode_data)
        BILLING_TARGET.write_text(billing_data)
    print('native pricing oracle:', len(json.loads(data)['cases']), 'cases', hashlib.sha256(data.encode()).hexdigest())
if __name__ == '__main__': main()
