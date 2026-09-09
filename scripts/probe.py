#!/usr/bin/env python3
"""Conformance probe: does every backend a config actually uses satisfy the
protocol its role requires?

Three different things get confused when a live run misbehaves, so they are
probed separately here, and the fourth is never measured:

  reachability  the model id exists and answers at all.
  conformance   the wire protocol its configured role depends on: native tool
                calls, an honored tool_choice, replayed assistant/tool history,
                the branch request, the reducer's JSON checkpoint.
  surface       a parameter the harness sends that no roster file declares and
                an endpoint may simply reject.
  quality       how good the answers are. Not measured, ever: a PASS means the
                protocol held, not that the model reasoned well.

Every phase sends the request the runtime actually sends, assembled with the
engine's own payload builders rather than a hand-written lookalike, so a shape
that passes here is the shape that will run:

  catalog   [reachability] the model id appears in the provider's own /models
            listing. Absent is a failure, no listing at all is UNKNOWN.
  call      [conformance] a real completion with NO output cap: transport ok,
            finish reason not a truncation, and non-empty public content.
  tools     [conformance] native function calling under the production
            controller prompt and the real private-tool schema: the model emits
            some tool call at all. Whether it self-selects the private tool is
            recorded but never a pass criterion -- self-selection is the result
            being measured, not a requirement being enforced.
  parallel  [surface] `parallel_tool_calls: true`, which mini-swe-agent sends
            on every step (litellm model_kwargs) and the engine forwards to the
            controller verbatim. No roster declares it, so this is the only
            place a rejection is found before it kills every step of a run.
  forced    [conformance] a named tool_choice is honored instead of prose, with
            the private tool withheld exactly as a caller-forced turn arrives.
  replay    [conformance] the proxy's own outward assistant turn comes back as
            history: the provider's message with its raw metadata intact
            (reasoning blocks, refusals, vendor fields), the <deliberation>
            checkpoint prepended to it, and every tool call it made answered.
  branch    [conformance] one branch request as the fan-out builds it -- the
            whole callsite, the private call answered by the real branch shim,
            the caller's tool inventory plus the private tool, and
            `tool_choice="none"` as the only thing forbidding action.
  json      [conformance] response_format={"type": "json_object"} yields a
            parseable object at all (the reducer's transport requirement).
  reducer   [conformance] the real checkpoint instruction over the real reducer
            payload -- serialized callsite, tool inventory, earlier checkpoint,
            anonymized continuations -- validated by the engine's own
            parse_checkpoint. The schema is checked; the conclusion never is.

Which phases a backend must pass is derived from how the config uses it:
controller -> call, parallel, replay (plus tools and forced where the arm
advertises the private tool); branch -> call, replay, branch; reducer -> call,
json, reducer. --phases overrides that and probes exactly what you ask for.

No probe sends max_tokens, reasoning_effort or any other cap, so a truncated or
empty answer is a real property of the endpoint and is reported as a failure
rather than a pass. A provider rate limit is reported as LIMITED and stops that
backend's remaining phases: the transport already retried with backoff, and
probing harder is the busy loop a 429 forbids. No phase retries a call and no
phase substitutes another model.

Nothing secret and nothing quotable is printed: API keys and any ${VAR} values
expanded into the config (account ids, base URLs) are redacted from every line
of output and from the JSON report, and no phase prints model prose -- only
finish reasons, character and token counts, structural key names, and the
provider's own sanitized error text.

Exit status is nonzero if any probed check failed, or if a required check could
not be evaluated at all (missing credentials, or a rate limit).

    export CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=... OPENCODE_API_KEY=...
    python scripts/probe.py configs/research.yaml
    python scripts/probe.py configs/research.yaml --only br-,red-
    python scripts/probe.py configs/cloudflare.yaml --phases catalog,call
    python scripts/probe.py configs/research.yaml --json runs/onboarding/probe.json
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402

from reasonproxy.config import Config  # noqa: E402

# The auxiliary payloads are imported from the engine, including its
# module-private builders, instead of being re-created here. A probe that
# assembled its own branch turn or its own reducer message would certify a
# shape the runtime never sends, and would drift silently the first time the
# real one changed; these are the exact functions the request path uses.
from reasonproxy.engine import (  # noqa: E402
    Checkpoint,
    _branch_rejection,
    _inert,
    _known_tool_ids,
    _name,
    _no_output,
    _prepend_text,
    _reason_assistant_message,
    _reducer_data,
    _tool_results,
    classify,
    escape_tags,
    parse_checkpoint,
    render_body,
    render_checkpoint,
)
from reasonproxy.prompts import (  # noqa: E402
    CHECKPOINT_LISTS,
    CONTROLLER_PROMPTS,
    REASON_TOOL,
    REASON_WIRE_NAME,
    REDUCER_CHECKPOINT_V2,
    TAG,
    branch_shim,
    revisions,
)
from reasonproxy.upstream import (  # noqa: E402
    SESSION_HEADERS,
    USER_AGENT,
    Completion,
    Upstream,
    Usage,
    session_scope,
)

Msg = dict[str, Any]

# Phase order is execution order: a later phase may replay what an earlier one
# actually returned, which is the point of `replay`.
PHASES = (
    "catalog", "call", "tools", "parallel", "forced", "replay", "branch", "json", "reducer",
)

# What a phase measures, so a reader never mistakes a protocol result for a
# score. Printed as a legend and carried in the JSON report.
PHASE_KINDS = {
    "catalog": "reachability",
    "call": "conformance",
    "tools": "conformance",
    "parallel": "surface",
    "forced": "conformance",
    "replay": "conformance",
    "branch": "conformance",
    "json": "conformance",
    "reducer": "conformance",
}

TRUNCATED = frozenset({"length", "max_tokens"})

# A provider that is throttling says so in its own error text; the transport has
# already retried with backoff by the time one of these arrives.
_RATE_LIMIT_RE = re.compile(r"\b429\b|rate[ _-]?limit|too many requests|quota", re.IGNORECASE)

SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": "Run a shell command in the task repository.",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    },
}

# A realistic agent state where deliberating is the obviously right move.
TRAJECTORY: list[Msg] = [
    {"role": "system", "content": "You are a coding agent working in a repository. "
                                  "Use the shell tool to investigate."},
    {"role": "user", "content": "Equivalent normalized paths are colliding in the cache. Fix it."},
    {"role": "assistant", "content": "Running the focused tests.",
     "tool_calls": [{"id": "c0", "type": "function",
                     "function": {"name": "shell",
                                  "arguments": '{"cmd": "pytest -q tests/test_cache.py"}'}}]},
    {"role": "tool", "tool_call_id": "c0",
     "content": "FAILED tests/test_cache.py::test_equivalent_paths - "
                "AssertionError: cache miss for './a/../b' after storing 'b'\n"
                "1 failed, 41 passed"},
    {"role": "user", "content": "Two prior fix attempts already failed. Several diagnoses "
                                "remain plausible. Decide how to proceed."},
]

# The first four turns end on an answered tool call, so this prefix is legal
# history on a strict provider on its own.
HISTORY_PREFIX = TRAJECTORY[:4]

# Id used when no live controller turn of this backend produced a real private
# call, shaped like the ones the engine assigns.
PRIVATE_CALL_ID = "rp_probe_reason"

# Answer to a real tool call inside replayed history. The probe executes
# nothing, so it says that and invents no observation.
PROBE_TOOL_RESULT = (
    "[probe] Not executed. This conformance probe runs no tools, so there is no observation; "
    "the call is present only because the assistant turn that made it is being replayed."
)

# A checkpoint fixture, in the reducer's own field schema. It is probe *input*
# -- replayed history and the earlier-checkpoint section of a reducer payload --
# and never an expectation about what a model should conclude.
CHECKPOINT_FIELDS: dict[str, Any] = {
    "conclusion": "Cache keys are built before path canonicalization, so equivalent spellings "
                  "hash to different keys.",
    "evidence": ["test_equivalent_paths misses for './a/../b' after storing 'b'",
                 "41 other cache tests pass, so storage itself works"],
    "alternatives": ["the normalizer itself is wrong rather than the key order",
                     "two caches disagree and only one of them normalizes"],
    "next_action": "Read key construction and its call sites before editing anything.",
    "verification": ["re-run the focused test", "assert both spellings resolve to one key"],
    "carry_forward": ["two prior fix attempts already failed"],
}
CHECKPOINT_BODY = render_body(CHECKPOINT_FIELDS)

# What a reducer sees when it is updating an earlier checkpoint of the same turn.
EARLIER_CHECKPOINT = Checkpoint(
    internal=escape_tags(CHECKPOINT_BODY), public=CHECKPOINT_BODY, reduced=True
)

# Continuation fixtures for the reducer payload: same agent, real disagreement,
# nothing executed. Inputs only -- the reducer's own text is never graded.
CONTINUATIONS = (
    "The failing case is './a/../b' against a stored 'b', so the key is almost certainly built "
    "from the raw string. Read the key builder and every call site before touching the "
    "normalizer; a fix inside the normalizer would move the bug rather than remove it.\n"
    "Verification: one test asserting both spellings map to a single key.",
    "Two attempts already failed, which suggests the normalizer is not the only writer. Grep for "
    "every place a cache key is constructed; if two call sites disagree, normalizing in one of "
    "them is exactly the failure already observed twice.",
    "Consider that canonicalization may be correct and the collision is a hash-vs-equality "
    "mismatch: equal keys that hash differently would produce this miss with 41 other tests "
    "passing. Cheapest decisive check is printing the key for both spellings.",
)


# --------------------------------------------------------------------------- #
# secrets
# --------------------------------------------------------------------------- #

class Redactor:
    """Replaces every configured secret with its variable name.

    Provider errors, base URLs and account ids can all carry credentials or
    account identity; a probe report is meant to be pasted into an issue or
    committed as evidence, so nothing sensitive may reach it.
    """

    def __init__(self, cfg: Config, raw: str) -> None:
        names = {p.api_key_env for p in cfg.providers.values()}
        names |= set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", raw))
        self._subs = sorted(
            ((val, f"[redacted:{name}]")
             for name in names
             if len(val := os.environ.get(name, "")) >= 4),
            key=lambda kv: len(kv[0]),
            reverse=True,
        )

    def __call__(self, text: str) -> str:
        for secret, label in self._subs:
            text = text.replace(secret, label)
        return text


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Check:
    backend: str
    model: str
    phase: str
    state: str  # PASS | FAIL | UNKNOWN | NOKEY | LIMITED
    note: str = ""
    latency_ms: int | None = None
    # A property worth reporting that is not a failure: an ignored surface
    # parameter, a missing tool_call id, an answer the runtime tolerates.
    caveat: str = ""

    @property
    def failed(self) -> bool:
        return self.state == "FAIL"


@dataclass(slots=True)
class Outcome:
    state: str
    note: str
    latency_ms: int | None = None
    caveat: str = ""


def _verdict(c: Completion, *, want_content: bool = True) -> str | None:
    """Shared honesty gate: why this completion is not usable, or None."""
    if not c.ok:
        return f"{c.status}: {c.error or 'no detail'}"
    if c.finish_reason in TRUNCATED:
        return (f"finish_reason={c.finish_reason}: a provider-side default cap "
                f"truncated the answer (this probe sends no cap)")
    if want_content and not c.content.strip():
        if c.reasoning:
            return (f"HTTP 200 but content is empty; {len(c.reasoning)} chars arrived as "
                    f"reasoning only, which is not usable public output")
        return f"HTTP 200 but content is empty (finish_reason={c.finish_reason})"
    return None


def _rate_limited(c: Completion) -> bool:
    """The provider throttled us, as opposed to rejecting the request."""
    return not c.ok and bool(_RATE_LIMIT_RE.search(c.error or ""))


def _reject(c: Completion, *, want_content: bool = True) -> Outcome | None:
    """LIMITED for a rate limit, FAIL for anything else unusable, else None.

    A rate limit says nothing about conformance, so it is never recorded as a
    failure of the backend's role -- and it is never retried here either.
    """
    if _rate_limited(c):
        return Outcome(
            "LIMITED",
            f"provider rate limit after {c.attempts} transport attempt(s), not retried: "
            f"{c.error}",
            c.latency_ms,
        )
    if (bad := _verdict(c, want_content=want_content)) is not None:
        return Outcome("FAIL", bad, c.latency_ms)
    return None


# --------------------------------------------------------------------------- #
# per-backend probe state
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Probe:
    """One backend's sequence of phases, and what they learned about it."""

    up: Upstream
    backend: str
    model: str
    prompt: str          # controller prompt id used for the injected policy
    session: str         # conversation bound to every call this probe makes
    usage: Usage = field(default_factory=Usage)
    phase: str = ""
    limited: bool = False
    # The provider's own last usable assistant turn, kept verbatim so `replay`
    # sends back real provider metadata instead of a reconstruction.
    assistant: Msg | None = None
    assistant_from: str = ""
    # A real private call this backend emitted, if it ever self-selected one.
    private_turn: Msg | None = None
    private_id: str = PRIVATE_CALL_ID

    async def complete(
        self,
        messages: list[Msg],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        overrides: dict[str, Any] | None = None,
    ) -> Completion:
        c = await self.up.complete(
            self.backend, messages, tools=tools, tool_choice=tool_choice, overrides=overrides
        )
        # One row per upstream call, at the single call site, so every attempt
        # the probe paid for is accounted exactly once -- including the failed
        # and throttled ones.
        self.usage.add(self.phase, c)
        if _rate_limited(c):
            self.limited = True
        return c

    def remember(self, c: Completion) -> None:
        """Keep this turn for `replay`, exactly as the engine would replay it.

        `as_assistant_message()` is the same accessor `_compose` hands back to
        the caller, so reasoning blocks, refusals and vendor-specific fields
        stay attached to the turn instead of being flattened into content and
        tool_calls.
        """
        if c.ok and not c.truncated:
            self.assistant = c.as_assistant_message()
            self.assistant_from = self.phase


