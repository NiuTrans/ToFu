"""Seeded exact differential oracle for experimental knowledge HTML projection."""
import json
from pathlib import Path
import random
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from benchmarks.knowledge_html.reference import load_reference
_html_to_text,_reference_sha256=load_reference()
from benchmarks.knowledge_html.extractor import extract_html_projection


def check():
    cases=['plain 中文','<h1>A <i>B</i></h1><p>C</p>',
        '<table><tr><th>x</th><td>82|181</td></tr></table>',
        '<script>hidden &x;</script><svg>hidden</svg><p>shown</p>',
        'a<!-- hidden -->b','a<![CDATA[ b ]]>c','a<br>b</br>c',
        '<pre> \t\n </pre><p> \t\n </p>',
        '&amp;&unknown;&#128;&#xD800;&#0;&#x110000;',
        '<h2>outer<h3>inner</h3>end</h2>',
        '<table><tr><td>outer<table><tr><td>inner</td></tr></table>end</td></tr></table>']
    cases.extend(['<div>a'*3000+'</div>'*3000,'<div>'*3000+'text'+'</orphan>'*3000,'<pre><textarea> \n'+'<div>x'*1000+'</pre>tail'])
    randomizer=random.Random(20261007)
    tags=['p','div','h1','h3','tr','td','th','b','i','br','pre','textarea','template','svg','noscript']
    text=['alpha',' 中文 ',' \t\n ','&amp;','&notanentity;','&#128;','a|b','<&','<!--skip-->']
    for _ in range(4096):
        parts=[]
        for _ in range(randomizer.randrange(1,24)):
            choice=randomizer.randrange(4);tag=randomizer.choice(tags)
            parts.append('<'+tag+'>' if choice==0 else '</'+tag+'>' if choice==1 else randomizer.choice(text))
        cases.append(''.join(parts))
    mismatches=[]
    for index,value in enumerate(cases):
        expected=_html_to_text(value)
        actual=extract_html_projection(value)
        if expected!=actual:
            mismatches.append({'index':index,'source':value,'expected':expected,'actual':actual})
            if len(mismatches)==5:break
    print(json.dumps({'reference_source_sha256':_reference_sha256,'generated':len(cases),'checked':index+1,'mismatches':mismatches},ensure_ascii=True,indent=2))
    assert not mismatches,'candidate differs from canonical HTML extraction'

if __name__=='__main__':check()
