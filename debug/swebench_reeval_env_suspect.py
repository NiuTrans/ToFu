#!/usr/bin/env python3
"""Re-evaluate patches whose prior failure looked environment-driven.

Reads the curated list at ``swebench_workdir/cc_env_suspect.json`` (or a path
passed on the CLI) and re-runs ``evaluate_patch()`` for each row using the
patch already saved under ``swebench_workdir/patches/``.

Does NOT re-run model inference — this only re-evaluates the stored patch
against the (now-fixed) conda envs.

Updates ``swebench_workdir/swebench_results.json`` in-place (preserves all
other results) and rewrites ``swebench_workdir/details/{stem}.json`` for each
re-evaluated instance with fresh test outcomes.

This is the targeted reeval pass for the env-fix (markupsafe<2.1 for sphinx,
real 'py' package for pylint envs, etc.).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
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
RESULTS_JSON = WORKDIR / 'swebench_results.json'
LOG_FILE = WORKDIR / 'reeval_env_suspect.log'
DEFAULT_SUSPECT = WORKDIR / 'cc_env_suspect.json'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-5s %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler(str(LOG_FILE), mode='a', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger('reeval-env')


def _load_rows(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if isinstance(data, dict) and 'rows' in data:
        return data['rows']
    if isinstance(data, list):
        return data
    raise ValueError(f'Unexpected suspect file format: {path}')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--suspect-file', default=str(DEFAULT_SUSPECT),
                    help='JSON with rows of {instance_id, tool}')
    ap.add_argument('--max-workers', type=int, default=MAX_EVAL_WORKERS)
    args = ap.parse_args()

    rows = _load_rows(Path(args.suspect_file))
    log.info('[ReevalEnv] Loaded %d suspect rows from %s', len(rows), args.suspect_file)
    if not rows:
        log.info('[ReevalEnv] Nothing to re-evaluate.')
        return 0

    log.info('[ReevalEnv] Loading SWE-bench Verified dataset...')
    instances = load_swebench_instances(load_all=True)
    inst_map = {inst.instance_id: inst for inst in instances}
    log.info('[ReevalEnv] Indexed %d instances', len(inst_map))

    needed_iids = {r['instance_id'] for r in rows}
    needed = [inst_map[i] for i in needed_iids if i in inst_map]
    env_map = setup_all_conda_envs(needed)

    results_data = json.loads(RESULTS_JSON.read_text()) if RESULTS_JSON.exists() else {}
    results_list = results_data.get('results', [])
    key_to_idx = {
        f'{r["instance_id"]}__{r["tool"]}': i for i, r in enumerate(results_list)
    }
    log.info('[ReevalEnv] Loaded results.json with %d entries', len(results_list))

    def _do_one(row: dict):
        iid = row['instance_id']
        tool = row['tool']
        stem = f'{iid}__{tool}'
        inst = inst_map.get(iid)
        if not inst:
            return stem, None, f'instance not in dataset: {iid}'

        patch_path = PATCH_DIR / f'{stem}.diff'
        if not patch_path.exists():
            return stem, None, f'patch file missing: {patch_path}'
        model_patch = patch_path.read_text()
        if not model_patch or model_patch.startswith('# (empty'):
            return stem, None, 'patch empty — skipping'

        log.info('[ReevalEnv] %s — evaluating (%d chars)', stem, len(model_patch))
        t0 = time.time()
        eval_result = evaluate_patch(inst, model_patch, tool, WORKDIR, env_map or {})
        dt = time.time() - t0

        detail_file = DETAILS_DIR / f'{stem}.json'
        inf = {}
        if detail_file.exists():
            try:
                inf = json.loads(detail_file.read_text()).get('inference', {})
            except Exception as e:
                log.warning('[ReevalEnv] %s — could not parse existing detail: %s', stem, e)

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

    updated_count = 0
    failed_count = 0
    flipped_to_resolved = 0
    t0 = time.time()
    prev_status_by_key = {k: results_list[i]['resolved'] for k, i in key_to_idx.items()}

    with ThreadPoolExecutor(max_workers=args.max_workers,
                            thread_name_prefix='reeval-env') as ex:
        futs = {ex.submit(_do_one, r): r for r in rows}
        for fut in as_completed(futs):
            row = futs[fut]
            stem = f'{row["instance_id"]}__{row["tool"]}'
            try:
                _, br, info = fut.result()
            except Exception as e:
                log.error('[ReevalEnv] %s — crashed: %s', stem, e, exc_info=True)
                failed_count += 1
                continue
            if br is None:
                log.warning('[ReevalEnv] %s skipped — %s', stem, info)
                failed_count += 1
                continue
            status = '✅' if br.resolved else '❌'
            prev_resolved = prev_status_by_key.get(f'{br.instance_id}__{br.tool}', False)
            flipped = ' 🔄→✅' if (br.resolved and not prev_resolved) else (
                ' 🔄→❌' if (not br.resolved and prev_resolved) else '')
            log.info('[ReevalEnv] %s %s%s F2P=%d/%d P2P=%d/%d (%s)',
                     status, stem, flipped,
                     br.fail_to_pass_passed, br.fail_to_pass_total,
                     br.pass_to_pass_passed, br.pass_to_pass_total, info)

            if br.resolved and not prev_resolved:
                flipped_to_resolved += 1

            key = f'{br.instance_id}__{br.tool}'
            rec = asdict(br)
            if key in key_to_idx:
                results_list[key_to_idx[key]] = rec
            else:
                results_list.append(rec)
                key_to_idx[key] = len(results_list) - 1
            updated_count += 1

    results_data['results'] = results_list
    results_data.setdefault('metadata', {})['env_fix_reeval_applied'] = {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'updated': updated_count,
        'failed': failed_count,
        'flipped_to_resolved': flipped_to_resolved,
        'total_candidates': len(rows),
        'suspect_file': args.suspect_file,
    }
    backup = RESULTS_JSON.with_suffix('.before_env_reeval.json')
    if not backup.exists() and RESULTS_JSON.exists():
        backup.write_bytes(RESULTS_JSON.read_bytes())
        log.info('[ReevalEnv] Backed up results to %s', backup)
    RESULTS_JSON.write_text(json.dumps(results_data, indent=2))
    dt = time.time() - t0
    log.info('[ReevalEnv] Done in %.1fs — updated=%d failed=%d flipped_to_resolved=%d',
             dt, updated_count, failed_count, flipped_to_resolved)
    return 0


if __name__ == '__main__':
    sys.exit(main())