def _injected(messages: list[Msg], prompt: str) -> list[Msg]:
    """Runtime policy where `Engine._inject` puts it: after the caller's leading
    system/developer turns, never rewriting them."""
    _, text = CONTROLLER_PROMPTS[prompt]
    i = 0
    while i < len(messages) and messages[i].get("role") in {"system", "developer"}:
        i += 1
    return [*messages[:i], {"role": "system", "content": text}, *messages[i:]]


def _private_call_turn() -> Msg:
    """The assistant turn every branch and reducer payload is built around: the
    private call, alone, with empty arguments."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": PRIVATE_CALL_ID,
            "type": "function",
            "function": {"name": REASON_WIRE_NAME, "arguments": "{}"},
        }],
    }


def _fallback_assistant() -> Msg:
    """Stand-in for `replay` when no live turn of this backend was recorded."""
    return {
        "role": "assistant",
        "content": "Reading how cache keys are built before changing anything.",
        "tool_calls": [{
            "id": "probe_shell_1",
            "type": "function",
            "function": {"name": "shell", "arguments": '{"cmd": "grep -rn cache_key src"}'},
        }],
    }


# --------------------------------------------------------------------------- #
# phases
# --------------------------------------------------------------------------- #

async def phase_call(p: Probe) -> Outcome:
    c = await p.complete([{"role": "user", "content": "Reply with exactly: READY"}])
    if (bad := _reject(c)) is not None:
        return bad
    p.remember(c)
    return Outcome("PASS", f"finish={c.finish_reason}, {len(c.content.strip())} content chars, "
                           f"{len(c.reasoning)} reasoning chars", c.latency_ms)


async def phase_tools(p: Probe) -> Outcome:
    """Native function calling on the controller path, with the real private
    tool declared beside a caller tool."""
    c = await p.complete(_injected(TRAJECTORY, p.prompt), tools=[SHELL_TOOL, REASON_TOOL])
    if (bad := _reject(c, want_content=False)) is not None:
        return bad
    names = [_name(tc) for tc in c.tool_calls]
    if not names:
        return Outcome("FAIL", f"no tool call under a realistic agent prompt "
                               f"(finish={c.finish_reason}, {len(c.content)} chars content, "
                               f"{len(c.reasoning)} chars reasoning): unusable as a controller",
                       c.latency_ms)
    p.remember(c)

    caveat = ""
    if REASON_WIRE_NAME not in names:
        selected = "chose another tool"
    else:
        # Classified with the engine's own rule, so a later phase replays a
        # private call the runtime would actually have accepted. Self-selection
        # and its protocol validity are recorded, never required.
        call, violation = classify(c, offered=True, known_ids=_known_tool_ids(TRAJECTORY))
        if call is not None:
            p.private_turn = _reason_assistant_message(c, call)
            p.private_id = call.id
            selected = "self-selected the private tool, protocol-valid"
            if call.real:
                caveat = (f"emitted {len(call.real)} real tool call(s) beside the private one; "
                          f"the engine answers them unexecuted")
        else:
            kind, detail = violation or ("unknown", "")
            selected = "self-selected the private tool"
            caveat = f"private-call protocol violation ({kind}): {detail}"
    return Outcome("PASS", f"emitted {len(names)} tool call(s) {sorted(set(names))} — {selected}",
                   c.latency_ms, caveat)


async def phase_parallel(p: Probe) -> Outcome:
    """`parallel_tool_calls: true` on the controller path.

    mini-swe-agent sends it in litellm `model_kwargs` on every step, so it
    reaches the controller as a caller parameter and is forwarded verbatim. No
    roster declares it -- an endpoint that rejects the key would otherwise fail
    every step of a run, in both the deliberating and the control arm.
    """
    c = await p.complete(
        _injected(TRAJECTORY, p.prompt),
        tools=[SHELL_TOOL, REASON_TOOL],
        overrides={"parallel_tool_calls": True},
    )
    if (bad := _reject(c, want_content=False)) is not None:
        return bad
    if _no_output(c):
        return Outcome("FAIL", f"parallel_tool_calls=true was accepted but the turn returned "
                               f"nothing usable (finish={c.finish_reason}, "
                               f"{len(c.reasoning)} reasoning chars)", c.latency_ms)
    p.remember(c)
    return Outcome("PASS", f"accepted parallel_tool_calls=true (the parameter mini-swe-agent "
                           f"sends): {len(c.tool_calls)} tool call(s), finish={c.finish_reason}",
                   c.latency_ms)


async def phase_forced(p: Probe) -> Outcome:
    """A caller-forced tool choice. The private tool is withheld, because a
    named tool_choice is the caller overruling deliberation."""
    c = await p.complete(
        _injected(TRAJECTORY, p.prompt),
        tools=[SHELL_TOOL],
        tool_choice={"type": "function", "function": {"name": "shell"}},
    )
    if (bad := _reject(c, want_content=False)) is not None:
        return bad
    names = [_name(tc) for tc in c.tool_calls]
    if "shell" not in names:
        return Outcome("FAIL", f"forced tool_choice=shell ignored: returned "
                               f"{sorted(set(names)) or 'prose'} (finish={c.finish_reason})",
                       c.latency_ms)
    p.remember(c)
    return Outcome("PASS", "honored a named tool_choice", c.latency_ms)


async def phase_replay(p: Probe) -> Outcome:
    """The next caller turn, exactly as the harness sends it back.

    The outward assistant message is the provider's own turn with the
    checkpoint prepended by `_compose`; a harness echoes that message verbatim
    on its next request, raw provider metadata included, followed by the
    results of whatever tool calls it carried. Reconstructing only content and
    tool_calls here would probe a shape nothing sends and would hide the
    endpoints that reject their own echoed reasoning blocks.
    """
    if p.assistant is not None:
        assistant = copy.deepcopy(p.assistant)
        origin = f"the {p.assistant_from} phase"
    else:
        assistant = _fallback_assistant()
        origin = "a canonical fixture (no earlier live turn to replay)"

    caveat = ""
    calls = list(assistant.get("tool_calls") or ())
    if calls and not all(tc.get("id") for tc in calls):
        # Unanswerable history otherwise: a tool result has nothing to name.
        calls = [tc if tc.get("id") else {**tc, "id": f"probe_call_{i}"}
                 for i, tc in enumerate(calls)]
        assistant["tool_calls"] = calls
        caveat = ("provider returned tool calls without ids; the probe assigned some so the turn "
                  "stays replayable")
    answers = [{"role": "tool", "tool_call_id": tc["id"], "content": PROBE_TOOL_RESULT}
               for tc in calls]

    block = render_checkpoint(CHECKPOINT_BODY, "d-probe")
    assistant["content"] = _prepend_text(assistant.get("content"), block + "\n\n")
    preserved = sorted(set(assistant) - {"role", "content", "tool_calls"})

    messages = [
        *_injected(HISTORY_PREFIX, p.prompt),
        assistant,
        *answers,
        {"role": "user", "content": "Continue from that checkpoint. State the next step."},
    ]
    c = await p.complete(messages, tools=[SHELL_TOOL])
    if (bad := _reject(c, want_content=False)) is not None:
        return bad
    if _no_output(c):
        return Outcome("FAIL", f"accepted the history but returned neither content nor a tool "
                               f"call (finish={c.finish_reason}, {len(c.reasoning)} reasoning "
                               f"chars)", c.latency_ms)
    return Outcome("PASS", f"accepted an assistant turn replayed from {origin} with "
                           f"{len(calls)} tool call(s), {len(answers)} tool result(s), "
                           f"raw metadata keys {preserved or 'none'} preserved, and a "
                           f"<{TAG}> checkpoint (finish={c.finish_reason})",
                   c.latency_ms, caveat)


async def phase_branch(p: Probe) -> Outcome:
    """One branch request, as `Engine._fanout` builds it.

    The branch inherits the entire callsite; the private call is answered by
    the real shim, which is also the whole branch instruction; the caller's
    tool inventory is declared together with the private tool so the replayed
    history stays legal on a strict provider, and `tool_choice="none"` is the
    only thing stopping the continuation from acting.
    """
    live_call = p.private_turn is not None
    assistant = p.private_turn if live_call else _private_call_turn()
    origin = "its own self-selected private call" if live_call else "a synthetic private call"
    messages = [
        *_injected(TRAJECTORY, p.prompt),
        assistant,
        *_tool_results(assistant, p.private_id, branch_shim()),
    ]
    c = await p.complete(messages, tools=[SHELL_TOOL, REASON_TOOL], tool_choice="none")
    if (bad := _reject(c, want_content=False)) is not None:
        return bad
    # The runtime's own usability rule, not a stricter probe-local one.
    if rejected := _branch_rejection(c):
        return Outcome("FAIL", f"continuation unusable as deliberation: {rejected} "
                               f"(finish={c.finish_reason})", c.latency_ms)
    caveat = ""
    if c.tool_calls:
        caveat = (f"ignored tool_choice=none and proposed {len(c.tool_calls)} tool call(s); the "
                  f"reducer receives them as inert proposals and nothing is executed")
    return Outcome("PASS", f"continued from the full callsite answered by the branch shim, off "
                           f"{origin}: {len(c.content.strip())} content chars, "
                           f"{len(c.reasoning)} reasoning chars, finish={c.finish_reason}",
                   c.latency_ms, caveat)


async def phase_json(p: Probe) -> Outcome:
    c = await p.complete(
        [{"role": "system", "content": "Return only a single JSON object."},
         {"role": "user", "content": 'Return exactly {"ok": true, "sum": <the value of 2+2>}.'}],
        overrides={"response_format": {"type": "json_object"}},
    )
    if (bad := _reject(c)) is not None:
        return bad
    try:
        parsed = json.loads(c.content)
    except ValueError as exc:
        return Outcome("FAIL", f"json_object mode returned unparseable content: {exc}",
                       c.latency_ms)
    if not isinstance(parsed, dict):
        return Outcome("FAIL", f"json_object mode returned a {type(parsed).__name__}, not an "
                               f"object", c.latency_ms)
    caveat = ""
    if parsed.get("ok") is not True or parsed.get("sum") != 4:
        # The object parsed, which is the transport property being probed.
        # Whether it answered is model quality and is not scored here.
        caveat = (f"the object did not answer the question (keys {sorted(parsed)[:8]}); "
                  f"json_object mode itself conformed")
    return Outcome("PASS", f"parseable JSON object, {len(parsed)} key(s)", c.latency_ms, caveat)


async def phase_reducer(p: Probe) -> Outcome:
    """The reducer request as `Engine._collapse` builds it.

    Real instruction, real single data message (serialized callsite, tool
    inventory, earlier checkpoint, anonymized continuations, missing-branch
    note) and `json_object`, validated with the engine's own schema gate. The
    checkpoint's shape is the criterion; its conclusion is never inspected.
    """
    assistant = p.private_turn if p.private_turn is not None else _private_call_turn()
    callsite = [*_injected(TRAJECTORY, p.prompt), assistant]
    blocks = [f"<continuation_{n + 1}>\n{_inert(text)}\n</continuation_{n + 1}>"
              for n, text in enumerate(CONTINUATIONS)]
    data = _reducer_data(callsite, [SHELL_TOOL], blocks, EARLIER_CHECKPOINT, 1)
    c = await p.complete(
        [{"role": "system", "content": REDUCER_CHECKPOINT_V2},
         {"role": "user", "content": data}],
        overrides={"response_format": {"type": "json_object"}},
    )
    if (bad := _reject(c)) is not None:
        return bad
    fields, error = parse_checkpoint(c.content)
    if fields is None:
        return Outcome("FAIL", f"reducer output is not a valid checkpoint: {error} "
                               f"({len(c.content)} content chars, finish={c.finish_reason})",
                       c.latency_ms)
    lists = ", ".join(f"{key}={len(fields[key])}" for key in CHECKPOINT_LISTS)
    return Outcome("PASS", f"validated checkpoint over the real reducer payload "
                           f"({len(data)} chars in, {len(c.content)} chars out; {lists})",
                   c.latency_ms)


PHASE_FNS: dict[str, Callable[[Probe], Awaitable[Outcome]]] = {
    "call": phase_call,
    "tools": phase_tools,
    "parallel": phase_parallel,
    "forced": phase_forced,
    "replay": phase_replay,
    "branch": phase_branch,
    "json": phase_json,
    "reducer": phase_reducer,
}


def probe_session(backend: str) -> str:
    """Stable within the process, one per backend, and obviously a probe.

    A backend's phases are related turns of one probe conversation, so they
    share an id; two backends are two conversations. The transport decides
    which providers are told about it -- the probe sets no headers itself.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", backend.lower()).strip("-")
    return f"rp-probe-{slug}-{os.getpid()}"


