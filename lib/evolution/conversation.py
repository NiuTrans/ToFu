"""Owner-facing conversation projection over the goal journal; no second store.

Recent/earlier pages are bounded to eight updates. Delivery labels derive only
from durable role receipts and source-linked assignments, never task animation
or model prose. Acceptance is not verification of a deliverable.
"""
import json
from .coordination import page_for, snapshot, PAGE_CHARS


def delivery_for(state, event):
    result={'status':'saved','note':''}
    if event['origin']!='user':return result
    coordinator=[a for a in state['acks'] if a['role']=='coordinator' and a['through_seq']>=event['seq']]
    if coordinator:
        ack=max(coordinator,key=lambda a:a['through_seq'])
        result={'status':'considered' if ack['decision']=='accepted' else ack['decision'],'note':ack['note']}
    assignments=[e for e in state['updates'] if e.get('source_seq')==event['seq'] and e['kind']=='assignment']
    if assignments:
        latest=max(assignments,key=lambda e:e['seq'])
        result={'status':'assigned','note':'','assignment_seq':latest['seq']}
        receipts=[a for a in state['acks'] if a['role']=='executing' and a['through_seq']>=latest['seq']]
        if receipts:
            ack=max(receipts,key=lambda a:a['through_seq'])
            result.update(status=ack['decision'],note=ack['note'])
    elif event['kind']=='guidance':
        receipts=[a for a in state['acks'] if a['role']=='executing' and a['through_seq']>=event['seq']]
        if receipts:
            ack=max(receipts,key=lambda a:a['through_seq'])
            result.update(status=ack['decision'],note=ack['note'])
    return result


def conversation_page(run_id, *, user_id, after=0, recent=False, before=None):
    state=snapshot(run_id,user_id=user_id)
    if recent or before is not None:
        eligible=[e for e in state['updates'] if before is None or e['seq']<before]
        candidates=[];size=0
        for event in reversed(eligible[-8:]):
            projected=page_for({**state,'updates':[event]},'all',0)['updates'][0]
            length=len(json.dumps(projected,ensure_ascii=False))
            if candidates and size+length>PAGE_CHARS:break
            candidates.insert(0,projected);size+=length
        page={'updates':candidates,'through_seq':candidates[-1]['seq'] if candidates else 0,
              'revision':state['revision'],'has_more':False}
    else:page=page_for(state,'all',after)
    first=page['updates'][0]['seq'] if page['updates'] else 0
    page.update(has_earlier=bool(first and any(e['seq']<first for e in state['updates'])),before_seq=first,
                delivery={str(e['seq']):{**delivery_for(state,e),'note':delivery_for(state,e)['note'][:240]}
                          for e in state['updates'] if e['origin']=='user'},
                acknowledgements=state['acks'],coordinator=state['manager'],followup=state.get('followup'))
    return page
