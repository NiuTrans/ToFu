# Paper build guidance

This package owns source snapshots, bounded compilation, build receipts and
reconstructible PDF/SyncTeX artifacts. Wire authority is
`contracts/paper_build_v1.schema.json`; the parent Paper guide applies.

`process.py` supervises native commands through `process_worker.py`.
`workers.py` is the frozen dispatcher used by both Desktop launchers.
`artifact_repository.py` is the application/storage adapter for retained builds;
it never enters the portable compiler dependency graph. Durable bytes, document
pins and upload/read lifecycles belong to Sidecar `paper_artifacts` operations.
`document_repository.py` addresses logical source bindings and reading revisions;
`saved_documents.py` opens retained manuscripts without source discovery or compilation.
Sidecar `paper_documents` owns durable generation fencing and reading-state CAS.
`artifact_reader.py` supplies seekable cache/retained streams. The repository
owns read identity and leases; HTTP and source location never require hydration.
`notify.py` returns failed-build verdicts to a live conversation inbox
(volatile, digest-deduped). `citation_audit.py` audits `\cite`/`\ref` against
.bib/`\label` inside frozen snapshots. `imports.py` runs arXiv e-print
download→extract→discover→select. `source_read.py` serves the browser's
read-only code view from durable bindings. `source_edit.py` is the browser's
journaled editing lane (sha256-fenced full writes + search/replace patches
through the project write tools). `proposals.py` parks agent-proposed edit
hunks for per-hunk user accept/reject (bounded in-memory store, accepts
re-validate through `tool_apply_diff`). `layout.py` reports page/section/
fullness maps from build artifacts without recompiling.

- All execution adapters produce the same build receipt. Analysis and browser
  callers consume artifacts; they never recompile to answer a layout query.
- Carry owner and document identity explicitly. A cache pathname grants no
  authority. Failed, cancelled or unverified work cannot replace a verified
  successful build or settle a research passing receipt.
- Build only frozen input snapshots. Outputs, logs and temporary tool state
  belong to the build directory, with finite byte/count/time limits.
- Keep the compiler independent of HTTP, application storage and device
  transport so the Desktop Agent can run the same implementation.

Verification: `tests/paper/test_paper_build_contract.py`,
`tests/paper/test_paper_build_sources.py`, `tests/paper/test_paper_build_compiler.py`,
`tests/paper/test_paper_build_process.py` (native TeX/probe budgets and retirement),
`tests/paper/test_paper_build_pdf_validation.py` (isolated parser and native budgets),
`tests/paper/test_paper_build_diagnostics.py` (structured file/line diagnostics),
`tests/paper/test_paper_build_inverse.py`/`layout.py`/`source_read.py`/`notify.py`
(agent loop and browser source interaction), `tests/paper/test_paper_citation_audit.py`,
`tests/paper/test_paper_arxiv_import.py`/`test_paper_archive.py`,
then the preview, layout, research and device boundary suites.

`overleaf.py` starts independent leased chunks through the shared demand owner;
completed bytes use one anonymous caller spool, never worker-written cache files.
The receiver consumes in canonical order, and every reader joins before spool
close/lease release. Verify Overleaf transfer, cancellation and identity tests.
