"""Owner/program/control-bound immutable failed-candidate membership index.

Programs prepare nodes only from confirmed numerical failures after independent
replan review; the program CAS publishes the successor root. Membership grants
no scientific verdict or effect replay. The shared content-addressed trie owns
node schema, bounded lookup and capacity-probed cache sizing.
"""
from copy import deepcopy

from lib.research.source_index import ReceiptSourceIndex, digest, normalize_reference
from lib.storage.errors import StorageError

NAMESPACE = 'evolution.validation_candidate_nodes.v1'
FAILED = digest({'confirmed_numerical_failure': 1})
COMPUTED_FIELDS = ('scientific_protocol', 'scientific_required_outcome',
                   'validation_parent_sequence', 'failed_candidate_index')


def template(step):
    return {key: value for key, value in step.items() if key not in COMPUTED_FIELDS}


def _numeric_identity(value):
    # JSON numbers with equal values remain equal controls (100 and 100.0).
    if type(value) in (int, float):
        numerator, denominator = (value, 1) if type(value) is int else value.as_integer_ratio()
        return {'number_ratio': [numerator, denominator]}
    if isinstance(value, dict):
        return {key: _numeric_identity(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_numeric_identity(item) for item in value]
    return value


class FailedCandidates(ReceiptSourceIndex):
    namespace = NAMESPACE

    def __init__(self, client, *, user_id, program_id, step, **kwargs):
        from .tool_evidence import normalize_scientific_protocol
        protocol = deepcopy(normalize_scientific_protocol(step['scientific_protocol']))
        protocol['measurement_scope'].pop('candidate_digest')
        binding = digest(_numeric_identity({'program_id': program_id, 'step': template(step),
            'protocol': protocol, 'required_outcome': step['scientific_required_outcome']}))
        super().__init__(client, user_id=user_id, paper_hash=binding[:32], lang='en', **kwargs)
        # Full control hash; the inherited short research binding is not authority.
        self.prefix = self.prefix.split(':', 1)[0] + ':' + binding + ':'

    def _write(self, node):
        sha = super()._write(node)
        self.cache.pop(sha, None)
        if self._read(sha) != node:
            raise StorageError('database_integrity', 'Failed candidate index was not exactly acknowledged')
        return sha

    def contains(self, reference, candidate):
        try:
            reference = normalize_reference(reference)
        except ValueError as exc:
            raise StorageError('database_integrity', 'Invalid failed candidate index reference') from exc
        if reference['version'] != 1 or not reference['count']:
            raise StorageError('database_integrity', 'Published failed candidate index is empty')
        fact = self.lookup(reference, candidate)
        if fact is not None and fact != FAILED:
            raise StorageError('database_integrity', 'Invalid failed candidate index fact')
        return fact is not None
