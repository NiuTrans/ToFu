"""lib/local_serve/tool_defs.py — LLM tool schemas for managed local serving.

The family lets the chat agent take a MODEL PATH (HF directory or GGUF
file) and deploy it as a managed local OpenAI-compatible server — the
user no longer starts vLLM/SGLang/Ollama/llama.cpp themselves. The classic
paste-an-API-URL flow is untouched; a managed deployment simply becomes an
owner-scoped model-routing provider access once it is running.

Flow contract the descriptions teach the model:

    local_serve_recommend read-only: probe hardware, rank the curated
                          open-model catalog against it, and name a cheap
                          cloud API alternative when local hardware is too
                          weak. Use when the user has NOT picked a model yet.
    local_serve_prepare   read-only: inspect the path, probe hardware,
                          return the exact engine + launch plan. ALWAYS run
                          this first and show the user the plan.
    local_serve_deploy    write, human-confirmed: install the engine
                          (bounded uv venv), start the server, register it.
                          Returns immediately; poll local_serve_status.
    local_serve_status    read: live phase (installing/starting/running/
                          failed) + last error + log tail.
    local_serve_list      read: all known deployments.
    local_serve_stop      write: SIGTERM the managed server (keeps the
                          deployment so it can be started again).
    local_serve_remove    write, human-confirmed: stop + unregister the
                          provider + forget the deployment.

These run on the machine hosting the Tofu server. If the user's model
files live on a DIFFERENT machine, managed deployment cannot reach them —
say so instead of probing paths that do not exist locally.
"""

LOCAL_SERVE_RECOMMEND_TOOL = {
    "type": "function",
    "function": {
        "name": "local_serve_recommend",
        "description": (
            "Probe this machine's GPU/RAM/disk and rank a curated catalog "
            "of open-weight models (Qwen3, DeepSeek-R1-Distill, Llama, "
            "Gemma…) against it: each candidate comes with the best variant "
            "(BF16 for vLLM / Q4_K_M GGUF for llama.cpp), the fit tier "
            "(comfortable/tight/extreme/cpu), and the exact download "
            "command. When the hardware cannot comfortably run a capable "
            "model, the response includes a cheap cloud API alternative "
            "(DeepSeek V4 Flash) instead — offer it honestly rather than "
            "pushing a hopeless local install. Read-only. Use this FIRST "
            "whenever the user wants a local model but has not picked one "
            "yet; once they choose, guide the download, then "
            "local_serve_prepare + local_serve_deploy."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "purpose": {
                    "type": "string",
                    "enum": ["chat", "coding", "reasoning", "vision",
                             "multilingual"],
                    "description": (
                        "Optional use-case filter. Omit for a general "
                        "ranking across all categories."
                    )
                },
            },
            "required": [],
        },
    },
}
LOCAL_SERVE_PREPARE_TOOL = {
    "type": "function",
    "function": {
        "name": "local_serve_prepare",
        "description": (
            "Inspect a local model path (HuggingFace directory or .gguf "
            "file), probe this machine's GPU/RAM/disk, and return the "
            "recommended engine with exact launch parameters, resource tier, "
            "and the OOM degradation ladder. Read-only — run this FIRST and "
            "relay the plan to the user before any deployment. Fails with a "
            "structured error when the path is missing or not a recognised "
            "model (never guess: ask the user for the real path)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_path": {
                    "type": "string",
                    "description": (
                        "Absolute path to the model on THIS machine: a "
                        "directory containing config.json (HF/transformers "
                        "layout) or a single .gguf file."
                    )
                },
                "engine": {
                    "type": "string",
                    "enum": ["vllm", "sglang", "ollama", "llamacpp"],
                    "description": (
                        "Optional engine override. Omit to let the planner "
                        "pick the most stable engine for the detected model "
                        "and hardware."
                    )
                },
            },
            "required": ["model_path"],
        },
    },
}