async def run_live_phases(
    up: Upstream, name: str, model: str, phases: list[str], prompt: str
) -> tuple[list[Check], Usage, str]:
    """One backend, its phases in order. Sequential per backend so a probe never
    self-throttles a single model into 429s."""
    p = Probe(up=up, backend=name, model=model, prompt=prompt, session=probe_session(name))
    out: list[Check] = []
    with session_scope(p.session):
        for phase in phases:
            if p.limited:
                out.append(Check(name, model, phase, "LIMITED",
                                 "not attempted: the provider is rate-limiting this probe, and "
                                 "probing through a 429 is the busy loop it forbids"))
                continue
            p.phase = phase
            try:
                r = await PHASE_FNS[phase](p)
            except Exception as exc:  # a transport/config fault is a real failure
                r = Outcome("FAIL", f"{type(exc).__name__}: {exc}")
            out.append(Check(name, model, phase, r.state, r.note, r.latency_ms, r.caveat))
    return out, p.usage, p.session


async def catalog_ids(up: Upstream, provider: str) -> set[str] | None:
    """Model ids the provider lists, or None if it serves no usable listing.

    Issued on the transport's own client, so credentials and our identity come
    only from the one place that owns them. A listing belongs to no
    conversation, so it carries no session header.
    """
    r = await up._client(provider).get("/models")  # noqa: SLF001
    if r.status_code >= 400:
        return None
    payload = r.json()
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return None
    ids = {row.get("id") for row in rows if isinstance(row, dict) and row.get("id")}
    return ids or None


