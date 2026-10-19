# Native response resource projections

`__init__.py` owns scoped reads through existing Turn/Evolution repositories and
SDK host read ports. `mcp.py` admits exact registered read-only capabilities;
`validation.py` owns semantic relationships beyond the rich-response JSON schema.
No model-provided owner, path, endpoint or write operation is accepted here.

The rendering/operations contracts are `docs/RENDER_CONTRACT.md` and
`docs/RESOURCE_CONTROL.md`. Test `tests/artifacts/test_response_surfaces.py` and
`tests/visual/test_visual_dynamic_surfaces.py`, including cancellation, ownership
rechecks and failed reads. Keep provider execution and persistence in their owners.
