"""Frozen diagnostics whose returned data governs navigation or semantic joins.

Entry: adaptive_workloads(). Uses the shared read-only world and exact oracle;
no tool/code strategy is prescribed. Dependencies: baseline workload contract.
"""
from .workloads import Workload, canonical


def adaptive_workloads() -> tuple[Workload, ...]:
    records = [{'recordKey':f'k{i:03}', 'phase':'eligible' if i % 3 else 'held',
                'credits':(i * 37 + 11) % 997,
                'detail':'Supporting record detail. ' * 8} for i in range(96)]
    selected = [row for row in records if row['phase'] == 'eligible']
    navigation = Workload('observed-navigation', 'observed_dependency',
        'Read entry and follow its descriptor to the selected dataset. Use the '
        'descriptor\'s field mapping and selection rule. Return only '
        '{"count":number,"total":number}: count matching records and sum their '
        'mapped amount field. Dataset names and field mappings are supplied by '
        'the returned data, not by this request.',
        canonical({'entry':{'descriptorDataset':'route-k7','revision':41},
                   'route-k7':{'datasetName':'batch-m4','statusField':'phase',
                               'requiredStatus':'eligible','amountField':'credits'},
                   'batch-m4':records}),
        canonical({'count':len(selected),'total':sum(row['credits'] for row in selected)}))

    agreements = [
        {'id':'alpha','fee':1200,'clause':
         'Renewal requires written acceptance. Silence is not acceptance.'},
        {'id':'beta','fee':900,'clause':
         'Renews automatically unless notice is delivered thirty days before '
         'the anniversary. The anniversary is in ten days.'},
        {'id':'gamma','fee':3000,'clause':
         'Already cancelled in writing; service ends on the anniversary.'}]
    ledger = [{'id':f'p{i:03}','agreementId':agreements[i % 3]['id'],
               'status':'posted' if i % 4 else 'pending',
               'amount':(i * 73 + 19) % 2003,
               'note':'Historical supporting detail. ' * 8} for i in range(180)]
    chosen = [row for row in ledger if row['agreementId']=='beta' and row['status']=='posted']
    judgment = Workload('judgment-ledger-join', 'semantic_computation',
        'Read agreements and payments. Which agreement poses the greatest risk '
        'of an unwanted automatic renewal? Interpret its clause rather than '
        'ranking fees. For that agreement, count posted payments and sum their '
        'amounts; pending payments do not count. Return only '
        '{"id":"...","count":number,"total":number}.',
        canonical({'agreements':agreements,'payments':ledger}),
        canonical({'id':'beta','count':len(chosen),'total':sum(row['amount'] for row in chosen)}))
    return navigation, judgment
