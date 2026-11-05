# Model metadata guidance

This directory owns developer-maintained model facts and capability vocabulary.
The admission and ownership contract is `docs/MODEL_REGISTRATION.md`.

- `data/*.json` describes provider offerings; do not advertise a local artifact
  as a provider offering unless that provider actually supplies it.
- `model_inference_defaults.json` contains maintained exact Model defaults.
- `capability_taxonomy.py` owns capability meaning and chat exclusion; verify
  `tests/model_routing/test_capability_taxonomy_parity.py` after changing it.
- `local_model_artifacts.json` pins public local artifacts. `local_artifacts.py`
  validates its contract and uses common Model admission before export.
  `scripts/gen_packaged_local_models.py` produces the standalone search manifest;
  never edit that generated consumer or enable runtime supply during export.
  Verify the local-artifact and packaged-manifest tests named by the admission
  contract, then run the generator's `--check` mode.
