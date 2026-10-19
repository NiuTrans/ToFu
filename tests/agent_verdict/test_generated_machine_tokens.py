"""Executable parity for Python authority and generated browser sanitation."""
import json
from pathlib import Path
import subprocess

import pytest

from scripts.gen_agent_machine_tokens import ROOT, load_authority, render_outputs
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit


def machine_token_corpus():
    corpus = [
        '', '  unchanged outer spaces \n\n\n ',
        'A [progress: resolved=2 remaining=3] B',
        'A [verdict: continue_worker] B',
        '[VERDICT: STOP][VERDICT: BLOCKED][VERDICT: CONTINUE_PLANNER]',
        '  before [VU: TASK_DONE] after  ',
        '  [vu: task_done]  ', '[VU: task_done]',
        '[PROGRESS: resolved=-1 remaining=0]',
        '[PROGRESS: resolved=1 remaining=]', '[VERDICT: UNKNOWN]',
        '[PROGRESS: resolved=1remaining=2]', '[ VU: TASK_DONE ]',
        '[PROGRESS: resolved=1, remaining=2]',
        '[PROGRESS: resolved=1;remaining=2]',
        ' first\n\n[VU: TASK_DONE]\n\n\nlast ',
        '\ufeff [VU: TASK_DONE] \ufeff',
        '[verdıct: contİnue]', '[progreſſ: reſolved=1 remainıng=2]',
        '\ud800 unchanged lone surrogate \udfff',
    ]
    # Exercise every Python whitespace, including C0 separators and NEL, plus
    # BOM/zero-width-space which must NOT be accepted or stripped as whitespace.
    whitespace = [chr(code) for code in range(0x3100) if chr(code).isspace()]
    for space in whitespace + ['\ufeff', '\u200b']:
        corpus.extend([
            f'{space}before [VU: TASK_DONE] after{space}',
            f'[PROGRESS:{space}resolved=1{space}remaining=2{space}]',
            f'[VERDICT:{space}STOP{space}]',
            f'{space}unchanged{space}',
        ])
    # All Unicode decimal digits recognized by this Python, including astral
    # scripts; non-decimal numeric characters are malformed and remain intact.
    for code in range(0x110000):
        digit = chr(code)
        if digit.isdecimal():
            corpus.append(f' x [progress: resolved={digit} remaining={digit}] y ')
    for digit in ['²', 'Ⅳ', '-2', '1.0']:
        corpus.append(f'[PROGRESS: resolved={digit} remaining=0]')
    return corpus


def test_generated_outputs_are_current():
    for path, expected in render_outputs().items():
        assert path.read_text(encoding='utf-8') == expected, str(path.relative_to(ROOT))


def test_generated_typescript_matches_real_python_strip():
    bundle = native_module_path(
        '.native/agent-machine-tokens-parity.js',
        'frontend/src/conversation/presentation/agent-machine-tokens.generated.ts',
    )
    corpus = machine_token_corpus()
    result = subprocess.run(
        ['node', '-e',
         'require(process.argv[1]); '
         'const inputs = JSON.parse(require("node:fs").readFileSync(0, "utf8")); '
         'process.stdout.write(JSON.stringify(inputs.map(stripAgentMachineTokens)));',
         str(Path(bundle))],
        input=json.dumps(corpus), capture_output=True, text=True,
        cwd=ROOT, timeout=30, check=True,
    )
    actual = json.loads(result.stdout)
    strip = load_authority()['strip_machine_tokens']
    expected = [strip(text) for text in corpus]
    for text, python, typescript in zip(corpus, expected, actual, strict=True):
        assert typescript == python, repr(text)
    assert [strip(text) for text in expected] == expected


def test_check_detects_drift_without_writing(tmp_path, monkeypatch):
    import scripts.gen_agent_machine_tokens as generator

    output = tmp_path / 'stale.ts'
    output.write_text('stale', encoding='utf-8')
    monkeypatch.setattr(generator, 'ROOT', tmp_path)
    monkeypatch.setattr(generator, 'render_outputs', lambda: {output: 'current'})
    monkeypatch.setattr('sys.argv', ['gen_agent_machine_tokens.py', '--check'])
    with pytest.raises(SystemExit) as error:
        generator.main()
    assert error.value.code == 2
    assert output.read_text(encoding='utf-8') == 'stale'
