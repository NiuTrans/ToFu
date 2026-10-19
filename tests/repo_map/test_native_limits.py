"""First-use limit construction preserves frozen dataclass and admission APIs."""
import copy
from dataclasses import FrozenInstanceError, asdict, fields, replace
import pickle
import sys

import pytest
from lib.repo_map._native_limits import StreamLimits
from lib.repo_map import outline

pytestmark = pytest.mark.unit


def valid():
    return StreamLimits(8 << 20, 8 << 20, 32 << 20, 16 << 20)


def test_limits_keep_frozen_value_copy_and_dataclass_contracts():
    limits = valid()
    names = ('path_bytes', 'request_bytes', 'file_frame_bytes', 'source_read_bytes')
    assert tuple(field.name for field in fields(limits)) == limits.__match_args__ == names
    assert asdict(limits) == dict(zip(names, limits.arguments()))
    assert repr(limits) == ('StreamLimits(path_bytes=8388608, request_bytes=8388608, '
                            'file_frame_bytes=33554432, source_read_bytes=16777216)')
    for rebuilt in (valid(), copy.copy(limits), copy.deepcopy(limits), pickle.loads(pickle.dumps(limits))):
        assert rebuilt is not limits and rebuilt == limits and hash(rebuilt) == hash(limits)
    assert limits != limits.arguments()
    with pytest.raises(FrozenInstanceError):
        limits.path_bytes = 1
    with pytest.raises(FrozenInstanceError):
        del limits.request_bytes
    changed = replace(limits, path_bytes=1)
    assert changed.path_bytes == 1 and changed != limits and limits.path_bytes == 8 << 20
    with pytest.raises(ValueError, match='source budget'):
        replace(limits, source_read_bytes=1)


@pytest.mark.parametrize('field', ['path_bytes', 'request_bytes', 'file_frame_bytes', 'source_read_bytes'])
@pytest.mark.parametrize('value', [False, 0, -1, 1.0, sys.maxsize])
def test_limits_refuse_non_integer_or_out_of_range_replacements(field, value):
    with pytest.raises(ValueError, match='positive bounded integers'):
        replace(valid(), **{field: value})


def test_source_budget_admits_exact_canonical_boundary():
    minimum = max(outline._MAX_SOURCE_BYTES, outline._MAX_REF_BYTES * 4 + 4)
    assert replace(valid(), source_read_bytes=minimum).source_read_bytes == minimum
    with pytest.raises(ValueError, match='source budget'):
        replace(valid(), source_read_bytes=minimum-1)
