"""Native HTML vocabulary/oracle remain derived from the declared Python owner."""
import json

import pytest

pytestmark = pytest.mark.unit


def test_generated_profile_entities_and_oracle_match_current_owner():
    from scripts.gen_native_html_projection import outputs
    for path, expected in outputs().items():
        assert path.read_text() == expected, str(path)
        if path.suffix == '.json':
            rows = json.loads(expected)['cases']
            assert any(row.get('error') == 'numeric_reference' for row in rows)
            assert any(row['source'] == '<a b= "unterminated>tail' and
                       row['expected'] == 'tail' for row in rows)
