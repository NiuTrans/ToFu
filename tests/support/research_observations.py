"""Synthetic paired measurements for deterministic research consistency tests."""
from copy import deepcopy
import math
from lib.research.observation_audit import partition_digest

def observation_payload(protocol, reference, outcome='supported'):
    partitions = {'dataset_size':1000,'train':[[0,600]],'validation':[[600,800]],'test':[[800,1000]]}
    context = {'system':'candidate','dataset':protocol['dataset'],'metric':protocol['primary_metric'],
        'dataset_digest':'d'*64,'split_digest':partition_digest(partitions),
        'evaluation_partition':'test','budget_unit':'training_steps','budget':100}
    seeds = list(protocol['random_seeds'])
    baseline = [0.5 for _ in seeds]
    method = [(0.3 if outcome=='refuted' else 0.7) + index*0.01 for index in range(len(seeds))]
    differences = [left-right for left,right in zip(method,baseline)]
    mean = math.fsum(differences)/len(differences)
    se = math.sqrt(math.fsum((value-mean)**2 for value in differences)/(len(differences)-1)/len(differences))
    baseline_context=deepcopy(context);baseline_context['system']=protocol['baseline']
    return {'scientific_outcome':outcome, 'scientific_audit':{
        key:{'status':'verified','evidence_refs':[reference]}
        for key in ('baseline_fairness','uncertainty','seeds','validation_split')},
        'scientific_observations':{'format':protocol.get('observation_format', 'tofu.scientific-observations/v1'),
        'comparison':{'method':deepcopy(context),'baseline':baseline_context},
        'partitions':partitions,'samples':{'seeds':seeds,
        **({'method_seeds':list(seeds),'baseline_seeds':list(seeds)} if protocol.get('observation_format') == 'tofu.scientific-observations/v2' else {}),
        'method_values':method,
        'baseline_values':baseline,'mean_difference':mean,'standard_error':se}}}


def synthetic_frozen_scope(protocol):
    """Synthetic identity fixture for scope admission; no benchmark provenance claim."""
    context = observation_payload(protocol, 'metrics')['scientific_observations']['comparison']['method']
    return {**{key: context[key] for key in ('dataset_digest', 'split_digest', 'budget_unit', 'budget')},
            'candidate_digest': 'c'*64, 'baseline_digest': 'b'*64}
