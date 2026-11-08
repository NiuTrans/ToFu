# Model metadata guidance

This directory owns developer-maintained model facts and capability vocabulary.
The admission and ownership contract is `docs/MODEL_REGISTRATION.md`.

- `data/*.json` describes provider offerings; do not advertise a local artifact
  as a provider offering unless that provider actually supplies it.
- `model_inference_defaults.json` contains maintained exact Model defaults.
  Regenerate its boot-pinned Python consumer with
  `scripts/gen_api_v1_model_routing_contract.py`; verify the generator's `--check`
  mode and `tests/model_routing/test_inference_defaults_*.py`. The running
  server adopts maintained defaults on restart, with its schema/compiler.
- `capability_taxonomy.py` owns capability meaning, model kinds, media price
  units and chat exclusion; verify
  `tests/model_routing/test_capability_taxonomy_parity.py` after changing it.
- `media_model_facts.json` holds maintained official media facts per exact
  Creator/Model identity, each with a checked date and HTTPS sources;
  `media_facts.py` validates it against the model-routing schema definitions.
  Record only officially published values, never a reseller price or a guess.
  Verify `tests/model_routing/test_media_model_facts.py`; the running server
  adopts edits on restart.
- `local_model_artifacts.json` pins public local artifacts. `local_artifacts.py`
  validates its contract and uses common Model admission before export.
  `scripts/gen_packaged_local_models.py` produces the standalone search manifest;
  never edit that generated consumer or enable runtime supply during export.
  Verify the local-artifact and packaged-manifest tests named by the admission
  contract, then run the generator's `--check` mode.
