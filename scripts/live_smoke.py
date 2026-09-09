#!/usr/bin/env python3
"""Wire-protocol conformance fixture for the ReasonProxy chat-completions surface.

THIS IS NOT A BENCHMARK. It produces no reward, no pass rate and no capability
measurement, and nothing here may be reported as a result. It answers exactly
one question: does a proxy response still satisfy the wire invariants that
mini-swe-agent 2.4.6 depends on, including provider acceptance of an assistant
message that carries content *and* tool_calls on replay?

Benchmark measurement lives in scripts/benchmark.py:

    python scripts/benchmark.py run --frozen latest --conformance

Invariants, from the installed harness source
(minisweagent/models/utils/actions_toolcall.py, config/mini.yaml, 2.4.6):

  P1  every tool_call names the harness tool `bash`; any other name makes
      parse_toolcall_actions raise FormatError
  P2  each tool_call id is non-empty and unique in the message; mini-swe copies
      it into the following role:"tool" message
  P3  arguments parse as a JSON object carrying a string `command`
  P4  finish_reason is a non-empty provider string
  P5  one assistant choice whose content is a string or null
  P6  replaying that assistant message verbatim plus one role:"tool" reply per
      returned id is accepted: 200, and P1-P5 again

The fixture task is a fixed harmless echo of a token. Only the argv forms in
PERMITTED are executed, always via subprocess argv and never through a shell,
and the real returncode and output are what goes back on the wire. Any other
proposed command is answered with the harness's own not-executed sentinel,
because no observation exists for it. Nothing here is invented.

Exit status: 0 every probed condition satisfied P1-P6; 1 a request was refused
or an invariant was violated (the report says which); 2 nothing failed but the
model returned no tool_call, so the replay was never exercised.

Usage:

    python scripts/live_smoke.py --conditions ctl/zen-lightning rp/zen-lightning
    python scripts/live_smoke.py --base http://127.0.0.1:8100/v1 --conditions rp/cf-super
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

# Verbatim from minisweagent/models/utils/actions_toolcall.py (2.4.6). The
# fixture must offer the same tool the real harness offers, or it proves nothing.
BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Execute a bash command",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute",
                }
            },
            "required": ["command"],
        },
    },
}

# mini.yaml's system_template, i.e. what every real trial sends.
SYSTEM = "You are a helpful assistant that can interact with a computer."

TOKEN = "reasonproxy-wire-check"
# The only commands this fixture runs: matched as exact argv after shlex.split
# and executed as argv, never through a shell. Disclosed in the prompt below.
PERMITTED: tuple[tuple[str, ...], ...] = (
    ("echo", TOKEN),
    ("printf", r"%s\n", TOKEN),
)
PERMITTED_TEXT = " or ".join(shlex.join(argv) for argv in PERMITTED)

USER = (
    "This is a wire-protocol fixture for an OpenAI-compatible proxy. It is not a "
    "benchmark task, nothing you write is scored, and no ability of yours is being "
    "measured; only the shape of the messages is checked.\n\n"
    f"Call the bash tool once to print the token {TOKEN}. This fixture executes "
    f"exactly {PERMITTED_TEXT} and nothing else; any other command comes back "
    "explicitly not executed, with no output, because nothing will have run.\n\n"
    "The assistant turn already in this transcript was written by the fixture, not "
    "by you. Issue the command yourself anyway - the repeat is what is being checked."
)

# Fixture-authored opening turn: content plus a native tool_call, which is the
# shape mini-swe replays on every step after the first. Its tool result is the
# real local output of PERMITTED[0], produced at startup.
SEED_CONTENT = (
    "Fixture-scripted turn, not model output: printing the fixture token, so this "
    "request already carries an assistant message with content and a tool_call."
)
SEED_CALL_ID = "call_fixture_0"


class Violation(Exception):
    pass


def observation(result: dict[str, Any]) -> str:
    """mini.yaml's observation_template shape, i.e. what a real trial sends back."""
    return json.dumps(result, indent=2)


