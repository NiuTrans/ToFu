"""Operator CLI for selected kernel file-read observations, not scientific acceptance.

The supplied command executes under the shared managed process runtime. Its
output is suppressed from the diagnostic JSON; application adapters retain it.
Use python -m scripts.trace_experiment_reads with explicit project/files/timeout.
"""
import argparse
import json
from lib.project_mod.file_read_probe import run_observed_process


def observe(project, paths, command, *, timeout_seconds, tracer=None, pin_inputs=False, expected_input_sha256=None):
    from lib.project_mod.run_command import _get_cmd_env
    report = run_observed_process(project, paths, command, timeout_seconds=timeout_seconds,
        tracer=tracer, pin_inputs=pin_inputs, expected_input_sha256=expected_input_sha256, child_env=_get_cmd_env(str(project)))
    report.pop('command_result')
    report.pop('evidence_result')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--file', action='append', required=True, dest='paths')
    parser.add_argument('--timeout-seconds', type=float, required=True)
    parser.add_argument('--pin-inputs', action='store_true', help='Bind whole @tofu-input:relative-path argv tokens to selected open file descriptors; contents remain mutable')
    parser.add_argument('--expected-sha256', action='append', default=[], metavar='PATH=SHA256', help='Require caller-provided frozen digests for every selected file before allocation')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    expected = None
    if args.expected_sha256:
        try:
            pairs = [value.rsplit('=',1) for value in args.expected_sha256]
            expected = dict(pairs)
            if len(expected) != len(pairs): raise ValueError('duplicate expected path')
        except ValueError: parser.error('Expected unique PATH=SHA256 entries')
    print(json.dumps(observe(args.project,args.paths,command,timeout_seconds=args.timeout_seconds,pin_inputs=args.pin_inputs,expected_input_sha256=expected),sort_keys=True))


if __name__ == '__main__': main()