LOCAL_SERVE_DEPLOY_TOOL = {
    "type": "function",
    "function": {
        "name": "local_serve_deploy",
        "description": (
            "Deploy a local model as a managed server: install the selected "
            "engine into an isolated venv (bounded disk budget), launch it "
            "on 127.0.0.1 in the managed port band, wait for readiness "
            "(retrying with degraded parameters on OOM), and register the "
            "endpoint as a selectable model provider. REQUIRES human "
            "approval. Returns immediately after the plan is approved — the "
            "deployment proceeds in the background; poll local_serve_status "
            "with the returned instance_id until status is 'running' or "
            "'failed'. Always run local_serve_prepare first and only call "
            "this after the user agrees to the plan."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_path": {
                    "type": "string",
                    "description": "Same path passed to local_serve_prepare."
                },
                "engine": {
                    "type": "string",
                    "enum": ["vllm", "sglang", "ollama", "llamacpp"],
                    "description": "Optional engine override (same as prepare)."
                },
            },
            "required": ["model_path"],
        },
    },
}

LOCAL_SERVE_STATUS_TOOL = {
    "type": "function",
    "function": {
        "name": "local_serve_status",
        "description": (
            "Report one managed deployment: phase (planned/installing/"
            "starting/running/stopped/failed), live pid + /models probe, "
            "last error, and a log tail. Use it to poll after "
            "local_serve_deploy (deployments can take minutes — model load "
            "and first-run engine downloads are slow) and to diagnose a "
            "failed start. On 'failed' the response carries a structured "
            "diagnosis {kind, summary, suggestions, retryable} — relay the "
            "summary and suggestions, then act: retryable=true means "
            "re-calling local_serve_deploy is worthwhile (possibly after "
            "the user frees VRAM/disk); retryable=false means the model, "
            "engine, or driver must change first — offer to pick a smaller "
            "model via local_serve_recommend or the cloud alternative."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "Deployment id returned by local_serve_deploy."
                },
            },
            "required": ["instance_id"],
        },
    },
}

LOCAL_SERVE_LIST_TOOL = {
    "type": "function",
    "function": {
        "name": "local_serve_list",
        "description": (
            "List every known managed local deployment with its engine, "
            "model path, status, endpoint, and last error. Read-only."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

LOCAL_SERVE_STOP_TOOL = {
    "type": "function",
    "function": {
        "name": "local_serve_stop",
        "description": (
            "Stop a running managed deployment (SIGTERM to the server "
            "process group). The deployment stays registered as stopped and "
            "can be started again with local_serve_deploy. Stopping frees "
            "GPU/RAM but any conversation using the model loses it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "Deployment id from local_serve_list/status."
                },
            },
            "required": ["instance_id"],
        },
    },
}

LOCAL_SERVE_REMOVE_TOOL = {
    "type": "function",
    "function": {
        "name": "local_serve_remove",
        "description": (
            "Remove a managed deployment entirely: stop the server, "
            "unregister its provider (the model disappears from the model "
            "picker), and delete the ledger row. REQUIRES human approval. "
            "The engine venv and the user's model files are NOT deleted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {
                    "type": "string",
                    "description": "Deployment id from local_serve_list/status."
                },
            },
            "required": ["instance_id"],
        },
    },
}

LOCAL_SERVE_TOOLS = [
    LOCAL_SERVE_RECOMMEND_TOOL,
    LOCAL_SERVE_PREPARE_TOOL,
    LOCAL_SERVE_DEPLOY_TOOL,
    LOCAL_SERVE_STATUS_TOOL,
    LOCAL_SERVE_LIST_TOOL,
    LOCAL_SERVE_STOP_TOOL,
    LOCAL_SERVE_REMOVE_TOOL,
]

LOCAL_SERVE_TOOL_NAMES = frozenset({
    'local_serve_recommend', 'local_serve_prepare', 'local_serve_deploy',
    'local_serve_status', 'local_serve_list', 'local_serve_stop',
    'local_serve_remove',
})

__all__ = ['LOCAL_SERVE_TOOLS', 'LOCAL_SERVE_TOOL_NAMES']
