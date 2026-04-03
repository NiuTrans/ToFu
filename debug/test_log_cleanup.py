#!/usr/bin/env python3
"""Test the log cleanup function against the user's exact example."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.project_mod.tools import _clean_command_output

# ─── The user's exact example input ───────────────────────────────────
SAMPLE_INPUT = r"""Loading weights:   0%|          | 0/299 [00:00<?, ?it/s][Worker 0] Starting on cuda:0, processing 5021 samples

Loading weights:   0%|          | 0/299 [00:00<?, ?it/s][Worker 7] Starting on cuda:7, processing 5021 samples

Loading weights:   0%|          | 0/299 [00:00<?, ?it/s][Worker 4] Starting on cuda:4, processing 5021 samples

Loading weights:   0%|          | 0/299 [00:00<?, ?it/s][Worker 2] Starting on cuda:2, processing 5021 samples

Loading weights:   0%|          | 0/299 [00:00<?, ?it/s][Worker 1] Starting on cuda:1, processing 5021 samples

Loading weights:   0%|          | 0/299 [00:00<?, ?it/s][Worker 6] Starting on cuda:6, processing 5021 samples

Loading weights:   0%|          | 0/299 [00:00<?, ?it/s][Worker 3] Starting on cuda:3, processing 5021 samples

