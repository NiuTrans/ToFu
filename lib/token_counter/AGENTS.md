# Token counter ownership

`api.py` owns resolution/fallback and public CountResult metadata; `base.py`
owns request text, structural and image charges. `tiktoken_counter.py` owns
content-free text caches, multiplicity and call-local identity receipts.
`native.py` borrows the current app NativeHost count-only encoder, preserving
tiktoken surrogate repair; it never owns model/account/storage authority.

Read docs/modules/context_engineering.md and docs/DEVELOPER_RUNTIME.md.
Verify tests/token_counter plus native_host/app_assembly neighbors for lifecycle
changes. Native ABI constants derive from native_token_count_abi_v1.json via
gen_native_token_count_abi.py; never hand-edit generated consumers. Explicit
native selection must not allocate token-ID vectors, spawn a local executor,
or change cache/fallback/confidence semantics.
