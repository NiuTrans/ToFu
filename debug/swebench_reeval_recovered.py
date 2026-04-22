#!/usr/bin/env python3
"""Re-evaluate just the patches that were recovered from the git-add-timeout bug.

Reads swebench_workdir/recovery_manifest.json to get the list of recovered
`{instance_id}__{tool}` stems, then runs evaluate_patch() on each in parallel.

Updates swebench_workdir/swebench_results.json in-place (preserves all other
results) and rewrites swebench_workdir/details/{stem}.json for each recovered
instance with the fresh evaluation data.

This is much faster than the full --reeval pass because we only touch the ~40
patches that were silently dropped, not all ~1700.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from debug.swebench_runner import (
    BenchmarkResult, _save_per_run_detail, evaluate_patch,
    load_swebench_instances, setup_all_conda_envs,
    MAX_EVAL_WORKERS,
)

WORKDIR = _PROJECT_ROOT / 'swebench_workdir'
PATCH_DIR = WORKDIR / 'patches'
DETAILS_DIR = WORKDIR / 'details'
MANIFEST_FILE = WORKDIR / 'recovery_manifest.json'
RESULTS_JSON = WORKDIR / 'swebench_results.json'
LOG_FILE = WORKDIR / 'reeval_recovered.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-5s %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler(str(LOG_FILE), mode='a', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger('reeval-recovered')


def _split_stem(stem: str) -> tuple[str, str] | None:
    """Split 'django__django-10097__cc-glm' -> ('django__django-10097', 'cc-glm')."""
    parts = stem.rsplit('__', 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def main() -> int:
    if not MANIFEST_FILE.exists():
        log.error('Recovery manifest missing: %s — run swebench_recover_empty_patches.py first',
                  MANIFEST_FILE)
        return 1
    manifest = json.loads(MANIFEST_FILE.read_text())
    recovered = manifest.get('details', {}).get('recovered', [])
    log.info('[Reeval] Loaded manifest: %d recovered patches', len(recovered))
    if not recovered:
        log.info('[Reeval] Nothing to re-evaluate.')
        return 0

    # Build instance index from the full dataset (needed by evaluate_patch)
    log.info('[Reeval] Loading SWE-bench Verified dataset...')
    instances = load_swebench_instances(load_all=True)
    inst_map = {inst.instance_id: inst for inst in instances}
    log.info('[Reeval] Indexed %d instances', len(inst_map))

    # Pre-build the conda envs we'll need
    needed_iids = {_split_stem(r['stem'])[0] for r in recovered if _split_stem(r['stem'])}
    needed = [inst_map[i] for i in needed_iids if i in inst_map]
    env_map = setup_all_conda_envs(needed)

    # Load existing results.json — we'll patch entries in-place.
    results_data = json.loads(RESULTS_JSON.read_text()) if RESULTS_JSON.exists() else {}
    results_list = results_data.get('results', [])
    key_to_idx = {
        f'{r["instance_id"]}__{r["tool"]}': i for i, r in enumerate(results_list)
    }
    log.info('[Reeval] Loaded results.json with %d entries', len(results_list))

    def _do_one(stem: str):
        parts = _split_stem(stem)
        if not parts:
            return stem, None, f'bad stem: {stem}'
        iid, tool = parts
        inst = inst_map.get(iid)
        if not inst:
            return stem, None, f'instance not in dataset: {iid}'

        patch_path = PATCH_DIR / f'{stem}.diff'
        if not patch_path.exists():
            return stem, None, f'patch file missing: {patch_path}'
        model_patch = patch_path.read_text()
        if not model_patch or model_patch.startswith('# (empty'):
            return stem, None, 'patch still empty after recovery'

        log.info('[Reeval] %s — evaluating (%d chars)', stem, len(model_patch))
        t0 = time.time()
        eval_result = evaluate_patch(inst, model_patch, tool, WORKDIR, env_map or {})
        dt = time.time() - t0

        # Pull inference metadata from existing detail file (unchanged)
        detail_file = DETAILS_DIR / f'{stem}.json'
        inf = {}
        if detail_file.exists():
            try:
                inf = json.loads(detail_file.read_text()).get('inference', {})
            except Exception as e:
                log.warning('[Reeval] %s — could not parse existing detail: %s', stem, e)

        br = BenchmarkResult(
            instance_id=iid,
            repo=inst.repo,
            difficulty=inst.difficulty,
            tool=tool,
            duration_s=inf.get('duration_s', 0),
            cost_usd=inf.get('cost_usd', 0),
            input_tokens=inf.get('input_tokens', 0),
            output_tokens=inf.get('output_tokens', 0),
            cache_read_tokens=inf.get('cache_read_tokens', 0),
            cache_write_tokens=inf.get('cache_write_tokens', 0),
            num_turns=inf.get('num_turns', 0),
            resolved=eval_result.resolved,
            patch_applies=eval_result.patch_applies,
            fail_to_pass_passed=sum(1 for v in eval_result.fail_to_pass_results.values() if v),
            fail_to_pass_total=len(eval_result.fail_to_pass_results),
            pass_to_pass_passed=sum(1 for v in eval_result.pass_to_pass_results.values() if v),
            pass_to_pass_total=len(eval_result.pass_to_pass_results),
            error=eval_result.error or '',
        )

        # Persist full detail (trajectory preservation for HF release)
        dummy_inf = type('obj', (object,), {
            'model_patch': model_patch,
            'duration_s': inf.get('duration_s', 0),
            'cost_usd': inf.get('cost_usd', 0),
            'input_tokens': inf.get('input_tokens', 0),
            'output_tokens': inf.get('output_tokens', 0),
            'cache_read_tokens': inf.get('cache_read_tokens', 0),
            'cache_write_tokens': inf.get('cache_write_tokens', 0),
            'num_turns': inf.get('num_turns', 0),
            'error': inf.get('error', ''),
            'raw_output': inf.get('raw_output', ''),
        })()
        _save_per_run_detail(WORKDIR, inst, tool, dummy_inf, eval_result, br)
        return stem, br, f'{dt:.1f}s'

    # Run in parallel
    updated_count = 0
    failed_count = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=MAX_EVAL_WORKERS,
                            thread_name_prefix='reeval-rec') as ex:
        futs = {ex.submit(_do_one, r['stem']): r['stem'] for r in recovered}
        for fut in as_completed(futs):
            stem = futs[fut]
            try:
                _, br, info = fut.result()
            except Exception as e:
                log.error('[Reeval] %s — crashed: %s', stem, e, exc_info=True)
                failed_count += 1
                continue
            if br is None:
                log.warning('[Reeval] %s skipped — %s', stem, info)
                failed_count += 1
                continue
            status = '✅' if br.resolved else '❌'
            log.info('[Reeval] %s %s F2P=%d/%d P2P=%d/%d (%s)',
                     status, stem,
                     br.fail_to_pass_passed, br.fail_to_pass_total,
                     br.pass_to_pass_passed, br.pass_to_pass_total, info)

            # Patch the results list in-place
            key = f'{br.instance_id}__{br.tool}'
            from dataclasses import asdict
            rec = asdict(br)
            if key in key_to_idx:
                results_list[key_to_idx[key]] = rec
            else:
                results_list.append(rec)
                key_to_idx[key] = len(results_list) - 1
            updated_count += 1

    # Save final results
    results_data['results'] = results_list
    results_data.setdefault('metadata', {})['recovery_reeval_applied'] = {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'updated': updated_count,
        'failed': failed_count,
        'total_recovered_candidates': len(recovered),
    }
    RESULTS_JSON.write_text(json.dumps(results_data, indent=2))
    elapsed = time.time() - t0
    log.info('[Reeval] Done in %.1fs: updated=%d, failed=%d', elapsed, updated_count, failed_count)
    log.info('[Reeval] Updated results.json: %s', RESULTS_JSON)
    return 0


if __name__ == '__main__':
    sys.exit(main())
