"""Component evidence must prove equal work and physical library sharing."""
from copy import deepcopy
import pytest
from benchmarks.native_shared_library import validate_pair

pytestmark = pytest.mark.unit


def pair():
    before = dict(arm='independent', first='token', work={'tokens': [2], 'start': 'a'},
        identity=[1, '100'], archive_sha256='a' * 64, application_version='fixture',
        source_sha256={'module': 'b' * 64}, joined_cleanup=True, system_admission=False,
        scope='combined native library cold component', wall_ns=10, cpu_ns=9, maxrss_kib=100,
        member_identities=[1, 2], handles_shared=False)
    after = dict(before, arm='shared', identity=[2, '200'], member_identities=[3, 3], handles_shared=True)
    return before, after


def test_equal_completed_work_with_independent_owners_is_comparable():
    validate_pair(*pair())


@pytest.mark.parametrize('field,value', [
    ('work', {'tokens': [1], 'start': 'a'}), ('source_sha256', {'module': 'c' * 64}),
    ('identity', [1, '100']), ('joined_cleanup', False), ('system_admission', True),
    ('member_identities', [3, 4]), ('handles_shared', False), ('cpu_ns', 0),
    ('archive_sha256', 'd' * 64), ('first', 'flow'),
])
def test_incomplete_or_unequal_evidence_cannot_supply_ratios(field, value):
    before, after = deepcopy(pair())
    after[field] = value
    with pytest.raises(ValueError):
        validate_pair(before, after)
