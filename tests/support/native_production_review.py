"""Explicit fixture-owner review over the real production HTTP boundaries.

Only inspect artifacts produced by actual write_file calls. These requests model
human review and steering; they grant no review authority to the provider.
"""


class DeliveryReview:
    def __init__(self, client, conversation, turn_id):
        self.client = client
        self.conversation = conversation
        self.turn_id = turn_id
        self.path = f'/api/v4/conversations/{conversation}/turns/{turn_id}/review'
        self.facts = f'/api/v4/conversations/{conversation}/working-facts'

    def get(self, path):
        response = self.client.get(path)
        assert response.status_code == 200, response.text
        return response.json()['data']

    def post(self, path, body):
        response = self.client.post(path, json=body)
        assert response.status_code == 200, response.text
        return response.json()['data']

    def artifacts(self):
        rows = self.get(f'/api/v4/artifacts?conv={self.conversation}')['artifacts']
        return {name: next(row for row in rows if row['title'] == name + '.html')
                for name in ('ledger', 'report')}

    def requirements(self, region, artifacts):
        view = self.get(self.path)
        return self.post(self.path, {'action': 'requirements',
            'requirementsRevision': view['requirementsRevision'],
            'objective': 'Deliver the requested regional report and preserve the once-only publication ledger',
            'criteria': [{'id': 'ledger', 'kind': 'requirement', 'text': 'The ledger contains one publication'},
                {'id': 'region', 'kind': 'requirement', 'text': 'Report region: ' + region}],
            'outputs': [{'id': name, 'label': name, 'artifactId': row['id']}
                        for name, row in artifacts.items()]})

    def region_fact(self, identifier=None):
        region = next(row for row in self.get(self.path)['criteria'] if row['id'] == 'region')
        current = self.get(self.facts)
        value = self.post(self.facts, {'action': 'record', 'revision': current['revision'],
            'kind': 'observation', 'text': region['text'],
            'sources': [{'kind': 'criterion', 'targetKind': 'turn', 'id': self.turn_id,
                'criterionId': 'region', 'version': region['digest'], 'quote': region['text']}],
            **({'id': identifier} if identifier else {})})['facts'][0]
        return [{'id': value['id'], 'revision': value['revision']}]

    def artifact_review(self, artifact):
        return self.get(f"/api/v4/artifacts/{artifact['id']}/review")

    def accept_artifact(self, artifact, fact_bindings=()):
        path = f"/api/v4/artifacts/{artifact['id']}/review"
        view = self.get(path)
        if not view['criteria']:
            view = self.post(path, {'action': 'requirements',
                'requirementsRevision': view['requirementsRevision'],
                'objective': 'Match the inspected delivery requirement',
                'criteria': [{'id': 'content', 'kind': 'requirement', 'text': 'Meets the current delivery requirement'}]})
        return self.post(path, {'action': 'decision',
            'requirementsRevision': view['requirementsRevision'], 'decisionsRevision': view['decisionsRevision'],
            'artifactDigest': view['artifactDigest'], 'criterionId': 'content',
            'criterionDigest': view['criteria'][0]['digest'], 'verdict': 'pass',
            'note': 'Fixture owner inspected the produced bytes', 'factBindings': list(fact_bindings)})

    def decide(self, criterion_id, output_ids, fact_bindings=()):
        view = self.get(self.path)
        criterion = next(row for row in view['criteria'] if row['id'] == criterion_id)
        return self.post(self.path, {'action': 'decision',
            'requirementsRevision': view['requirementsRevision'], 'decisionsRevision': view['decisionsRevision'],
            'criterionId': criterion_id, 'criterionDigest': criterion['digest'], 'verdict': 'pass',
            'note': 'Fixture owner inspected the exact output versions',
            'outputBindings': [{'id': row['id'], 'version': row['version']}
                               for row in view['outputs'] if row['id'] in output_ids],
            'factBindings': list(fact_bindings)})

    def inspect(self, artifact, expected):
        response = self.client.get(f"/api/artifacts/{artifact['id']}/raw")
        assert response.status_code == 200, response.text
        assert response.text == expected

    def before_crash(self, contents):
        artifacts = self.artifacts()
        for name, artifact in artifacts.items():
            self.inspect(artifact, contents[name])
        self.requirements('west', artifacts)
        binding = self.region_fact()
        ledger = self.accept_artifact(artifacts['ledger'])
        report = self.accept_artifact(artifacts['report'], binding)
        self.decide('ledger', {'ledger'})
        view = self.decide('region', {'report'}, binding)
        assert ledger['status'] == report['status'] == 'accepted'
        assert view['status'] == 'pending', 'Running task must not imply accepted delivery'
        return {'artifacts': artifacts, 'turn': view, 'ledger': ledger, 'report': report, 'binding': binding}

    def revise(self, before):
        assert self.artifact_review(before['artifacts']['report'])['status'] == 'accepted'
        changed = self.requirements('east', before['artifacts'])
        ledger = next(row for row in changed['criteria'] if row['id'] == 'ledger')
        previous = next(row for row in before['turn']['criteria'] if row['id'] == 'ledger')
        assert ledger['state'] == 'pass' and ledger['evidence'] == previous['evidence']
        assert self.artifact_review(before['artifacts']['ledger']) == before['ledger']
        assert self.artifact_review(before['artifacts']['report'])['criteria'][0]['state'] == 'stale'
        return changed

    def finish(self, before, contents):
        artifacts = self.artifacts()
        assert artifacts['ledger']['id'] == before['artifacts']['ledger']['id']
        assert artifacts['report']['parent_id'] == before['artifacts']['report']['id']
        for name, artifact in artifacts.items():
            self.inspect(artifact, contents[name])
        self.requirements('east', artifacts)
        binding = self.region_fact(before['binding'][0]['id'])
        self.accept_artifact(artifacts['report'], binding)
        accepted = self.decide('region', {'report', 'answer'}, binding)
        assert accepted['status'] == 'accepted'
        assert all(row['covered'] for row in accepted['outputs'])
        assert self.artifact_review(artifacts['ledger']) == before['ledger']
        return {'accepted': accepted, 'artifacts': artifacts,
            'old_report': self.artifact_review(before['artifacts']['report'])}
