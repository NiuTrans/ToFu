"""The Tofu adapter must project the deployment's real capabilities.

A request-scoped v2 routing envelope REPLACES the catalog's route facts, so an
incomplete projection silently removes a model capability:

* without ``vision`` the agent's ``inspect_image`` refuses with "the current
  model has no vision support" (TB2.1 chess-best-move failed on that refusal
  while the Claude Code control on the SAME relay read the board correctly);
* a hand-written deployment reasoning profile replaced the catalog's official
  GLM-5.3 profile, so ``thinking`` (and ``clear_thinking: false`` on replayed
  reasoning) vanished from the wire while the pi and Claude Code arms send
  both next to ``reasoning_effort: max`` through the same relay.

The projection is also the contract the product consumes: these tests resolve
the envelope through the real model_routing inference resolver and the real
vision predicate, not through string inspection of the JSON.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evaluations.terminal_bench import harness

pytestmark = pytest.mark.unit


def routing(model="glm-5.3-flash"):
    envelope = harness.tofu_model_routing(base_url="http://relay/v1", model=model,
                                          context_tokens=262144, output_tokens=65536)
    return envelope["model_routing"]


def test_vision_capability_reaches_the_models_offerings_and_deployments():
    doc = routing()
    assert "vision" in doc["models"][0]["capabilities"]
    assert "vision" in doc["offerings"][0]["capabilities"]


def test_projection_keeps_every_capability_the_catalog_infers_for_eval_models():
    """The declared projection must not fall behind the product catalog.

    The runner ships without the product tree (``git archive evaluations``),
    so it cannot import the catalog at run time; this test is where the
    declared list is held to the catalog's modality table for every model the
    adapter is run with.
    """
    from lib.llm_dispatch.config._pricing import MANAGED_TIER_TAGS
    from lib.llm_dispatch.discovery._capabilities import _infer_capabilities
    for model in sorted(harness.TOFU_VISION_MODELS | set(harness.TOFU_CATALOG_CREATORS)):
        projected = set(routing(model)["models"][0]["capabilities"])
        # Pricing tiers ('cheap') select auxiliary models by price; they are
        # not serving modalities and round 1 ran without them.
        assert set(_infer_capabilities(model)) - MANAGED_TIER_TAGS <= projected, model
        assert "tools" in projected
    assert "vision" in routing("glm-5.3-flash")["models"][0]["capabilities"]
    assert "vision" not in routing("some-unknown-model")["models"][0]["capabilities"]


def test_runner_builds_the_tofu_spec_from_the_evaluations_tree_alone(tmp_path):
    """The deployed runner is ``git archive <sha> evaluations``: no ``lib`` on its path.

    An adapter import of the product tree turned every trial of the
    2026-11-06 smoke into ``runner_error: No module named 'lib'``.
    """
    repo = Path(harness.__file__).resolve().parents[2]
    shutil.copytree(repo / "evaluations", tmp_path / "evaluations",
                    ignore=shutil.ignore_patterns("__pycache__"))
    program = (
        "from evaluations.terminal_bench import harness as h; "
        "a = h.tofu_agent(artifact=h.Artifact(name='tofu', filename='t.tar.gz', sha256='a' * 64, version='1'), "
        "files_url='http://r/files', model_base_url='http://r/v1', model='glm-5.3-flash', timeout_sec=60, "
        "instruction='x', context_tokens=262144, output_tokens=65536, source_guard=True); "
        "import json, sys; routing = json.loads(a['env']['TOFU_AGENT_MODEL_ROUTING']); "
        "assert 'lib' not in sys.modules; print(sorted(routing['model_routing']['models'][0]['capabilities']))")
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    probe = subprocess.run([sys.executable, "-c", program], cwd=tmp_path, env=environment,
                           capture_output=True, text=True, check=False)
    assert probe.returncode == 0, probe.stderr[-2000:]
    assert "vision" in probe.stdout


def _compiled_wire(model="glm-5.3-flash", *, replayed_reasoning=False):
    """Request parameters the product compiles for ``--thinking max`` on this route.

    Admission (normalize_document) fills the catalog's official profile, the
    inference owner resolves model/offering/connection/deployment layers, and
    the reasoning compiler writes the wire: the same path a headless run takes.
    """
    from lib.llm.reasoning import capture_intent, compile_reasoning
    from lib.model_routing.domain import normalize_document
    from lib.model_routing.inference import resolve_inference_profile
    doc = normalize_document(routing(model))
    profile = resolve_inference_profile(model=doc["models"][0], offering=doc["offerings"][0],
                                        connection=doc["connections"][0], deployment=doc["deployments"][0])
    messages = [{"role": "user", "content": "task"}]
    if replayed_reasoning:
        messages += [{"role": "assistant", "content": "", "reasoning_content": "plan",
                      "tool_calls": [{"id": "c1", "type": "function",
                                      "function": {"name": "run_command", "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    body = {"model": model, "messages": messages, "temperature": 1.0,
            "_reasoning_intent": capture_intent(thinking_depth=harness.TOFU_THINKING_LEVEL)}
    compile_reasoning(body, model, inference_profile=profile)
    return {key: value for key, value in body.items() if key != "messages" and not key.startswith("_")}


def test_official_protocol_effort_and_thinking_reach_the_wire():
    """--thinking max compiles to the catalog's GLM-5.3 wire: thinking + effort max."""
    first = _compiled_wire()
    assert first["reasoning_effort"] == "max"
    assert first["thinking"] == {"type": "enabled"}
    assert first["temperature"] == 1.0
    # Replayed reasoning is kept server-side (GLM Preserved Thinking), as the
    # other arms request on every call.
    replay = _compiled_wire(replayed_reasoning=True)
    assert replay["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert replay["reasoning_effort"] == "max"


def test_reasoning_profile_is_the_catalogs_not_a_second_adapter_copy():
    from lib.model_routing.domain import normalize_document
    from lib.model_routing.model_defaults import current_model_defaults
    from lib.model_routing.domain import ModelRef
    doc = routing()
    assert "inference_profile" not in doc["deployments"][0]
    official, _previous = current_model_defaults().resolve(ModelRef("zhipu", "glm-5.3-flash"))
    admitted = normalize_document(doc)["models"][0]["inference_profile"]
    assert admitted["reasoning"] == official["reasoning"]


def test_an_unknown_model_does_not_gain_a_fabricated_vision_claim():
    doc = routing("some-other-model")
    assert "vision" not in doc["models"][0]["capabilities"]
    assert "inference_profile" not in doc["deployments"][0]


def test_tofu_guard_config_install_and_program(tmp_path):
    agent = harness.tofu_agent(
        artifact=harness.Artifact(name="tofu", filename="tofu.tar.gz", sha256="a" * 64, version="1"),
        files_url="http://r/files", model_base_url="http://r/v1", model="glm-5.3-flash",
        timeout_sec=60, instruction="do the thing", context_tokens=262144, output_tokens=65536,
        source_guard=True)
    config = json.loads(agent["log_files"]["tofu-server-config.json"])
    from lib.search.url_blocklist import url_is_blocked
    assert url_is_blocked("https://github.com/harbor-framework/x", config["search"]["skip_domains"])
    assert "guard_check" in agent
    assert "server_config.json" in agent["program"]
