"""The compiled TofuSQL catalog must match its canonical generated contract."""
from scripts import gen_tofusql_catalog as generator
import pytest

pytestmark = pytest.mark.unit


def test_tofusql_catalog_generated_output_is_current():
    assert generator.OUTPUT.read_text(encoding='utf-8') == generator._render(generator._load())