def execute(command: str) -> tuple[dict[str, Any], bool]:
    """Run `command` iff it is exactly one permitted argv; never through a shell.

    Returns (observation, executed): a permitted command yields its real
    returncode and output, anything else the harness's own not-executed
    sentinel (returncode -1, exception_info), because no observation exists.
    """
    try:
        argv = tuple(shlex.split(command))
    except ValueError as exc:
        argv, reason = (), f"unparseable command ({exc})"
    else:
        reason = f"this fixture executes only {PERMITTED_TEXT}"
    if argv not in PERMITTED:
        return {"returncode": -1, "output": "", "exception_info": f"action was not executed: {reason}"}, False
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        info = f"action was not executed: {type(exc).__name__}: {exc}"
        return {"returncode": -1, "output": "", "exception_info": info}, False
    return {"returncode": done.returncode, "output": done.stdout + done.stderr}, True


def post(
    base: str, key: str | None, session: str, payload: dict[str, Any], timeout: float
) -> tuple[int, dict[str, Any], float]:
    request = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        # Every real trial carries this header (mini-swe sends it through
        # extra_headers; Harbor injects {trial_name}__agent), so the fixture
        # must exercise the same session-routing path.
        headers={"Content-Type": "application/json", "X-Session-ID": session},
        method="POST",
    )
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw[:800]}
        return exc.code, parsed, time.monotonic() - started


