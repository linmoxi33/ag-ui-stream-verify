#!/usr/bin/env python3
"""Test runner: 12 cases, 7 of them behavioural (subprocess -> output/exit code)."""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import agui_stream_verify as v  # noqa: E402

CLI = os.path.join(ROOT, "agui_stream_verify.py")
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def run_cli(*args):
    p = subprocess.run([sys.executable, CLI, *args], cwd=ROOT,
                       capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def write(tmp, name, body):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


S = 'data: {"type":"RUN_STARTED","threadId":"t","runId":"r"}\n\n'
F = 'data: {"type":"RUN_FINISHED","threadId":"t","runId":"r"}\n\n'


@case
def clean_stream_parses():
    events, f = v.parse_stream((S + F).encode(), "sse")
    assert len(events) == 2 and not f, (events, f)


@case
def jsonl_parses():
    body = '{"type":"RUN_STARTED","threadId":"t","runId":"r"}\n{"type":"RUN_ERROR","message":"x"}\n'
    events, f = v.parse_stream(body.encode(), "jsonl")
    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_ERROR"] and not f


@case
def clean_has_no_findings():
    events, _ = v.parse_stream((S + F).encode(), "sse")
    assert v.validate(events) == []


@case
def malformed_outcome_is_silent_accept():
    body = ('data: {"type":"RUN_STARTED","threadId":"t","runId":"r"}\n\n'
            'data: {"type":"RUN_FINISHED","threadId":"t","runId":"r","outcome":{"type":123}}\n\n')
    events, _ = v.parse_stream(body.encode(), "sse")
    findings = v.validate(events)
    assert any(f.silent_accept and f.code == "E011" for f in findings), findings


@case
def unknown_outcome_type_is_error():
    body = ('data: {"type":"RUN_STARTED","threadId":"t","runId":"r"}\n\n'
            'data: {"type":"RUN_FINISHED","threadId":"t","runId":"r","outcome":{"type":"paused"}}\n\n')
    events, _ = v.parse_stream(body.encode(), "sse")
    assert any(f.code == "E012" for f in v.validate(events))


@case
def content_without_start_fails():
    body = ('data: {"type":"RUN_STARTED","threadId":"t","runId":"r"}\n\n'
            'data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"m","delta":"x"}\n\n' + F)
    events, _ = v.parse_stream(body.encode(), "sse")
    assert any(f.code == "E021" for f in v.validate(events))


@case
def unterminated_run_fails():
    events, _ = v.parse_stream(S.encode(), "sse")
    assert any(f.code == "E070" for f in v.validate(events))


@case
def cli_clean_exit_zero():
    rc, out, _ = run_cli("examples/in/clean.sse")
    assert rc == 0 and "0 error" in out, (rc, out)


@case
def cli_silent_exit_one_and_json():
    rc, out, _ = run_cli("examples/in/silent-outcome.sse", "--json")
    report = json.loads(out)
    assert rc == 1 and report["counts"]["silent_accept"] == 1, (rc, report)


@case
def cli_missing_file_exit_two():
    rc, _, err = run_cli("/nonexistent/stream.sse")
    assert rc == 2 and "cannot read" in err, (rc, err)


@case
def cli_bad_json_exit_one():
    with tempfile.TemporaryDirectory() as tmp:
        path = write(tmp, "bad.sse", "data: {not json}\n\n")
        rc, out, _ = run_cli(path, "--json")
        report = json.loads(out)
        assert rc == 1 and report["counts"]["error"] >= 1, (rc, report)


@case
def cli_fail_on_never_exit_zero():
    with tempfile.TemporaryDirectory() as tmp:
        path = write(tmp, "bad.sse", "data: {not json}\n\n")
        rc, out, _ = run_cli(path, "--json", "--fail-on", "never")
        report = json.loads(out)
        assert rc == 0 and report["ok"] is False and report["failed"] is False, (rc, report)


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(CASES) - failed} passed, {failed} failed ({len(CASES)} cases)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())