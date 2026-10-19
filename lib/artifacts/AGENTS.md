# Artifact ownership map

`core.py` owns the storage facade and format/dedupe projection; durable rows and
blob ownership belong to Sidecar artifact operations and native `artifact.rs`.
Read `docs/modules/task_engine.md` for publication decisions.

`review.py` binds artifact versions to the shared owner-acceptance mechanics
in `lib/acceptance/`. Wire authority: `contracts/artifact_review_v1.schema.json`; behavior and
verification: `docs/contracts/ARTIFACT_REVIEW.md`. Task completion is separate.
`lib/acceptance/dependencies.py` adapts selected working-fact bindings; requirement
sources use `read_criteria` so decisions never recursively evaluate themselves.

`response_files.py` implements `contracts/response_file_v1.schema.json`: verified
roots, no-follow reads, immutable source identity and owner-checked delivery.
`file_snapshot.py` owns the shared descriptor-pinned reader, streaming digest and
exact quote checks. It grants no roots; publication and working-file provenance
supply their existing authority. Verify mutation, cancellation and bounded memory.
`scanner.py` selects renderable assistant content; explicit operator selection
and snippet thresholds must not become persistence admission policy.

Verify producer, scanner, API, versioning, response-file mutation/cancellation
and owner-isolation suites under `tests/artifacts/`, then native artifact and
differential RPC tests. Source/frozen export behavior belongs to `pdf_export.py`;
its optional renderer must not tax artifact storage or application idle.
`markdown_view.py` shares inert Markdown body rendering between standalone
previews and PDF export. Preserve escaped raw HTML and the escaped-source
fallback; verify artifact view CSP/ownership and PDF export alongside browser
review of both historical and current Markdown versions.

`image_validation_worker.py` owns complete response-image decoding in an isolated
source/frozen process. The server must never change global Pillow policy. Its
role dispatches through `lib/paper/builds/workers.py` before application startup;
both desktop specs include it. Verify image byte/pixel/frame fidelity, parent
death and missing-Python-stream frozen entry before changing this boundary.
