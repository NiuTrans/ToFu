# Repository scan and ranking

`outline.py` owns source admission/extraction; `rank.py` owns weighted ranking
and fallback; `api.py` owns public scan/rank/render composition. The caller
owns scanner authorization and lifecycle; no module owns a repository cache.

`_native_rank.py` lazily prefers the optional independently installed
`tofu-repo-kernel` wheel. Package absence/admission failure uses NetworkX;
computation failure keeps reference-count fallback. The process retains only
one immutable native binding; graph arrays are call-local. Algorithm/C ABI
source authority is `packages/tofu-repo-kernel/`, never the experiment wrappers.

Verify with an installed wheel: `tests/repo_map/test_native_rank.py`, neighboring
`tests/repo_map`, `tests/meta/test_repo_map.py`, `tests/tasks_pkg/test_repo_map_handler.py`,
then `benchmarks/repository_index/native_publication_measure.py` pilot/full runs.
The owning decision is `docs/modules/tools_execution.md`.
