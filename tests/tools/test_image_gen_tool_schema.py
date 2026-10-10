"""Image-generation model schema — editing fidelity, routing, and cost."""

from __future__ import annotations

import pytest

from lib.tools.gateway import tool_schema_tokens
from lib.tools.image_gen import GENERATE_IMAGE_TOOL


pytestmark = pytest.mark.unit


def test_generate_image_schema_keeps_generation_edit_and_reuse_contracts():
    schema = GENERATE_IMAGE_TOOL["function"]
    desc = schema["description"].lower()
    props = schema["parameters"]["properties"]
    prompt = props["prompt"]["description"].lower()
    source = props["source_image"]["description"].lower()
    output = props["output_path"]["description"].lower()
    svg = props["svg"]["description"].lower()

    assert "create/draw/design" in desc and "edit" in desc
    assert "supplied by the user" in desc and "produced earlier" in desc
    assert "exact source_image reference" in desc
    assert "only the desired changes" in desc and "preserves" in desc
    assert "unmentioned content" in desc and "capable edit model" in desc
    assert "project path" in desc and "/api/images/" in desc
    assert "reuse it" in desc and "instead of regenerating" in desc
    assert "detailed english prompt" in desc
    assert "aspect_ratio" in desc and "resolution" in desc

    for detail in ("style", "composition", "colors", "lighting"):
        assert detail in prompt
    assert "only desired changes" in prompt
    assert "url/path" in source and "/api/images/" in source
    assert "remote url" in source and "exact result reference" in source
    assert "omit to generate a new image" in source
    assert props["aspect_ratio"]["enum"] == [
        "1:1", "16:9", "9:16", "4:3", "3:4"]
    assert "default 1:1" in props["aspect_ratio"]["description"].lower()
    assert props["resolution"]["enum"] == ["1K", "2K"]
    assert "default" in props["resolution"]["description"].lower()

    assert "project-relative" in output and "directories are created" in output
    assert "no project" in output and "server uploads" in output
    assert "rootname:subdir" in output and "bare relative uses primary" in output
    assert "routing, not a literal filename" in output
    assert "vtracer" in svg and "background removal" in svg
    assert "same-name .svg" in svg and "beside the png" in svg
    assert all(kind in svg for kind in ("logos", "icons", "illustrations"))
    assert "default false" in svg
    assert schema["parameters"]["required"] == ["prompt"]


def test_generate_image_schema_stays_within_enabled_token_budget():
    tokens = tool_schema_tokens([GENERATE_IMAGE_TOOL])
    assert tokens <= 400, (
        f'generate_image schema costs {tokens} tokens; compact repeated edit '
        'and output-routing prose without weakening reuse or fidelity')
