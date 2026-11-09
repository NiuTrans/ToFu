import pytest

from tests.support.myday_records import Clock, Records


@pytest.fixture
def records():
    return Records()


@pytest.fixture
def clock():
    return Clock()
