#!/usr/bin/env python3
"""Offline conformance checker for recorded AG-UI event streams.

Reads a recorded stream (SSE `text/event-stream` or JSONL), validates it against
the AG-UI 1.0 run/text/tool/state ordering rules, and reports the difference
between the spec-strict verdict and a simulation of the TypeScript SDK's
post-enforcement view (unknown members stripped, malformed RUN_FINISHED outcome
treated as success). A stream that only the strict pass rejects is a
"silent_accept": the client keeps reading as if nothing happened.

Zero dependencies. No network. Exit codes: 0 clean, 1 findings, 2 usage/input.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

EVENT_TYPES = {
    "RUN_STARTED", "RUN_FINISHED", "RUN_ERROR",
    "STEP_STARTED", "STEP_FINISHED",
    "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END",
    "TEXT_MESSAGE_CHUNK",
    "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_RESULT",
    "REASONING_START", "REASONING_END", "REASONING_MESSAGE_START",
    "REASONING_MESSAGE_CONTENT", "REASONING_MESSAGE_END", "REASONING_SIGNATURE",
    "STATE_SNAPSHOT", "STATE_DELTA", "MESSAGES_SNAPSHOT",
    "ACTIVITY_SNAPSHOT", "ACTIVITY_DELTA",
    "SUBAGENT_STARTED", "SUBAGENT_FINISHED", "SUBAGENT_ERROR",
    "RAW", "CUSTOM",
}
TERMINAL = {"RUN_FINISHED", "RUN_ERROR"}
OUTCOMES = {"success", "interrupt", "cancelled"}
TEXT_OPEN = {"TEXT_MESSAGE_START": "TEXT_MESSAGE_END", "TEXT_MESSAGE_CONTENT": None}


@dataclass
class Finding:
    code: str
    level: str
    index: int
    message: str
    event_type: str = ""
    silent_accept: bool = False

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "level": self.level,
            "event_index": self.index,
            "event_type": self.event_type,
            "message": self.message,
            "silent_accept": self.silent_accept,
        }


@dataclass
class State:
    active_run: bool = False
    terminated: bool = False
    run_id: str | None = None
    thread_id: str | None = None
    text_open: dict[str, str] = field(default_factory=dict)   # messageId -> role
    tool_open: dict[str, str] = field(default_factory=dict)   # toolCallId -> name
    steps_open: set[str] = field(default_factory=set)
    reasoning_open: set[str] = field(default_factory=set)


def _obj(value) -> bool:
    return isinstance(value, dict)


def parse_stream(raw: bytes, fmt: str) -> tuple[list[dict], list[Finding]]:
    """Parse SSE or JSONL bytes into events. Returns (events, parse_findings)."""
    findings: list[Finding] = []
    text = raw.decode("utf-8", errors="replace")
    events: list[dict] = []
    if fmt == "jsonl":
        for ln, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                findings.append(Finding("E000", "error", ln - 1,
                                        f"line {ln}: invalid JSON: {exc.msg}", "?", False))
        return events, findings
    # SSE: dispatch on blank lines, concatenate `data:` lines per event.
    data: list[str] = []
    for ln, line in enumerate(text.splitlines(), 1):
        if line == "":
            if data:
                payload = "\n".join(data)
                try:
                    events.append(json.loads(payload))
                except json.JSONDecodeError as exc:
                    findings.append(Finding("E000", "error", len(events),
                                            f"line {ln}: invalid SSE data JSON: {exc.msg}", "?", False))
                data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        try:
            events.append(json.loads("\n".join(data)))
        except json.JSONDecodeError as exc:
            findings.append(Finding("E000", "error", len(events),
                                    f"EOF: invalid SSE data JSON: {exc.msg}", "?", False))
    return events, findings


def _require(ev: dict, idx: int, etype: str, names: tuple[str, ...],
             findings: list[Finding]) -> None:
    for name in names:
        if name not in ev:
            findings.append(Finding("E010", "error", idx,
                                    f"{etype} missing required member `{name}`", etype))


def validate(events: list[dict], *, enforce_sim: bool = True) -> list[Finding]:
    findings: list[Finding] = []
    st = State()
    if not events:
        findings.append(Finding("E001", "error", 0, "stream is empty", ""))
        return findings
    for i, ev in enumerate(events):
        if not _obj(ev):
            findings.append(Finding("E002", "error", i, "event is not a JSON object", ""))
            continue
        etype = ev.get("type")
        if not isinstance(etype, str):
            findings.append(Finding("E002", "error", i, "event has no string `type`", ""))
            continue
        if etype not in EVENT_TYPES:
            findings.append(Finding("W001", "warning", i,
                                    f"unknown event type `{etype}` (consumer must tolerate it)", etype))
            continue
        if i == 0 and etype not in ("RUN_STARTED", "RUN_ERROR"):
            findings.append(Finding("E003", "error", i,
                                    f"stream must open with RUN_STARTED or RUN_ERROR, got {etype}", etype))
        if st.terminated:
            findings.append(Finding("E004", "error", i, f"{etype} after the run was closed", etype))
        if etype == "RUN_STARTED":
            if st.active_run:
                findings.append(Finding("E005", "error", i, "RUN_STARTED while a run is still active", etype))
            st.active_run, st.terminated = True, False
            st.run_id, st.thread_id = ev.get("runId"), ev.get("threadId")
            if not isinstance(ev.get("runId"), str) or not isinstance(ev.get("threadId"), str):
                findings.append(Finding("E006", "error", i,
                                        "RUN_STARTED requires string runId and threadId", etype))
        elif etype == "RUN_FINISHED":
            _require(ev, i, etype, ("threadId", "runId"), findings)
            outcome = ev.get("outcome", "__absent__")
            if outcome != "__absent__":
                if not _obj(outcome):
                    findings.append(Finding("E011", "error", i,
                                            "RUN_FINISHED.outcome must be an object", etype))
                else:
                    otype = outcome.get("type")
                    if not isinstance(otype, str):
                        findings.append(Finding("E011", "error", i,
                                                "RUN_FINISHED.outcome.type must be a string", etype))
                    elif otype not in OUTCOMES:
                        findings.append(Finding("E012", "error", i,
                                                f"RUN_FINISHED.outcome.type `{otype}` is not described by the 1.0 schema", etype))
            if st.terminated or not st.active_run:
                findings.append(Finding("E007", "error", i, "RUN_FINISHED without an active run", etype))
            st.active_run, st.terminated = False, True
        elif etype == "RUN_ERROR":
            _require(ev, i, etype, ("message",), findings)
            st.active_run, st.terminated = False, True
        elif etype == "TEXT_MESSAGE_START":
            _require(ev, i, etype, ("messageId",), findings)
            mid = ev.get("messageId")
            if isinstance(mid, str):
                if mid in st.text_open:
                    findings.append(Finding("E020", "error", i,
                                            f"TEXT_MESSAGE_START repeats open message `{mid}`", etype))
                st.text_open[mid] = str(ev.get("role", ""))
        elif etype == "TEXT_MESSAGE_CONTENT":
            _require(ev, i, etype, ("messageId", "delta"), findings)
            mid = ev.get("messageId")
            if isinstance(mid, str) and mid not in st.text_open:
                findings.append(Finding("E021", "error", i,
                                        f"TEXT_MESSAGE_CONTENT for `{mid}` before its START", etype))
        elif etype == "TEXT_MESSAGE_END":
            _require(ev, i, etype, ("messageId",), findings)
            mid = ev.get("messageId")
            if isinstance(mid, str):
                if mid not in st.text_open:
                    findings.append(Finding("E022", "error", i,
                                            f"TEXT_MESSAGE_END for `{mid}` without START", etype))
                st.text_open.pop(mid, None)
        elif etype == "TOOL_CALL_START":
            _require(ev, i, etype, ("toolCallId", "toolCallName"), findings)
            tid = ev.get("toolCallId")
            if isinstance(tid, str):
                if tid in st.tool_open:
                    findings.append(Finding("E030", "error", i,
                                            f"TOOL_CALL_START repeats `{tid}`", etype))
                st.tool_open[tid] = str(ev.get("toolCallName", ""))
        elif etype in ("TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_RESULT"):
            tid = ev.get("toolCallId")
            if etype != "TOOL_CALL_RESULT" and (not isinstance(tid, str) or tid not in st.tool_open):
                findings.append(Finding("E031", "error", i,
                                        f"{etype} for unknown toolCallId `{tid}`", etype))
            if etype == "TOOL_CALL_END" and isinstance(tid, str):
                st.tool_open.pop(tid, None)
        elif etype == "STEP_STARTED":
            _require(ev, i, etype, ("stepName",), findings)
            st.steps_open.add(str(ev.get("stepName")))
        elif etype == "STEP_FINISHED":
            name = str(ev.get("stepName"))
            if name not in st.steps_open:
                findings.append(Finding("E041", "error", i, f"STEP_FINISHED for unopened step `{name}`", etype))
            st.steps_open.discard(name)
        elif etype == "STATE_DELTA":
            delta = ev.get("delta")
            if not isinstance(delta, list):
                findings.append(Finding("E050", "error", i, "STATE_DELTA.delta must be a JSON Patch array", etype))
        elif etype == "ACTIVITY_DELTA":
            if "patch" not in ev:
                findings.append(Finding("E051", "error", i, "ACTIVITY_DELTA requires `patch`", etype))
        elif etype in ("REASONING_MESSAGE_START", "REASONING_MESSAGE_END"):
            mid = ev.get("messageId")
            if not isinstance(mid, str):
                findings.append(Finding("E060", "error", i, f"{etype} requires string messageId", etype))
        if st.terminated:
            # Anything still open when the run closed is a violation, but the
            # specific close-order check is emitted once at EOF below.
            pass
    if st.active_run and not st.terminated:
        findings.append(Finding("E070", "error", len(events) - 1,
                                "stream ended with a run still active (no RUN_FINISHED/RUN_ERROR)", ""))
    for mid in st.text_open:
        findings.append(Finding("E071", "error", len(events) - 1,
                                f"text message `{mid}` still open at stream end", "TEXT_MESSAGE_START"))
    for tid in st.tool_open:
        findings.append(Finding("E072", "error", len(events) - 1,
                                f"tool call `{tid}` still open at stream end", "TOOL_CALL_START"))
    for name in sorted(st.steps_open):
        findings.append(Finding("E073", "error", len(events) - 1,
                                f"step `{name}` still open at stream end", "STEP_STARTED"))

    if enforce_sim:
        # Upstream enforceEvents strips unrecognised members and, per issue
        # #3391, lets a malformed RUN_FINISHED outcome through as success.
        for f in findings:
            if f.code in ("E011", "E012") and f.event_type == "RUN_FINISHED":
                f.silent_accept = True
            if f.code == "E004" and f.event_type == "RUN_FINISHED":
                f.silent_accept = True
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agui-stream-verify")
    ap.add_argument("file", help="recorded stream: SSE or JSONL")
    ap.add_argument("--format", choices=("auto", "sse", "jsonl"), default="auto")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--no-enforce-sim", action="store_false", dest="enforce_sim")
    ap.add_argument("--fail-on", choices=("error", "warning", "never"), default="error")
    args = ap.parse_args(argv)

    try:
        raw = open(args.file, "rb").read()
    except OSError as exc:
        print(f"agui-stream-verify: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    fmt = args.format
    if fmt == "auto":
        head = raw.lstrip()[:1]
        fmt = "jsonl" if head in (b"{", b"[") else "sse"
    events, parse_findings = parse_stream(raw, fmt)
    findings = parse_findings + validate(events, enforce_sim=args.enforce_sim)

    errors = [f for f in findings if f.level == "error"]
    warnings = [f for f in findings if f.level == "warning"]
    silent = [f for f in findings if f.silent_accept]
    if args.fail_on == "never":
        failed = False
    elif args.fail_on == "warning":
        failed = bool(errors or warnings)
    else:
        failed = bool(errors)

    report = {
        "ok": not errors,
        "failed": failed,
        "file": args.file,
        "format": fmt,
        "mode": "strict+enforce-sim" if args.enforce_sim else "strict",
        "events": len(events),
        "counts": {"error": len(errors), "warning": len(warnings),
                   "silent_accept": len(silent)},
        "silent_accepts": [f.as_dict() for f in silent],
        "findings": [f.as_dict() for f in findings],
    }
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for f in findings:
            flag = " SILENT-ACCEPT" if f.silent_accept else ""
            print(f"{f.level.upper()} {f.code} @event {f.index} [{f.event_type}]{flag}: {f.message}")
        print(f"{len(events)} events, {len(errors)} error(s), {len(warnings)} warning(s), "
              f"{len(silent)} silent accept(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())