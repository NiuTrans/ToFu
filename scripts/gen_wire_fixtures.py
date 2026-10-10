#!/usr/bin/env python3
"""Generate the golden-wire fixture corpus from the contract authorities.

Fixtures are deterministic minimal payloads derived from the same schemas
the runtime gates enforce — never hand-written, never recorded traffic.
Byte-exact regeneration is the conformance guarantee: a schema change that
alters the wire shape changes the rendered fixture, so ``--check`` fails
until the corpus is republished in the same commit.

  contracts/fixtures/events/<type>.json
      One SSE wire event per schema'd ``EventSpec`` in
      ``lib/agent_core/events.py``, constructed through the real
      ``build_event`` gate (a field the schema does not declare cannot
      appear here).
  contracts/fixtures/push/<type>.json
      One push-hub frame per ``PushFrameSpec``, via ``build_push_frame``.
  contracts/fixtures/sync_v3/<Schema>.json
      Minimal valid payload for each fail-closed decode boundary in
      ``lib/conversation_sync/service.py`` (snapshot, turn page, heartbeat,
      change, reset), sampled from ``OPENAPI_SCHEMAS`` and proven against
      the real ``decode()`` at generation time.
  contracts/fixtures/sync_v3/<Schema>.legacy-observation-attempt-id.json
      The persisted pre-``attemptId`` client-observation shape (the
      clone-500 incident class): raw decode must fail closed; the service
      read-adapter registry must lift it back onto the contract.

Usage:
    python3 scripts/gen_wire_fixtures.py           # regenerate
    python3 scripts/gen_wire_fixtures.py --check   # verify freshness (CI)
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURES_DIR = os.path.join(ROOT, "contracts", "fixtures")

# Fail-closed decode boundaries served by ConversationSyncService. Keep in
# sync with the decode(...) call sites in lib/conversation_sync/service.py.
SYNC_DECODE_BOUNDARIES = (
    "ConversationSyncSnapshot",
    "ConversationTurnPage",
    "SyncHeartbeat",
    "ConversationChange",
    "SyncResetRequired",
)

# Boundaries carrying turns whose legacy persisted shape is fixtured.
SYNC_LEGACY_BOUNDARIES = (
    "ConversationSyncSnapshot",
    "ConversationTurnPage",
)

LEGACY_OBSERVATION_SUFFIX = ".legacy-observation-attempt-id"

_FIELD_SAMPLES = {
    "str": "s",
    "int": 1,
    "number": 1.5,
    "bool": True,
    "dict": {},
    "list": [],
}

# Explicit samples for pattern-constrained strings, keyed by the exact
# declared pattern. The sampler proves each against the pattern before use.
_PATTERN_SAMPLES = {
    "^plan_[0-9a-f]{24}$": "plan_" + "a" * 24,
    "^[0-9a-f]{24}$": "a" * 24,
    "^[0-9a-f]{64}$": "a" * 64,
    "^sha256:[0-9a-f]{64}$": "sha256:" + "a" * 64,
    "^[A-Za-z0-9][A-Za-z0-9._:~-]*$": "s",
}


def _field_sample(kind: str):
    from lib.agent_core.events import FIELD_KINDS

    for alternative in (part.strip() for part in kind.split("|")):
        if alternative == "None":
            continue
        if alternative not in FIELD_KINDS:
            raise ValueError(f"unknown FieldSpec kind alternative: {kind!r}")
        return _FIELD_SAMPLES[alternative]
    return None


def _event_fixtures() -> dict[str, object]:
    from lib.agent_core.events import all_event_specs, build_event

    fixtures = {}
    for spec in all_event_specs():
        if spec.schema is None:
            continue
        kwargs = {field.name: _field_sample(field.kind) for field in spec.schema}
        fixtures[f"events/{spec.type}.json"] = build_event(spec.type, **kwargs)
    return fixtures


def _push_fixtures() -> dict[str, object]:
    from lib.agent_core.events import all_push_frame_specs, build_push_frame

    fixtures = {}
    for spec in all_push_frame_specs():
        if spec.schema is None:
            continue
        kwargs = {field.name: _field_sample(field.kind) for field in spec.schema}
        fixtures[f"push/{spec.type}.json"] = build_push_frame(spec.type, **kwargs)
    return fixtures


# ── OpenAPI minimal sampler (sync-v3 boundaries) ─────────────────────


def _sample_openapi(schema, stack: tuple[str, ...]):
    from lib.conversation_sync.generated_contract import OPENAPI_SCHEMAS

    if not isinstance(schema, dict):
        return None
    ref = schema.get("$ref")
    if isinstance(ref, str):
        prefix = "#/components/schemas/"
        target_name = ref[len(prefix) :]
        if target_name in stack:
            # Recursive document references (snapshot ref blocks) bottom out
            # at the empty object; one level is enough to pin the shape.
            return {}
        target = OPENAPI_SCHEMAS.get(target_name)
        if target is None:
            raise ValueError(f"unknown schema reference: {ref!r}")
        return _sample_openapi(target, stack + (target_name,))
    if "const" in schema:
        return copy.deepcopy(schema["const"])
    choices = schema.get("enum")
    if isinstance(choices, list) and choices:
        return copy.deepcopy(choices[0])
    for variants_key in ("oneOf", "anyOf"):
        variants = schema.get(variants_key)
        if isinstance(variants, list) and variants:
            return _sample_openapi(variants[0], stack)
    kind = schema.get("type")
    if kind == "null":
        return None
    if kind == "string":
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            sample = _PATTERN_SAMPLES.get(pattern)
            if sample is None or not re.search(pattern, sample):
                raise ValueError(
                    f"pattern string needs an explicit matching sample in "
                    f"_PATTERN_SAMPLES: {stack[-1]} ({pattern!r})"
                )
            return sample
        minimum = schema.get("minLength")
        value = "s" * max(1, minimum if isinstance(minimum, int) else 1)
        maximum = schema.get("maxLength")
        if isinstance(maximum, int):
            value = value[:maximum]
        return value
    if kind in ("integer", "number"):
        value = 1 if kind == "integer" else 1.5
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)):
            value = max(value, minimum)
            value = int(value) if kind == "integer" else value
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            value = maximum
        return value
    if kind == "boolean":
        return True
    if kind == "array":
        items = schema.get("items")
        count = schema.get("minItems")
        count = count if isinstance(count, int) and count > 0 else 1
        maximum = schema.get("maxItems")
        if isinstance(maximum, int):
            count = min(count, maximum)
        return [_sample_openapi(items, stack) for _ in range(count)]
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties")
        if isinstance(properties, dict):
            return {
                name: _sample_openapi(child, stack)
                for name, child in properties.items()
            }
        minimum = schema.get("minProperties")
        additional = schema.get("additionalProperties")
        if isinstance(minimum, int) and minimum > 0 and isinstance(additional, dict):
            # Keyed maps (sharedToolDocuments, snapshotProjectionRefs):
            # synthesize the minimum membership so minProperties holds.
            key_schema = schema.get("propertyNames")
            key = (
                _sample_openapi(key_schema, stack)
                if isinstance(key_schema, dict)
                else "k"
            )
            if not isinstance(key, str):
                key = "k"
            value = _sample_openapi(additional, stack)
            # Only minProperties == 1 exists in the contract today; a
            # pattern-constrained multi-key map would need distinct keys.
            # The guard above requires minimum >= 1, and duplicate keys
            # collapse, so one literal entry is exactly the old behavior.
            return {key: value}
        return {}
    # Unconstrained JSON slot (empty schema) — the empty object is the
    # minimal value every consumer must tolerate.
    return {} if isinstance(schema, dict) else None


def _strip_observation_attempt_ids(payload) -> int:
    """Remove every clientObservation attemptId; return the strip count."""
    stripped = 0
    for turn in payload.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        trace = (turn.get("projection") or {}).get("timingTrace")
        observations = (trace or {}).get("clientObservations")
        for observation in observations or []:
            if isinstance(observation, dict) and "attemptId" in observation:
                del observation["attemptId"]
                stripped += 1
    return stripped


def _sync_fixtures() -> dict[str, object]:
    from lib.conversation_sync.generated_contract import OPENAPI_SCHEMAS
    from lib.conversation_sync.validation import decode

    fixtures = {}
    for schema_name in SYNC_DECODE_BOUNDARIES:
        payload = _sample_openapi(OPENAPI_SCHEMAS[schema_name], (schema_name,))
        # Generation-time proof: a fixture the real decode boundary rejects
        # must fail the generator, not the test suite.
        decode(schema_name, payload)
        fixtures[f"sync_v3/{schema_name}.json"] = payload
    for schema_name in SYNC_LEGACY_BOUNDARIES:
        legacy = copy.deepcopy(fixtures[f"sync_v3/{schema_name}.json"])
        stripped = _strip_observation_attempt_ids(legacy)
        if stripped == 0:
            raise ValueError(
                f"{schema_name}: legacy fixture is vacuous — the sampled "
                "modern payload carried no clientObservation attemptId to "
                "strip; is TurnTimingTrace.clientObservations still "
                "declared?"
            )
        fixtures[f"sync_v3/{schema_name}{LEGACY_OBSERVATION_SUFFIX}.json"] = legacy
    return fixtures


def render_fixtures() -> dict[str, str]:
    """Repo-relative fixture path → committed bytes (JSON, sorted keys)."""
    payloads = {}
    payloads.update(_event_fixtures())
    payloads.update(_push_fixtures())
    payloads.update(_sync_fixtures())
    return {
        path: json.dumps(payload, indent=2, sort_keys=True) + "\n"
        for path, payload in sorted(payloads.items())
    }


def _committed_fixtures() -> dict[str, str]:
    committed = {}
    for directory, _subdirs, files in os.walk(FIXTURES_DIR):
        for name in files:
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, FIXTURES_DIR)
            with open(path, encoding="utf-8") as handle:
                committed[relative] = handle.read()
    return committed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="fail unless the committed corpus is fresh"
    )
    args = parser.parse_args()
    rendered = render_fixtures()
    committed = _committed_fixtures()
    stale = sorted(path for path in rendered if committed.get(path) != rendered[path])
    orphans = sorted(path for path in committed if path not in rendered)
    if args.check:
        if stale or orphans:
            for path in stale:
                print(
                    f"fixture drift: contracts/fixtures/{path} is stale",
                    file=sys.stderr,
                )
            for path in orphans:
                print(
                    f"fixture orphan: contracts/fixtures/{path} has no "
                    "generator output",
                    file=sys.stderr,
                )
            print("run python3 scripts/gen_wire_fixtures.py", file=sys.stderr)
            return 1
        print(f"wire fixtures fresh: {len(rendered)} fixtures")
        return 0
    for path in orphans:
        os.remove(os.path.join(FIXTURES_DIR, path))
        print(f"removed contracts/fixtures/{path}")
    for path, content in rendered.items():
        destination = os.path.join(FIXTURES_DIR, path)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        temporary = f"{destination}.publish-{os.getpid()}"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, destination)
    print(f"wrote {len(rendered)} fixtures under contracts/fixtures/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
