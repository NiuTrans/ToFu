"""lib/local_serve — Managed local model deployment.

Tofu's classic local-model flow is user-driven: the operator starts vLLM /
SGLang / Ollama / llama.cpp themselves and either configures its Connection in
Settings or lets ``lib/llm_dispatch/autodiscover_local.py`` find a well-known
loopback port. This package adds the managed path: the user hands over a MODEL PATH (a HuggingFace-format
directory or a ``.gguf`` file) and Tofu inspects it, probes the hardware,
picks the most stable engine + launch parameters for the observed resources,
installs the engine into an isolated venv, runs the server as a managed
child process, and registers the resulting endpoint as an owner-scoped
model-routing v2 ProviderAccess with pending model identities. From that
moment the regular dispatcher, health checks, and Settings surface own it.

Module map (import direction is strictly left-to-right, no cycles):

  _probe.py     model-path inspection (HF config + safetensors, GGUF header)
                and hardware probing (nvidia-smi, cgroup/RAM, disk, CPU)
  _plan.py      deterministic engine selection + resource-tiered launch
                parameter policy + OOM degradation ladders
  _env.py       isolated per-engine venv installation via uv, with a disk
                precheck against the LOCAL_SERVE budget
  _process.py   managed server lifecycle: spawn, bounded logs, readiness
                polling, OOM-ladder retry, stop/restart
  _store.py     durable instance ledger (data/config/local_serve.json)
  _register.py  owner-aware hand-off into the model-routing v2 authority

Safety rails:

* **Loopback only.** Managed servers bind 127.0.0.1 on the 18100-18199 band,
  never the well-known engine ports (8000/30000/11434), so they can neither
  shadow an operator-run engine nor confuse autodiscovery.
* **Everything is bounded.** Log files rotate at a fixed size, the ledger is
  capped, the disk budget for envs is explicit (default 20 GiB), and every
  subprocess wait has a timeout.
* **Durable vs reconstructible.** The ledger entry (what the user asked for
  and got) is durable state under data/config/. Envs, logs, and downloaded
  binaries under data/local_serve/ are reconstructible and may be reclaimed.
* **Agent-visible.** The chat agent drives this package through the
  ``local_serve_*`` tool family; installing packages and starting servers
  are approval-gated boundaries. Nothing here runs at import time.
* **Personal-mode host authority.** Managed child processes and their durable
  host ledger are refused outside personal mode until enterprise host resource
  scheduling and owner isolation exist. Endpoint routing itself is owner-scoped.
"""

__all__ = ['probe', 'plan']
