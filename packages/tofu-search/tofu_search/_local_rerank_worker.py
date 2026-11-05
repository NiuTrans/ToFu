"""One disposable local scoring process; invoked by local_rerank.py only.

The parent supplies its admitted artifact and owns deadlines/resources/output.
This file uses no host or package bootstrap imports. Heavy dependencies load
only after every pinned local file is verified. There is no remote-code or
network loading and no generation loop: only the creator's two-label score.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from pathlib import Path

if __package__:
    from .generated_process_lifetime import bind_parent_lifetime
else:
    from generated_process_lifetime import bind_parent_lifetime

PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements "
    "based on the Query and the Instruct provided. Note that the answer "
    'can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"
# This grouping is part of the evaluated numerical scoring policy. Changing
# padding/batch shape can change BF16 ranks and requires quality qualification.
SCORING_BATCH_SIZE = 8


class WorkerFailure(RuntimeError):
    pass


def verify_snapshot(root: Path, artifact: dict) -> None:
    for expected in artifact["source"]["files"]:
        name = expected["name"]
        if Path(name).name != name or name in (".", ".."):
            raise ValueError("invalid snapshot filename")
        path = root / name
        if path.stat().st_size != expected["bytes"]:
            raise ValueError("snapshot size mismatch")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected["sha256"]:
            raise ValueError("snapshot hash mismatch")


def score_request(request: dict) -> dict:
    artifact = request["artifact"]
    root = Path(request["model_dir"])
    try:
        verify_snapshot(root, artifact)
    except (OSError, ValueError) as error:
        raise WorkerFailure("snapshot_invalid") from error
    if artifact["execution"] != {
        "backend": "transformers",
        "architecture": "Qwen3ForCausalLM",
        "scoring": "qwen3_yes_no",
        "weight_dtype": "bfloat16",
    }:
        raise ValueError("unsupported admitted execution")
    config = json.loads((root / "config.json").read_text())
    if config.get("model_type") != "qwen3" or config.get("architectures") != ["Qwen3ForCausalLM"]:
        raise ValueError("snapshot architecture mismatch")
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from transformers.utils.logging import disable_progress_bar
    except (ImportError, OSError) as error:
        raise WorkerFailure("dependency_unavailable") from error

    disable_progress_bar()
    torch.set_num_threads(request["threads"])
    started = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(
        root, local_files_only=True, trust_remote_code=False, padding_side="left"
    )
    prefix = tokenizer.encode(PREFIX, add_special_tokens=False)
    suffix = tokenizer.encode(SUFFIX, add_special_tokens=False)
    room = request["max_tokens"] - len(prefix) - len(suffix)
    if room <= 0:
        raise ValueError("token budget cannot hold the ranking template")
    model = (
        AutoModelForCausalLM.from_pretrained(
            root,
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
            dtype=torch.bfloat16,
            attn_implementation="sdpa",
        )
        .eval()
        .to("cpu")
    )
    loaded = time.monotonic()
    texts = [
        f"<Instruct>: {INSTRUCTION}\n<Query>: {request['query']}\n<Document>: {document}"
        for document in request["documents"]
    ]
    labels = [tokenizer.convert_tokens_to_ids(word) for word in ("no", "yes")]
    if len(set(labels)) != 2 or any(label == tokenizer.unk_token_id for label in labels):
        raise ValueError("invalid ranking labels")
    scores, shapes, batches = [], [], []
    offset = 0
    for size in request["group_sizes"]:
        for start in range(offset, offset + size, SCORING_BATCH_SIZE):
            batches.append((start, min(start + SCORING_BATCH_SIZE, offset + size)))
        offset += size
    for start, end in batches:
        inputs = tokenizer(
            texts[start:end],
            padding=False,
            truncation=True,
            return_attention_mask=False,
            max_length=room,
        )
        inputs["input_ids"] = [prefix + ids + suffix for ids in inputs["input_ids"]]
        inputs = tokenizer.pad(inputs, padding=True, return_tensors="pt")
        if len(shapes) < 64:
            shapes.append(list(inputs["input_ids"].shape))
        with torch.inference_mode():
            logits = (
                model(**inputs, use_cache=False, logits_to_keep=1).logits[:, -1, labels].float()
            )
            scores.extend(torch.nn.functional.log_softmax(logits, dim=1)[:, 1].exp().tolist())
    if any(not math.isfinite(value) for value in scores):
        raise ValueError("nonfinite ranking scores")
    return {
        "ok": True,
        "scores": scores,
        "metrics": {
            "load_seconds": loaded - started,
            "score_seconds": time.monotonic() - loaded,
            "batch_shapes": shapes,
            "batches": len(batches),
        },
    }


def main() -> int:
    try:
        # Bind before reading input or loading native libraries: the parent may
        # disappear without reaching its normal deadline/cancellation cleanup.
        try:
            if len(sys.argv) != 2:
                raise ValueError("expected the launching parent PID")
            bind_parent_lifetime(int(sys.argv[1]))
        except (OSError, ValueError) as error:
            raise WorkerFailure("parent_lifetime_unavailable") from error
        result = score_request(json.load(sys.stdin))
    except Exception as error:
        # Detailed dependency exceptions can contain local paths or inputs.
        # The model-facing caller receives only a stable bounded failure kind.
        result = {
            "ok": False,
            "kind": str(error) if isinstance(error, WorkerFailure) else "inference_failed",
        }
    sys.stdout.write(json.dumps(result, allow_nan=False) + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