def check_message(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Assert P1-P5 on one response; return (assistant message, tool_calls)."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise Violation(f"P5 expected exactly one choice, got {type(choices).__name__} {len(choices or [])}")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise Violation("P5 choice carries no message object")
    if message.get("role") != "assistant":
        raise Violation(f"P5 message role is {message.get('role')!r}, expected 'assistant'")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise Violation(f"P5 content must be a string or null, got {type(content).__name__}")

    finish_reason = choice.get("finish_reason")
    if not isinstance(finish_reason, str) or not finish_reason:
        raise Violation(f"P4 finish_reason is {finish_reason!r}")

    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        raise Violation("P1 tool_calls is not a list")
    seen: set[str] = set()
    for call in tool_calls:
        function = (call or {}).get("function") or {}
        name = function.get("name")
        if name != "bash":
            raise Violation(
                f"P1 outward tool_call named {name!r}; mini-swe's parser raises FormatError for "
                "any name other than 'bash', so a private call must never leak"
            )
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise Violation(f"P2 tool_call id is {call_id!r}")
        if call_id in seen:
            raise Violation(f"P2 duplicate tool_call id {call_id!r}")
        seen.add(call_id)
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            raise Violation(f"P3 arguments must be a JSON string, got {type(arguments).__name__}")
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise Violation(f"P3 arguments are not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("command"), str):
            raise Violation("P3 arguments lack a string 'command' field")
    if tool_calls and finish_reason not in ("tool_calls", "stop", "length"):
        raise Violation(f"P4 finish_reason {finish_reason!r} with tool_calls present")
    return message, tool_calls


def probe(
    base: str,
    key: str | None,
    condition: str,
    session: str,
    timeout: float,
    seed: dict[str, Any],
) -> dict[str, Any]:
    history: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER},
        {
            "role": "assistant",
            "content": SEED_CONTENT,
            "tool_calls": [
                {
                    "id": SEED_CALL_ID,
                    "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"command": shlex.join(PERMITTED[0])})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": SEED_CALL_ID, "content": observation(seed)},
    ]

    report: dict[str, Any] = {
        "condition": condition,
        "session_id": session,
        "turns": [],
        "failures": [],
        "returned_tool_call": False,
        "replay_exercised": False,
    }
    for turn in (1, 2):
        status, payload, elapsed = post(
            base, key, session, {"model": condition, "messages": history, "tools": [BASH_TOOL]}, timeout
        )
        record: dict[str, Any] = {"turn": turn, "http_status": status, "seconds": round(elapsed, 2)}
        if status != 200:
            error = payload.get("error") or payload
            record["error"] = error
            detail = error.get("message") if isinstance(error, dict) else None
            report["turns"].append(record)
            # A refused request (auth, quota, upstream outage) is a failed
            # request, not a message-shape violation; say which it was.
            report["failures"].append(
                f"turn {turn}: HTTP {status}, request failed with no message to check"
                + (f": {str(detail)[:200]}" if detail else "")
            )
            break
        try:
            message, tool_calls = check_message(payload)
        except Violation as violation:
            record["error"] = str(violation)
            report["turns"].append(record)
            report["failures"].append(f"turn {turn}: {violation}")
            break

        record.update(
            {
                "finish_reason": payload["choices"][0].get("finish_reason"),
                "content_chars": len(message.get("content") or ""),
                "tool_calls": len(tool_calls),
                "tool_call_ids": [call["id"] for call in tool_calls],
                "usage_total_tokens": (payload.get("usage") or {}).get("total_tokens"),
            }
        )
        report["turns"].append(record)

        if turn == 2:
            report["replay_exercised"] = True
            break

        report["returned_tool_call"] = bool(tool_calls)
        if not tool_calls:
            # A model choosing not to call the tool is not a proxy violation, and
            # there is nothing left to replay, so P6 stays unexercised.
            break
        # Replay exactly what mini-swe replays: the assistant message verbatim,
        # then one tool reply per returned id, each carrying only what ran.
        replies = []
        for call in tool_calls:
            command = json.loads(call["function"]["arguments"])["command"]
            result, ran = execute(command)
            record.setdefault("commands", []).append(command if len(command) <= 300 else command[:300] + "...")
            record.setdefault("executed", []).append(ran)
            replies.append({"role": "tool", "tool_call_id": call["id"], "content": observation(result)})
        history = history + [message] + replies
    return report


def default_session(condition: str) -> str:
    """Stable within the process, unique per condition, and obviously a fixture."""
    slug = re.sub(r"[^a-z0-9]+", "-", condition.lower()).strip("-")
    return f"live-smoke-{slug}-{os.getpid()}"


def main() -> int:
    parser = argparse.ArgumentParser(description="ReasonProxy wire-protocol conformance fixture (not a benchmark)")
    parser.add_argument("--base", default="http://127.0.0.1:8100/v1")
    parser.add_argument("--conditions", nargs="+", required=True, help="virtual model aliases to probe")
    parser.add_argument("--key-env", default="REASONPROXY_API_KEY", help="env var holding the proxy bearer token")
    parser.add_argument("--session-id", default=None, help="X-Session-ID to send; default is per-condition")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--out", default=None, help="write the JSON report here")
    args = parser.parse_args()

    key = os.environ.get(args.key_env) or None
    seed, seed_ran = execute(shlex.join(PERMITTED[0]))
    if not seed_ran:
        print(f"fixture cannot run: {seed['exception_info']}")
        return 2

    print("wire-protocol fixture: message shapes only. No reward, no score, no capability claim.")
    print(f"endpoint {args.base}  auth {'bearer ' + args.key_env if key else 'none'}")
    print(f"executes locally, argv only: {PERMITTED_TEXT}")
    print(f"seed observation (real): {json.dumps(seed)}\n")

    reports = [
        probe(args.base, key, condition, args.session_id or default_session(condition), args.timeout, seed)
        for condition in args.conditions
    ]
    for report in reports:
        if report["failures"]:
            verdict = "FAIL"
        elif report["replay_exercised"]:
            verdict = "PASS"
        else:
            verdict = "INCONCLUSIVE"
        turns = ", ".join(
            f"t{t['turn']}={t['http_status']}"
            + (f"/{t['tool_calls']}tc/{t['finish_reason']}" if "tool_calls" in t else "")
            for t in report["turns"]
        )
        print(f"{verdict:<13} {report['condition']:<24} {turns}  session {report['session_id']}")
        for failure in report["failures"]:
            print(f"        {failure}")
        if not report["failures"] and not report["returned_tool_call"]:
            print("        turn 1 returned no tool_call: a model choice on the wire, so P6 was not exercised")
        for turn in report["turns"]:
            if "content_chars" not in turn:  # the turn that failed
                continue
            print(
                f"        turn {turn['turn']}: content {turn['content_chars']}B, "
                f"ids {turn['tool_call_ids']}, {turn['seconds']}s"
            )
            for command, ran in zip(turn.get("commands", []), turn.get("executed", [])):
                print(f"          {'executed' if ran else 'not executed'}: {command!r}")

    if args.out:
        with open(args.out, "w") as handle:
            json.dump({"fixture": "wire-protocol", "not_a_benchmark": True, "reports": reports}, handle, indent=2)
        print(f"\nreport written: {args.out}")

    failed = [r["condition"] for r in reports if r["failures"]]
    if failed:
        print(f"\nP1-P6 not satisfied in: {', '.join(failed)}")
        return 1
    unexercised = [r["condition"] for r in reports if not r["replay_exercised"]]
    if unexercised:
        print(f"\nno failure, but the replay was never exercised in: {', '.join(unexercised)}")
        return 2
    print("\nall probed conditions satisfy P1-P6.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