Loading weights:   0%|          | 0/299 [00:00<?, ?it/s]
Loading weights:   3%|▎         | 9/299 [00:00<00:04, 64.97it/s]
Loading weights:   3%|▎         | 9/299 [00:00<00:03, 79.13it/s]
Loading weights:   4%|▍         | 13/299 [00:00<00:02, 118.10it/s]
Loading weights:   5%|▍         | 14/299 [00:00<00:02, 128.46it/s]
Loading weights:   4%|▍         | 13/299 [00:00<00:03, 88.20it/s]
Loading weights:   5%|▍         | 14/299 [00:00<00:02, 125.83it/s]
Loading weights:   4%|▍         | 13/299 [00:00<00:02, 118.15it/s]
Loading weights:   4%|▍         | 13/299 [00:00<00:02, 95.88it/s]
Loading weights:   7%|▋         | 20/299 [00:00<00:03, 78.84it/s]
Loading weights:   7%|▋         | 20/299 [00:00<00:03, 79.61it/s]
Loading weights:   8%|▊         | 23/299 [00:00<00:02, 93.53it/s]
Loading weights:   7%|▋         | 22/299 [00:00<00:03, 75.85it/s]
Loading weights:   8%|▊         | 25/299 [00:00<00:03, 83.53it/s] 
Loading weights:   8%|▊         | 25/299 [00:00<00:02, 99.07it/s] 
Loading weights:   9%|▉         | 27/299 [00:00<00:03, 77.42it/s] 
Loading weights:   9%|▉         | 28/299 [00:00<00:03, 73.97it/s]
Loading weights:   9%|▉         | 27/299 [00:00<00:03, 81.02it/s] 
Loading weights:  10%|▉         | 29/299 [00:00<00:03, 72.12it/s]
Loading weights:  10%|█         | 30/299 [00:00<00:03, 72.28it/s]
Loading weights:  11%|█         | 33/299 [00:00<00:03, 81.13it/s]
Loading weights:  11%|█▏        | 34/299 [00:00<00:03, 74.53it/s]
Loading weights:  12%|█▏        | 36/299 [00:00<00:03, 73.80it/s]
Loading weights:  12%|█▏        | 36/299 [00:00<00:03, 80.67it/s]
Loading weights:  12%|█▏        | 36/299 [00:00<00:04, 64.31it/s]
Loading weights:  12%|█▏        | 36/299 [00:00<00:03, 70.65it/s]
Loading weights:  12%|█▏        | 37/299 [00:00<00:04, 65.47it/s]
Loading weights:  14%|█▍        | 42/299 [00:00<00:03, 74.36it/s]
Loading weights:  14%|█▎        | 41/299 [00:00<00:03, 74.90it/s]
Loading weights:  14%|█▍        | 42/299 [00:00<00:03, 68.31it/s]
Loading weights:  15%|█▍        | 44/299 [00:00<00:03, 73.89it/s]
Loading weights:  15%|█▌        | 45/299 [00:00<00:03, 68.96it/s]
Loading weights:  15%|█▌        | 45/299 [00:00<00:03, 72.72it/s]
Loading weights:  15%|█▌        | 45/299 [00:00<00:03, 65.82it/s]
Loading weights:  15%|█▌        | 45/299 [00:00<00:03, 71.75it/s]
Loading weights:  17%|█▋        | 50/299 [00:00<00:03, 76.75it/s]
Loading weights:  17%|█▋        | 50/299 [00:00<00:03, 72.40it/s]
Loading weights:  16%|█▋        | 49/299 [00:00<00:03, 63.25it/s]"""

# ─── Also test a full 0% → 100% progress bar sequence (single device) ───
SAMPLE_SINGLE_DEVICE = """
Downloading model:   0%|          | 0/100 [00:00<?, ?it/s]
Downloading model:  10%|█         | 10/100 [00:02<00:18, 5.00it/s]
Downloading model:  20%|██        | 20/100 [00:04<00:16, 5.00it/s]
Downloading model:  30%|███       | 30/100 [00:06<00:14, 5.00it/s]
Downloading model:  40%|████      | 40/100 [00:08<00:12, 5.00it/s]
Downloading model:  50%|█████     | 50/100 [00:10<00:10, 5.00it/s]
Downloading model:  60%|██████    | 60/100 [00:12<00:08, 5.00it/s]
Downloading model:  70%|███████   | 70/100 [00:14<00:06, 5.00it/s]
Downloading model:  80%|████████  | 80/100 [00:16<00:04, 5.00it/s]
Downloading model:  90%|█████████ | 90/100 [00:18<00:02, 5.00it/s]
Downloading model: 100%|██████████| 100/100 [00:20<00:00, 5.00it/s]
Done!
"""

# ─── Multi-device startup lines ───────────────────────────────────────
SAMPLE_MULTI_DEVICE = """[Worker 0] Starting on cuda:0, processing 5021 samples
[Worker 1] Starting on cuda:1, processing 5021 samples
[Worker 2] Starting on cuda:2, processing 5021 samples
[Worker 3] Starting on cuda:3, processing 5021 samples
[Worker 4] Starting on cuda:4, processing 5021 samples
[Worker 5] Starting on cuda:5, processing 5021 samples
[Worker 6] Starting on cuda:6, processing 5021 samples
[Worker 7] Starting on cuda:7, processing 5021 samples
"""


def run_test(name, input_text, expected_checks=None):
    print(f'\n{"═" * 70}')
    print(f'  TEST: {name}')
    print(f'{"═" * 70}')

    result = _clean_command_output(input_text)
    lines_in = len(input_text.strip().split('\n'))
    lines_out = len(result.strip().split('\n'))

    print(f'\n  Input:  {lines_in} lines')
    print(f'  Output: {lines_out} lines (compressed {lines_in - lines_out})')
    print(f'\n{"─" * 60}')
    print(result)
    print(f'{"─" * 60}')

    if expected_checks:
        for check_name, check_fn in expected_checks.items():
            ok = check_fn(result)
            status = '✅' if ok else '❌'
            print(f'  {status} {check_name}')
            if not ok:
                return False
    return True


if __name__ == '__main__':
    all_ok = True

    # Test 1: The user's exact multi-device interleaved progress bars
    all_ok &= run_test(
        'Multi-device interleaved progress bars (user example)',
        SAMPLE_INPUT,
        {
            'Fewer than 15 output lines': lambda r: len(r.strip().split('\n')) < 15,
            'Contains device count (×8 or ×7)': lambda r: '×' in r and 'device' in r,
            'Includes first (0%)': lambda r: '0%' in r,
            'Includes last progress': lambda r: '17%' in r or '16%' in r,
            'Worker startup lines collapsed with device range':
                lambda r: 'cuda:' in r,
        }
    )

    # Test 2: Single-device progress bar
    all_ok &= run_test(
        'Single-device progress bar (0%→100%)',
        SAMPLE_SINGLE_DEVICE,
        {
            'Includes 0%': lambda r: '0%' in r,
            'Includes ~50%': lambda r: '50%' in r or '40%' in r or '60%' in r,
            'Includes 100%': lambda r: '100%' in r,
            'Includes Done!': lambda r: 'Done!' in r,
            'No device annotation': lambda r: '×' not in r or 'device' not in r.split('×')[-1][:15] if '×' in r else True,
        }
    )

    # Test 3: Multi-device startup lines
    all_ok &= run_test(
        'Multi-device startup (no progress bars)',
        SAMPLE_MULTI_DEVICE,
        {
            'Shows first worker line': lambda r: '[Worker 0]' in r,
            'Shows device range': lambda r: 'cuda:0-7' in r,
            'Collapsed to few lines': lambda r: len(r.strip().split('\n')) <= 3,
        }
    )

    print(f'\n{"═" * 70}')
    print(f'  RESULT: {"ALL TESTS PASSED ✅" if all_ok else "SOME TESTS FAILED ❌"}')
    print(f'{"═" * 70}')
    sys.exit(0 if all_ok else 1)