def session_header_providers(cfg: Config) -> list[str]:
    """Providers the transport tells which conversation a request belongs to."""
    return sorted(
        name for name, prov in cfg.providers.items()
        if (httpx.URL(prov.base_url).host or "").lower() in SESSION_HEADERS
    )


# --------------------------------------------------------------------------- #
# role -> required phases
# --------------------------------------------------------------------------- #

def requirements(cfg: Config) -> tuple[dict[str, str], dict[str, set[str]]]:
    """How the config uses each backend, and therefore what it must satisfy."""
    roles: dict[str, set[str]] = {n: set() for n in cfg.backends}
    modes: dict[str, set[str]] = {n: set() for n in cfg.backends}
    need: dict[str, set[str]] = {n: {"catalog"} for n in cfg.backends}
    for vm in cfg.virtual_models.values():
        roles[vm.controller].add("controller")
        modes[vm.controller].add(vm.reason_mode)
        # `parallel` applies to every arm including the control: the harness
        # sends that parameter whatever the condition is.
        need[vm.controller] |= {"call", "parallel", "replay"}
        if vm.reason_mode in {"live", "noop"}:
            # These arms advertise the reason tool, so native function calling
            # and an honored tool_choice are load-bearing.
            need[vm.controller] |= {"tools", "forced"}
        for b in vm.branches:
            roles[b].add("branch")
            need[b] |= {"call", "replay", "branch"}
        if vm.reducer:
            roles[vm.reducer].add("reducer")
            need[vm.reducer] |= {"call", "json", "reducer"}
    labels = {
        n: ",".join(
            f"controller({'/'.join(sorted(modes[n]))})" if r == "controller" else r
            for r in sorted(roles[n])
        ) or "unused"
        for n in cfg.backends
    }
    return labels, need


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config")
    ap.add_argument("--only", default="", help="comma-separated backend name prefixes")
    ap.add_argument("--phases", default="",
                    help=f"comma-separated subset of {','.join(PHASES)}; "
                         "default is whatever each backend's role requires")
    ap.add_argument("--controller-prompt", default="concise", choices=sorted(CONTROLLER_PROMPTS),
                    help="prompt injected on every controller-path phase (default: the roster "
                         "default)")
    ap.add_argument("--json", dest="json_out", default="",
                    help="also write the report as JSON to this path")
    args = ap.parse_args()

    raw = Path(args.config).read_text()
    cfg = Config.load(args.config)
    redact = Redactor(cfg, raw)

    prefixes = tuple(p for p in args.only.split(",") if p)
    names = [n for n in cfg.backends if not prefixes or n.startswith(prefixes)]
    if not names:
        print(f"no backend matches --only {args.only!r}")
        return 1

    roles, need = requirements(cfg)
    asked = [p for p in PHASES if p in {q.strip() for q in args.phases.split(",") if q.strip()}]
    if args.phases and not asked:
        ap.error(f"--phases must name at least one of {','.join(PHASES)}")
    selected = {n: [p for p in PHASES if p in (set(asked) if asked else need[n])] for n in names}

    prompt_id, _ = CONTROLLER_PROMPTS[args.controller_prompt]
    # Only the endpoints this run actually calls, and only those that asked to
    # be told which conversation a request belongs to.
    used = {cfg.backends[n].provider for n in names}
    told = [p for p in session_header_providers(cfg) if p in used]
    print(f"config {args.config}  sha={cfg.sha}  backends={len(names)}")
    print(f"client {USER_AGENT}; controller-path prompt {prompt_id}")
    print("conversation id sent to: "
          + (", ".join(told) if told else "no probed provider asked for one"))
    print("phases requested: " + (",".join(asked) if asked else "role-derived") + "\n")

    checks: list[Check] = []

    # -- credentials: a phase that cannot run must say so, not pass quietly --
    missing = {
        name: cfg.providers[cfg.backends[name].provider].api_key_env
        for name in names
        if not os.environ.get(cfg.providers[cfg.backends[name].provider].api_key_env)
    }
    for name, env in missing.items():
        for phase in selected[name]:
            checks.append(Check(name, cfg.backends[name].model, phase, "NOKEY",
                                f"{env} is not set"))

    live = [n for n in names if n not in missing]
    providers = sorted({cfg.backends[n].provider for n in live if "catalog" in selected[n]})
    listings: dict[str, set[str] | None] = {}
    usages: dict[str, Usage] = {}
    sessions: dict[str, str] = {}

    up = Upstream(cfg)
    try:
        # -- catalog, once per provider ------------------------------------- #
        for provider in providers:
            try:
                listings[provider] = await catalog_ids(up, provider)
            except Exception as exc:
                listings[provider] = None
                print(f"  note  provider {provider}: /models listing unavailable "
                      f"({redact(f'{type(exc).__name__}: {exc}')})")
        for name in live:
            if "catalog" not in selected[name]:
                continue
            be = cfg.backends[name]
            ids = listings.get(be.provider)
            if ids is None:
                checks.append(Check(name, be.model, "catalog", "UNKNOWN",
                                    f"provider {be.provider} serves no usable /models listing; "
                                    f"catalog presence could not be checked"))
            elif be.model in ids:
                checks.append(Check(name, be.model, "catalog", "PASS",
                                    f"listed by provider {be.provider}"))
            else:
                checks.append(Check(name, be.model, "catalog", "FAIL",
                                    f"not in provider {be.provider}'s /models listing "
                                    f"({len(ids)} ids)"))

        # -- live conformance, backends in parallel -------------------------- #
        work = [
            run_live_phases(up, n, cfg.backends[n].model,
                            [p for p in selected[n] if p != "catalog"], args.controller_prompt)
            for n in live
        ]
        for name, (group, usage, session) in zip(live, await asyncio.gather(*work), strict=True):
            checks.extend(group)
            usages[name] = usage
            sessions[name] = session
    finally:
        await up.aclose()

    # -- report -------------------------------------------------------------- #
    shown = [p for p in PHASES if any(c.phase == p for c in checks)]
    width = max((len(n) for n in names), default=8)
    role_width = max((len(roles[n]) for n in names), default=6)
    # Wide enough for the longest phase name and for the longest state word.
    cell = max([len("LIMITED"), *(len(p) for p in shown)])
    header = f"  {'backend':<{width}}  {'role(s)':<{role_width}}  " + "  ".join(
        f"{p:<{cell}}" for p in shown
    )
    print("── conformance ──")
    print(header)
    by_backend = {n: {c.phase: c for c in checks if c.backend == n} for n in names}
    for name in names:
        cells = []
        for phase in shown:
            c = by_backend[name].get(phase)
            cells.append(f"{(c.state if c else '-'):<{cell}}")
        print(f"  {name:<{width}}  {roles[name]:<{role_width}}  " + "  ".join(cells))

    kinds: dict[str, list[str]] = {}
    for phase in shown:
        kinds.setdefault(PHASE_KINDS[phase], []).append(phase)
    print("  " + "; ".join(f"{kind}: {' '.join(ps)}" for kind, ps in kinds.items()))
    print("  a PASS is protocol conformance only; no phase scores answer quality")

    notable = [c for c in checks if c.state != "PASS" or c.caveat]
    if notable:
        print("\n── detail ──")
        for c in notable:
            if c.state != "PASS":
                print(f"  {c.state:<7} {c.backend}/{c.phase}: {redact(c.note)}")
            if c.caveat:
                print(f"  {'NOTE':<7} {c.backend}/{c.phase}: {redact(c.caveat)}")

    spent = {n: u for n, u in usages.items() if u.rows}
    if spent:
        print("\n── usage ── (what the probe cost; never a score)")
        for name, usage in spent.items():
            acc = usage.accounting()
            print(f"  {name:<{width}}  {len(usage.rows):>2} call(s)  "
                  f"in={acc['prompt_tokens']} out={acc['completion_tokens']}  "
                  f"unreported={acc['unreported_calls']}  "
                  f"unaccounted_attempts={acc['unaccounted_attempts']}")

    passes = [c for c in checks if c.state == "PASS"]
    failures = [c for c in checks if c.failed]
    nokey = [c for c in checks if c.state == "NOKEY"]
    limited = [c for c in checks if c.state == "LIMITED"]
    print(f"\n{len(passes)}/{len(checks)} checks passed"
          + (f", {len(failures)} FAILED" if failures else "")
          + (f", {len(nokey)} unevaluated (missing credentials)" if nokey else "")
          + (f", {len(limited)} unevaluated (provider rate limit)" if limited else "")
          + (f", {sum(c.state == 'UNKNOWN' for c in checks)} unknown" if any(
              c.state == "UNKNOWN" for c in checks) else ""))
    if not failures and not nokey and not limited:
        print("catalog presence is not conformance; only the live phases above are evidence.")

    if args.json_out:
        report: dict[str, Any] = {
            "recorded_at_unix": time.time(),
            "purpose": "protocol conformance per configured role, not benchmark scores",
            "config": args.config,
            "config_sha": cfg.sha,
            "client_user_agent": USER_AGENT,
            "controller_prompt": prompt_id,
            "prompt_revisions": revisions(
                controller=args.controller_prompt, branch=True, reducer=True
            ),
            "phases_requested": asked or "role-derived",
            "phase_kinds": {p: PHASE_KINDS[p] for p in PHASES},
            "output_token_override": None,
            "conversation_id_sent_to": told,
            "probe_sessions": sessions,
            "catalog_listing_available": {p: listings.get(p) is not None for p in providers},
            "checks": [
                {"backend": c.backend, "model": c.model, "phase": c.phase,
                 "kind": PHASE_KINDS[c.phase], "state": c.state, "note": redact(c.note),
                 "caveat": redact(c.caveat), "latency_ms": c.latency_ms,
                 "roles": roles[c.backend]}
                for c in checks
            ],
            # Per-call accounting from the same ledger the runtime uses, so the
            # report says what the probe spent and what the providers failed to
            # report, phase by phase.
            "usage": {
                name: {"accounting": usage.accounting(), "calls": usage.rows}
                for name, usage in usages.items()
            },
        }
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Redacted after serialization as well as per field: one pass over the
        # finished document is what guarantees no expanded ${VAR} survives
        # anywhere in it, including inside a provider's own usage object.
        path.write_text(redact(json.dumps(report, indent=2, sort_keys=False)) + "\n")
        print(f"report written to {path}")

    return 1 if (failures or nokey or limited) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
