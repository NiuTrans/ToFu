"""Committed-upload interruption must preserve source, rows and retry identity."""
import argparse
from pathlib import Path
from benchmarks.knowledge_html.interruption import interrupt_and_recover


def main(engine):
    raw=b'<h1>Committed evidence</h1><table><tr><td>durable</td><td>retained</td></tr></table>'
    results=[interrupt_and_recover(engine,variant,raw,'interrupted.html') for variant in ('reference','candidate')]
    assert results[0]['page']==results[1]['page']
    assert results[0]['producer_pid']!=results[1]['producer_pid']
    print('PASS: two owned producers killed/joined after real commits; native cold recovery exact; one durable document preserved and digest retry reused identity')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--engine',type=Path,required=True);main(parser.parse_args().engine)
