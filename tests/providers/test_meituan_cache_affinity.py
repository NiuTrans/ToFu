"""Fresh Meituan connections declare affinity on native chat as well as other faces."""
import json
from pathlib import Path

import pytest

from lib.llm.cache_affinity import cache_affinity_headers, cache_affinity_key
from lib.provider.provider_template_recipes import (
    compile_provider_template_bundle,
    normalize_provider_template,
)

pytestmark = pytest.mark.unit


def test_fresh_meituan_connections_carry_lineage_header_on_every_face():
    path = Path(__file__).resolve().parents[2] / "bundles/meituan/provider_templates/meituan.json"
    if not path.is_file():
        pytest.skip("Meituan bundle is absent in public builds")
    template = normalize_provider_template(json.loads(path.read_text()))
    bundle = compile_provider_template_bundle(
        "meituan", selected_model_ids=["glm-5.3-flash", "claude-opus-5.5", "gpt-6.1-sol"],
        _templates=[template])
    connections = bundle["connections"]
    assert any(row["base_url"] == template["base_url"] for row in connections)
    key = cache_affinity_key({"_conv_id": "new-install-conversation"})
    for connection in connections:
        assert cache_affinity_headers(
            connection.get("inference_profile"), protocol=connection["protocol"],
            oauth="", key=key,
        ) == {"Mt-Session-Id": key}, connection["base_url"]
