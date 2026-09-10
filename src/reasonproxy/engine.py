"""Branch-and-collapse deliberation loop.

    controller --__reasonproxy_reason()--> K same-agent continuations
              --reduce--> checkpoint --> controller resumes --> one real action

Semantically stateless: every response is a function of the incoming request,
the immutable config, and fresh upstream calls. Deliberation survives across
turns only because a *validated* checkpoint is emitted as ordinary assistant
content that the caller replays.

Two invariants drive most of the code below.

Nothing is invented. A synthetic tool result says exactly what happened, which
is usually "nothing was executed"; on the last private call a request honours
it also says that the capability is gone, so the controller never plans around
one it no longer has. Raw provider scratchpad (`reasoning_content`) is never
promoted to content, never persisted, never reduced, and never shown.

Nothing is silently repaired. The controller violating the exclusive-invocation
protocol, a reducer returning something unparseable, or a branch quorum failing
are all observable events: they abort with a typed error or degrade the request
explicitly and say so in the trace. A benchmark that quietly patches its own
protocol violations measures the patch, not the model.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .config import Config, VirtualModel
from .prompts import (
    CHECKPOINT_LISTS,
    CHECKPOINT_REQUIRED,
    CHECKPOINT_SECTIONS,
    CONTROLLER_PROMPTS,
    FAILED_DELIBERATION_RESULT,
    NOOP_REASON_RESULT,
    FOCUS_MAX_CHARS,
    NOT_EXECUTED_RESULT,
    REASON_TOOL,
    REASON_WIRE_NAME,
    REASON_WITHDRAWN_NOTICE,
    wire_for,
    REDUCER_CHECKPOINT_V2,
    TAG,
    TAG_VERSION,
    branch_shim,
    focus_suffix,
    revisions,
)
from .upstream import Completion, Upstream, Usage

Msg = dict[str, Any]

# Request keys the proxy owns because it rewrites or multiplexes them. Every
# other caller key — response_format, sampling, logprobs, user, metadata,
# provider extensions — is forwarded to the controller verbatim. `tool_choice`
# is forwarded too, but through the transport's own argument.
PROXY_OWNED = frozenset({"model", "messages", "tools", "stream", "stream_options", "n"})


class UpstreamError(RuntimeError):
    """Terminal failure of one request.

    Carries the full trace so the HTTP layer can persist an accounted record
    instead of a six-field stub, and the status the caller should see: 400 when
    the caller's own request is unusable, 502 when an upstream model is.
    """

    def __init__(
        self,
        stage: str,
        detail: str,
        *,
        trace: dict[str, Any] | None = None,
        status_code: int = 502,
    ) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail
        self.trace = trace
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# reserved-tag trust boundary
# --------------------------------------------------------------------------- #

# Any `<deliberation ...>` or `</deliberation>` delimiter, however malformed.
_TAG_RE = re.compile(rf"</?{TAG}\b[^>]*>", re.IGNORECASE)
# A complete block, used only to count genuine replayed checkpoints.
_BLOCK_RE = re.compile(rf"<{TAG}\b[^>]*>.*?</{TAG}\s*>", re.DOTALL | re.IGNORECASE)


def escape_tags(text: str) -> str:
    """Neutralize reserved-tag delimiters by escaping them.

    A coding agent reads repository files and terminal output, so a fixture or a
    hostile file can contain a lookalike checkpoint. Escaping the delimiters
    leaves nothing for injected text to forge or to close, which a quarantine
    marker with a guessable spelling does not.
    """
    return _TAG_RE.sub(lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"), text)


def _escape_content(content: Any) -> tuple[Any, int]:
    """Escape reserved tags in a string or in the text parts of multipart content."""
    if isinstance(content, str):
        hits = len(_TAG_RE.findall(content))
        return (escape_tags(content) if hits else content), hits
    if isinstance(content, list):
        hits = 0
        parts: list[Any] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                found = len(_TAG_RE.findall(part["text"]))
                if found:
                    hits += found
                    part = {**part, "text": escape_tags(part["text"])}
            parts.append(part)
        return (parts if hits else content), hits
    return content, 0


def _count_blocks(content: Any) -> int:
    if isinstance(content, str):
        return len(_BLOCK_RE.findall(content))
    if isinstance(content, list):
        return sum(
            len(_BLOCK_RE.findall(p["text"]))
            for p in content
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
    return 0


def sanitize_history(messages: list[Msg]) -> tuple[list[Msg], int, int]:
    """Escape reserved tags outside assistant messages; count what is genuine.

    Returns the (possibly rewritten) history, the number of escaped lookalikes,
    and the number of real checkpoints the caller replayed. Messages are copied
    on write: the caller's request objects are never mutated.
    """
    out: list[Msg] = []
    spoofed = 0
    replayed = 0
    for m in messages:
        if m.get("role") == "assistant":
            replayed += _count_blocks(m.get("content"))
            out.append(m)
            continue
        content, hits = _escape_content(m.get("content"))
        if hits:
            spoofed += hits
            m = {**m, "content": content}
        out.append(m)
    return out, spoofed, replayed


def render_checkpoint(body: str, cid: str) -> str:
    return (
        f'<{TAG} rp_version="{TAG_VERSION}" rp_id="{cid}">\n'
        f"{escape_tags(body).strip()}\n"
        f"</{TAG}>"
    )


def strict_output_requested(req: dict[str, Any]) -> bool:
    rf = req.get("response_format")
    return isinstance(rf, dict) and rf.get("type") in {"json_object", "json_schema"}


def choose_persistence(req: dict[str, Any], vm: VirtualModel) -> str:
    """A strict response contract outranks the configured persistence mode.

    Prepending a checkpoint to a JSON answer breaks the caller's parser, which
    costs the whole task; the checkpoint is worth strictly less than that.
    """
    if strict_output_requested(req):
        return "ephemeral"
    return "assistant_tags" if vm.persistence == "auto" else vm.persistence


# --------------------------------------------------------------------------- #
# checkpoint schema
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Checkpoint:
    """Deliberation result with distinct internal and public representations.

    `internal` is what the controller receives as its own tool result. `public`
    is what may be written into the transcript, and is None unless a validated
    checkpoint exists — an unvalidated collapse is never published.
    """

    internal: str
    public: str | None
    reduced: bool
    degraded: bool = False
    note: str = ""


def parse_checkpoint(raw: str) -> tuple[dict[str, Any] | None, str]:
    """Validate a reducer JSON object. Returns (fields, error).

    A schema is used instead of a minimum length because length cannot tell a
    dense checkpoint from a refusal or a half sentence, and instead of stripping
    action syntax because a validated object simply has nowhere to put any.
    """
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"not JSON ({exc.__class__.__name__})"
    if not isinstance(data, dict):
        return None, f"JSON {type(data).__name__}, not an object"

    fields: dict[str, Any] = {}
    for key in CHECKPOINT_REQUIRED:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            return None, f"{key!r} missing or not a non-empty string"
        fields[key] = value.strip()
    for key in CHECKPOINT_LISTS:
        value = data.get(key)
        if value is None:
            value = []
        elif isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return None, f"{key!r} is {type(value).__name__}, not a list"
        fields[key] = [str(v).strip() for v in value if str(v).strip()]
    return fields, ""


def render_body(fields: dict[str, Any]) -> str:
    """Deterministic local rendering. Empty sections are omitted, not padded."""
    out: list[str] = []
    for key, label in CHECKPOINT_SECTIONS:
        value = fields.get(key)
        if isinstance(value, str) and value:
            out.append(f"{label}: {value}")
        elif isinstance(value, list) and value:
            out.append(label + ":\n" + "\n".join(f"- {item}" for item in value))
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# reason-call classification
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class ReasonCall:
    id: str
    synthesized_id: bool = False
    real: list[dict[str, Any]] = field(default_factory=list)
    focus: str = ""


def _name(tc: dict[str, Any]) -> str:
    return (tc.get("function") or {}).get("name") or ""


def _args(tc: dict[str, Any]) -> str:
    return (tc.get("function") or {}).get("arguments") or ""


def _empty_arguments(raw: str) -> bool:
    text = raw.strip()
    if not text:
        return True
    try:
        parsed = json.loads(text)
    except ValueError:
        return False
    return isinstance(parsed, dict) and not parsed


def _focus_argument(
    raw_args: str, wire_args: str, wire_name: str
) -> tuple[str, tuple[str, str] | None]:
    """Validate the private call's arguments for the offered surface.

    v2/v3 surfaces take no arguments: the runtime supplies the trajectory, so
    anything else is a protocol violation. v4 accepts an optional focus
    question (a short string under FOCUS_MAX_CHARS); unknown fields,
    non-string values, and oversized questions are violations, never repaired.
    """
    if wire_args != "focus-optional":
        if not _empty_arguments(raw_args):
            return "", (
                "private_call_arguments",
                f"called {wire_name} with arguments {raw_args[:200]!r}; it takes none and the "
                f"runtime supplies the trajectory",
            )
        return "", None
    text = raw_args.strip()
    if not text:
        return "", None
    try:
        parsed = json.loads(text)
    except ValueError:
        return "", (
            "private_call_arguments",
            f"called {wire_name} with unparseable arguments {raw_args[:200]!r}; pass "
            f'{{"question": "..."}} or nothing',
        )
    if not isinstance(parsed, dict):
        return "", (
            "private_call_arguments",
            f"called {wire_name} with arguments {raw_args[:200]!r}; pass "
            f'{{"question": "..."}} or nothing',
        )
    unknown = [k for k in parsed if k != "question"]
    if unknown:
        return "", (
            "private_call_arguments",
            f"called {wire_name} with unknown argument(s) {unknown!r}; the only "
            f"supported argument is {{\"question\": \"...\"}}",
        )
    question = parsed.get("question", "")
    if not isinstance(question, str):
        return "", (
            "private_call_arguments",
            f"called {wire_name} with a non-string question; pass a short text question or nothing",
        )
    if len(question) > FOCUS_MAX_CHARS:
        return "", (
            "private_call_arguments",
            f"called {wire_name} with a {len(question)}-character question (limit "
            f"{FOCUS_MAX_CHARS}); keep the focus question short",
        )
    return question, None

def classify(
    out: Completion,
    *,
    offered: bool,
    known_ids: set[str],
    wire_name: str = REASON_WIRE_NAME,
    wire_args: str = "none",
) -> tuple[ReasonCall | None, tuple[str, str] | None]:
    """Split the controller's tool calls into the private call and real calls.

    Returns (call, violation). A violation aborts the request: repairing it
    would either execute an action the controller never got to reconsider, or
    hide that the controller ignored the exclusive-invocation rule — and the
    rate of that violation is itself a reported result.

    `wire_name` is the private tool this request offered (`__reasonproxy_reason`
    for v2 prompts, `deliberate` for v3/v4); interception belongs to the offered
    capability, never to a bare name. `wire_args` is "none" except for v4
    surfaces, which accept an optional focus question.
    """
    private = [tc for tc in out.tool_calls if _name(tc) == wire_name]
    if not private:
        return None, None
    if not offered:
        return None, (
            "private_call_not_offered",
            f"called {wire_name} when the tool was not offered "
            f"(deliberation budget spent or suppressed by the caller's tool_choice)",
        )
    if len(private) > 1:
        return None, (
            "duplicate_private_calls",
            f"emitted {len(private)} {wire_name} calls in one turn; it must be called once, alone",
        )

    tc = private[0]
    raw_args = _args(tc)
    focus, violation = _focus_argument(raw_args, wire_args, wire_name)
    if violation is not None:
        return None, violation

    cid = tc.get("id") or ""
    if cid and cid in known_ids:
        return None, (
            "reused_tool_call_id",
            f"reused tool_call id {cid!r}, which already identifies another call in this request",
        )

    real = [t for t in out.tool_calls if _name(t) != wire_name]
    for t in real:
        rid = t.get("id") or ""
        if rid and rid in known_ids:
            return None, (
                "reused_tool_call_id",
                f"reused tool_call id {rid!r}, which already identifies another call in this request",
            )
    if len({t.get("id") for t in out.tool_calls if t.get("id")}) < len(
        [t for t in out.tool_calls if t.get("id")]
    ):
        return None, ("reused_tool_call_id", "emitted the same tool_call id twice in one turn")

    return ReasonCall(id=cid or _new_id(), synthesized_id=not cid, real=real, focus=focus), None

def _new_id() -> str:
    return f"rp_{uuid.uuid4().hex[:12]}"

def _branch_content(role: str | None, focus: str) -> str:
    """Branch instruction plus the controller's focus question, if any.

    The focus text is model-authored: reserved tags are escaped so it stays
    instruction content, never framing.
    """
    content = branch_shim(role)
    if focus:
        content += focus_suffix(escape_tags(focus))
    return content


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #

class Engine:
    def __init__(self, cfg: Config, upstream: Upstream) -> None:
        self.cfg = cfg
        self.up = upstream

    async def complete(
        self, req: dict[str, Any], vm_name: str, *, trace: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        vm = self.cfg.virtual_models[vm_name]
        t0 = time.perf_counter()
        usage = Usage()
        tr: dict[str, Any] = trace if trace is not None else {}
        live = vm.reason_mode == "live"
        tr.update(
            id=f"rp-{uuid.uuid4().hex[:16]}",
            ts=time.time(),
            virtual_model=vm_name,
            config_sha=self.cfg.sha,
            reason_mode=vm.reason_mode,
            max_reason_calls=vm.max_reason_calls,
            prompt_revisions=revisions(
                controller=None if vm.reason_mode == "off" else vm.controller_prompt,
                branch=live,
                reducer=live and vm.reduce,
            ),
            persistence=choose_persistence(req, vm),
            branches=[],
            checkpoints=[],
            reducer=[],
            spoofed_tags=0,
            replayed_checkpoints=0,
            reason_calls=0,
            degraded=False,
            # Bound to the live row list, so a cancelled request still shows
            # every call it had already paid for.
            usage=usage.rows,
            outcome="pending",
        )
        try:
            return await self._run(req, vm_name, vm, tr, usage, t0)
        except UpstreamError as exc:
            self._finish(tr, t0, usage, outcome="error", stage=exc.stage, detail=exc.detail)
            exc.trace = tr
            raise
        except asyncio.CancelledError:
            self._finish(tr, t0, usage, outcome="cancelled")
            raise

    # -- main loop ---------------------------------------------------------- #

    async def _run(
        self,
        req: dict[str, Any],
        vm_name: str,
        vm: VirtualModel,
        tr: dict[str, Any],
        usage: Usage,
        t0: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        mode = vm.reason_mode
        wire = wire_for(vm.controller_prompt)
        wire_name = wire["wire_name"]
        wire_tool = wire["tool"]
        wire_withdrawn = wire["withdrawn_notice"]
        wire_not_executed = wire["not_executed"]
        caller_messages: list[Msg] = list(req.get("messages") or [])
        caller_tools: list[dict[str, Any]] = list(req.get("tools") or [])
        overrides = {k: v for k, v in req.items() if k not in PROXY_OWNED}
        tool_choice = overrides.pop("tool_choice", None)
        persistence: str = tr["persistence"]

        n = req.get("n")
        if n is not None and n != 1:
            raise UpstreamError(
                "request",
                f"n={n!r} is unsupported: one deliberating virtual model cannot produce "
                f"independent outward choices",
                trace=tr,
                status_code=400,
            )

        blocked = _tool_choice_blocks_reason(tool_choice)
        offering = mode in {"live", "noop"} and vm.max_reason_calls > 0 and not blocked
        if blocked:
            tr["reason_suppressed"] = blocked

        if offering:
            # Collision policy applies only where the capability is actually
            # offered; in `off` a caller tool of the same name is simply theirs.
            collision = [t for t in caller_tools if _tool_name(t) == wire_name]
            if collision:
                raise UpstreamError(
                    "request",
                    f"the caller's tool list already defines {wire_name!r}, the reserved "
                    f"private tool name; ReasonProxy will not shadow or overwrite it",
                    trace=tr,
                    status_code=400,
                )

        if mode == "off":
            # Control arm: no injection, no interception, no sanitization. The
            # caller's messages reach the controller byte-identical.
            out = await self._controller(
                vm, caller_messages, caller_tools or None, tool_choice, overrides, usage, tr
            )
            response = self._compose(vm_name, out, None, None, persistence, usage, tr)
            self._finish(tr, t0, usage, outcome="ok", finish_reason=out.finish_reason)
            return response, tr

        history, spoofed, replayed = sanitize_history(caller_messages)
        tr["spoofed_tags"] = spoofed
        tr["replayed_checkpoints"] = replayed
        history = self._inject(history, vm)
        if mode == "live" and wire.get("directive"):
            # Directed deliberation: the measured user instruction, appended
            # transiently per request. The harness never sees it, so the next
            # request's history carries no copy; each turn gets exactly one.
            _, directive_text = wire["directive"]
            history = [*history, {"role": "user", "content": directive_text}]
            tr["directed"] = True
        known_ids = _known_tool_ids(caller_messages)

        checkpoint: Checkpoint | None = None
        checkpoint_id: str | None = None
        deliberation_off = False

        for depth in range(vm.max_reason_calls + 1):
            offered = offering and depth < vm.max_reason_calls and not deliberation_off
            tools = [*caller_tools, wire_tool] if offered else (caller_tools or None)
            tr["reason_calls"] = depth
            out = await self._controller(vm, history, tools, tool_choice, overrides, usage, tr)

            # Interception belongs to the capability, not to the name. When the
            # tool was never injected for this request, a caller tool that
            # happens to share the reserved name is the caller's own.
            call, violation = (
                classify(out, offered=offered, known_ids=known_ids, wire_name=wire_name, wire_args=wire["args"])
                if offering
                else (None, None)
            )
            if violation:
                kind, detail = violation
                tr.setdefault("protocol_violations", []).append(
                    {"depth": depth, "kind": kind, "detail": detail}
                )
                raise UpstreamError(
                    "controller", f"{out.model} {detail}", trace=tr, status_code=502
                )

            if call is None:
                response = self._compose(
                    vm_name, out, checkpoint, checkpoint_id, persistence, usage, tr
                )
                self._finish(tr, t0, usage, outcome="ok", finish_reason=out.finish_reason)
                return response, tr

            known_ids.add(call.id)
            assistant = _reason_assistant_message(out, call, wire_name=wire_name)
            callsite = [*history, assistant]
            if call.synthesized_id:
                tr.setdefault("protocol_violations", []).append(
                    {
                        "depth": depth,
                        "kind": "missing_tool_call_id",
                        "detail": f"provider returned no id for the {wire_name} call; "
                        f"{call.id} was assigned so the turn stays replayable",
                    }
                )
            if call.real:
                tr.setdefault("protocol_violations", []).append(
                    {
                        "depth": depth,
                        "kind": "mixed_calls",
                        "detail": "emitted alongside the private call, so it was not executed: "
                        + ", ".join(_name(t) for t in call.real),
                    }
                )

            # Whether this is the last private call the request will honour.
            # The controller is told inside this same tool result, so it plans
            # its next action already knowing the capability is gone.
            withdrawn = depth + 1 >= vm.max_reason_calls

            if mode == "noop":
                tr["checkpoints"].append({"depth": depth, "noop": True})
                history = [
                    *callsite,
                    *_tool_results(
                        assistant, call.id, _private_result(NOOP_REASON_RESULT, withdrawn, wire_withdrawn),
                        wire_not_executed,
                    ),
                ]
                continue

            branches, failure = await self._fanout(
                callsite, assistant, call, caller_tools, vm, tr, usage, wire_tool, wire_name, call.focus
            )
            if failure:
                deliberation_off = True
                tr["degraded"] = True
                tr.setdefault("degradations", []).append({"depth": depth, "reason": failure})
                history = [
                    *callsite,
                    *_tool_results(
                        assistant, call.id, _private_result(FAILED_DELIBERATION_RESULT, True, wire_withdrawn),
                        wire_not_executed,
                    ),
                ]
                continue

            checkpoint = await self._collapse(
                callsite, branches, checkpoint, caller_tools, vm, tr, usage, wire_name
            )
            checkpoint_id = f"d-{uuid.uuid4().hex[:12]}"
            tr["checkpoints"].append(
                {
                    "id": checkpoint_id,
                    "depth": depth,
                    "reduced": checkpoint.reduced,
                    "degraded": checkpoint.degraded,
                    "note": checkpoint.note,
                    "internal_chars": len(checkpoint.internal),
                    "public_chars": len(checkpoint.public or ""),
                    "focus_chars": len(call.focus or ""),
                }
            )
            if self.cfg.trace_texts and checkpoint.public:
                tr["checkpoints"][-1]["text"] = checkpoint.public
            if checkpoint.degraded:
                tr["degraded"] = True
            history = [
                *callsite,
                    *_tool_results(
                        assistant, call.id, _private_result(checkpoint.internal, withdrawn, wire_withdrawn),
                        wire_not_executed,
                    ),
            ]

        raise AssertionError("bounded loop must return: the last iteration offers no reason tool")

    # -- controller --------------------------------------------------------- #

    async def _controller(
        self,
        vm: VirtualModel,
        messages: list[Msg],
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
        overrides: dict[str, Any],
        usage: Usage,
        tr: dict[str, Any],
    ) -> Completion:
        out = await self.up.complete(
            vm.controller,
            messages,
            tools=tools,
            tool_choice=tool_choice,
            overrides=overrides or None,
        )
        usage.add("controller", out)
        if not out.ok:
            raise UpstreamError("controller", f"{out.model}: {out.error}", trace=tr)
        if _no_output(out):
            # Thinking-by-default controllers can spend the whole budget in
            # reasoning_content and return nothing actionable. An empty
            # assistant message surfaces as a harness format error many turns
            # later; name the real cause here instead.
            raise UpstreamError(
                "controller",
                f"{out.model} produced no content, refusal, or tool call "
                f"(finish={out.finish_reason}, reasoning={len(out.reasoning)} chars). Raise the "
                f"output budget or pick a controller that does not overthink.",
                trace=tr,
            )
        return out

    def _inject(self, messages: list[Msg], vm: VirtualModel) -> list[Msg]:
        """Insert runtime policy after caller system/developer messages.

        Caller instructions stay authoritative and are never rewritten.
        """
        _, text = CONTROLLER_PROMPTS[vm.controller_prompt]
        i = 0
        while i < len(messages) and messages[i].get("role") in {"system", "developer"}:
            i += 1
        return [*messages[:i], {"role": "system", "content": text}, *messages[i:]]

    async def _fanout(
        self,
        callsite: list[Msg],
        assistant: Msg,
        call: ReasonCall,
        caller_tools: list[dict[str, Any]],
        vm: VirtualModel,
        tr: dict[str, Any],
        usage: Usage,
        wire_tool: dict[str, Any] = REASON_TOOL,
        wire_name: str = REASON_WIRE_NAME,
        focus: str = "",
    ) -> tuple[list[Completion], str]:
        """Run every branch to completion, then decide quorum.

        Branches inherit the entire callsite. The full tool inventory —
        including the private tool the callsite already used — is declared so
        the replayed history stays legal on strict providers, while
        `tool_choice="none"` is what actually stops a branch from acting.

        Each branch banks its own usage and trace row the instant it settles,
        including on its named timeout and failure paths. Accounting after the
        whole fan-out meant a sibling still hanging when the caller gave up
        erased calls this request had already paid for. A branch cancelled
        before it settled has no row at all: it was real provider work whose
        cost is unknown, and a zero row would be an invented bill.
        """
        roles: list[str | None] = (
            list(vm.branch_roles) if vm.branch_prompt == "roles" else [None] * len(vm.branches)
        )
        tools = [*caller_tools, wire_tool]
        banked = [False] * len(vm.branches)
        # One slot per branch, so what reaches the reducer is ordered by branch
        # index and never by which branch happened to answer first.
        accepted: list[Completion | None] = [None] * len(vm.branches)

        def bank(index: int, item: Completion) -> None:
            """Account one settled branch exactly once: usage, then trace."""
            banked[index] = True
            usage.add("branch", item)
            rejected = _branch_rejection(item)
            row: dict[str, Any] = {
                "index": index,
                "backend": item.backend,
                "model": item.model,
                "status": item.status,
                "finish_reason": item.finish_reason,
                "latency_ms": item.latency_ms,
                "attempts": item.attempts,
                "content_chars": len(item.content or ""),
                # Length only. Raw scratchpad is never written to the trace.
                "reasoning_chars": len(item.reasoning or ""),
                "truncated": item.truncated,
                "usable": not rejected,
            }
            if roles[index]:
                row["role"] = roles[index]
            if item.error:
                row["error"] = item.error
            if rejected:
                row["rejected"] = rejected
            elif self.cfg.trace_texts:
                row["text"] = item.content
            tr["branches"].append(row)
            if not rejected:
                accepted[index] = item

        async def one(index: int, backend: str) -> None:
            messages = [
                *callsite,
                *_tool_results(assistant, call.id, _branch_content(roles[index], focus)),
            ]
            started = time.perf_counter()
            try:
                item = await asyncio.wait_for(
                    self.up.complete(
                        backend,
                        messages,
                        tools=tools,
                        tool_choice="none",
                    ),
                    timeout=vm.branch_timeout_s,
                )
            except asyncio.TimeoutError:
                item = Completion.failed(
                    backend,
                    self._model_of(backend),
                    "timeout",
                    f"branch deadline {vm.branch_timeout_s}s",
                    int((time.perf_counter() - started) * 1000),
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - one branch must not orphan its siblings
                item = Completion.failed(
                    backend,
                    self._model_of(backend),
                    "error",
                    f"{type(exc).__name__}: {exc}",
                    int((time.perf_counter() - started) * 1000),
                )
            # Nothing is awaited between the call settling and this line, so
            # no cancellation can land in between and lose the call.
            bank(index, item)

        first_row = len(tr["branches"])
        # return_exceptions keeps a raising sibling from cancelling the rest
        # mid-flight, which would leave orphaned upstream calls billed to
        # nobody. Every branch is awaited and accounted.
        results = await asyncio.gather(
            *(one(i, b) for i, b in enumerate(vm.branches)), return_exceptions=True
        )
        for index, error in enumerate(results):
            if banked[index] or not isinstance(error, BaseException):
                continue
            # This one branch was cancelled, so it never reached `bank`. The
            # attempt happened and its usage is unknown, which is exactly what
            # a row built from no usage object records.
            backend = vm.branches[index]
            bank(
                index,
                Completion.failed(
                    backend, self._model_of(backend), "error", f"{type(error).__name__}: {error}", 0
                ),
            )
        # Rows land in settle order; the trace still reads by branch index.
        tr["branches"][first_row:] = sorted(tr["branches"][first_row:], key=lambda r: r["index"])

        good = [c for c in accepted if c is not None]
        if len(good) < vm.min_branches:
            return [], (
                f"{len(good)}/{len(vm.branches)} usable continuations, need {vm.min_branches}"
            )
        return good, ""

    def _model_of(self, backend: str) -> str:
        return self.cfg.backends[backend].model

    # -- collapse ----------------------------------------------------------- #

    async def _collapse(
        self,
        callsite: list[Msg],
        branches: list[Completion],
        previous: Checkpoint | None,
        caller_tools: list[dict[str, Any]],
        vm: VirtualModel,
        tr: dict[str, Any],
        usage: Usage,
        wire_name: str = REASON_WIRE_NAME,
    ) -> Checkpoint:
        # Anonymized and order-randomized so the reducer cannot prefer a family
        # it recognizes. The permutation is traced, so a run stays auditable.
        order = list(range(len(branches)))
        random.shuffle(order)
        tr.setdefault("reducer_branch_order", []).append(
            [branches[i].backend for i in order]
        )
        blocks = [
            f"<continuation_{n + 1}>\n{_inert(_branch_text(branches[i]))}\n</continuation_{n + 1}>"
            for n, i in enumerate(order)
        ]
        concat = "\n\n".join(blocks)
        missing = len(vm.branches) - len(branches)

        if not vm.reduce:
            # Concatenation ablation. The condition is the raw continuations,
            # but they are completed public content: a truncated or
            # scratchpad-only branch never reached this list.
            return Checkpoint(
                internal=escape_tags(concat),
                public=concat,
                reduced=False,
                note="reduce disabled (concatenation ablation)",
            )

        data = _reducer_data(callsite, caller_tools, blocks, previous, missing, wire_name)
        out = await self.up.complete(
            vm.reducer,
            [
                {"role": "system", "content": REDUCER_CHECKPOINT_V2},
                {"role": "user", "content": data},
            ],
            overrides={"response_format": {"type": "json_object"}},
        )
        usage.add("reducer", out)

        fields: dict[str, Any] | None = None
        if not out.ok:
            note = f"reducer {out.status}: {out.error}"
        elif out.truncated:
            note = f"reducer output truncated (finish_reason={out.finish_reason})"
        elif not (out.content or "").strip():
            note = (
                f"reducer returned no content (finish={out.finish_reason}, "
                f"{len(out.reasoning)} reasoning chars)"
            )
        else:
            fields, error = parse_checkpoint(out.content)
            note = "" if fields else f"reducer JSON invalid: {error}"

        row = {
            "backend": out.backend,
            "model": out.model,
            "status": out.status,
            "finish_reason": out.finish_reason,
            "truncated": out.truncated,
            "attempts": out.attempts,
            "latency_ms": out.latency_ms,
            "content_chars": len(out.content or ""),
            "reasoning_chars": len(out.reasoning or ""),
            "accepted": fields is not None,
            "note": note,
        }
        if out.error:
            row["error"] = out.error
        tr["reducer"].append(row)

        if fields is not None:
            body = render_body(fields)
            return Checkpoint(
                internal=escape_tags(body), public=body, reduced=True
            )

        # Collapse failed. Completed reducer prose is still useful to the
        # controller even when it did not validate, and the continuations
        # themselves are the honest fallback; neither is fit to publish.
        candidate = (out.content or "").strip() if (out.ok and not out.truncated) else ""
        return Checkpoint(
            internal=escape_tags(candidate or concat),
            public=None,
            reduced=False,
            degraded=True,
            note=note,
        )

    # -- outward composition ------------------------------------------------ #

    def _compose(
        self,
        vm_name: str,
        out: Completion,
        checkpoint: Checkpoint | None,
        checkpoint_id: str | None,
        persistence: str,
        usage: Usage,
        tr: dict[str, Any],
    ) -> dict[str, Any]:
        message = out.as_assistant_message()
        message.setdefault("role", "assistant")

        if checkpoint and checkpoint.public:
            if persistence != "assistant_tags":
                tr["persistence_skipped"] = persistence
            elif message.get("refusal"):
                # Attaching cognition to a refusal would change what the
                # provider said it was doing.
                tr["persistence_skipped"] = "refusal"
            else:
                block = render_checkpoint(checkpoint.public, checkpoint_id or "d-0")
                message["content"] = _prepend_text(message.get("content"), block + "\n\n")

        choice: dict[str, Any] = {
            "index": 0,
            "message": message,
            "finish_reason": out.finish_reason,
        }
        raw = out.raw_response if isinstance(out.raw_response, dict) else {}
        raw_choices = raw.get("choices")
        raw_choice = raw_choices[0] if isinstance(raw_choices, list) and raw_choices else {}
        if isinstance(raw_choice, dict):
            for key in ("logprobs", "stop_reason", "native_finish_reason", "matched_stop"):
                if raw_choice.get(key) is not None:
                    choice[key] = raw_choice[key]

        composite = usage.totals()
        response: dict[str, Any] = {
            "id": f"chatcmpl-rp-{uuid.uuid4().hex[:16]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": vm_name,
            "choices": [choice],
            # Composite across every internal call. Reporting only the
            # controller would understate what the condition actually spent.
            "usage": composite,
            "reasonproxy_usage": {
                "components": usage.rows,
                "controller": usage.accounting(roles={"controller"}),
                "composite": usage.accounting(),
            },
        }
        for key in ("system_fingerprint", "service_tier"):
            if raw.get(key) is not None:
                response[key] = raw[key]
        return response

    # -- trace -------------------------------------------------------------- #

    def _finish(
        self, tr: dict[str, Any], t0: float, usage: Usage, **terminal: Any
    ) -> None:
        tr["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        tr["usage_totals"] = {
            "composite": usage.accounting(),
            "controller": usage.accounting(roles={"controller"}),
        }
        tr.update(terminal)


# --------------------------------------------------------------------------- #
# message construction
# --------------------------------------------------------------------------- #

def _reason_assistant_message(
    out: Completion, call: ReasonCall, wire_name: str = REASON_WIRE_NAME
) -> Msg:
    """The exact call site: the assistant turn that requested deliberation.

    The provider's own message is replayed losslessly — content, real tool
    calls, ids, arguments, and any provider metadata all survive — because the
    branches must inherit what the controller actually did, not a summary of it.
    The only edit is assigning an id when the provider omitted one, without
    which the turn cannot be answered at all.
    """
    message = out.as_assistant_message()
    message.setdefault("role", "assistant")
    calls = message.get("tool_calls") or ()
    if all(tc.get("id") for tc in calls):
        return message
    patched = []
    for tc in calls:
        if not tc.get("id"):
            tc = {**tc, "id": call.id if _name(tc) == wire_name else _new_id()}
        patched.append(tc)
    message["tool_calls"] = patched
    return message


def _tool_results(
    assistant: Msg,
    private_id: str,
    private_content: str,
    not_executed: str = NOT_EXECUTED_RESULT,
) -> list[Msg]:
    """Answer every tool call in the assistant turn, in order.

    Providers reject an assistant turn whose tool calls are unanswered, and a
    real call emitted beside the private one must be answered truthfully — it
    was not executed, so there is no observation to invent.
    """
    return [
        {
            "role": "tool",
            "tool_call_id": tc.get("id"),
            "content": private_content if tc.get("id") == private_id else not_executed,
        }
        for tc in assistant.get("tool_calls") or ()
    ]


def _private_result(body: str, withdrawn: bool, notice: str = REASON_WITHDRAWN_NOTICE) -> str:
    """The private tool result, plus the withdrawal notice when it applies.

    Only this string carries the notice. `Checkpoint.public` must not: the
    caller replays published cognition into later requests, where deliberation
    is available again, and a checkpoint claiming otherwise would be false the
    moment it is reused.
    """
    return f"{body}\n\n{notice}" if withdrawn else body


def _prepend_text(content: Any, prefix: str) -> Any:
    if content is None:
        return prefix.rstrip()
    if isinstance(content, str):
        return prefix + content
    if isinstance(content, list):
        return [{"type": "text", "text": prefix.rstrip()}, *content]
    return content


def _known_tool_ids(messages: list[Msg]) -> set[str]:
    ids: set[str] = set()
    for m in messages:
        for tc in m.get("tool_calls") or ():
            if tc.get("id"):
                ids.add(tc["id"])
        if m.get("tool_call_id"):
            ids.add(m["tool_call_id"])
    return ids


def _tool_name(tool: dict[str, Any]) -> str:
    return (tool.get("function") or {}).get("name") or tool.get("name") or ""


def _tool_choice_blocks_reason(tool_choice: Any) -> str:
    """The caller's tool decision is authoritative.

    `none` forbids tools and a named function forces one; offering a private
    tool in either case would silently overrule the caller.
    """
    if isinstance(tool_choice, str) and tool_choice.lower() == "none":
        return "tool_choice=none"
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        return f"tool_choice={name}" if name else "tool_choice=forced"
    return ""


def _no_output(out: Completion) -> bool:
    """True only when the provider really returned nothing usable.

    A refusal, or multipart content that the flat `content` accessor cannot
    represent, is output: it must be preserved, not reported as an error.
    """
    if out.tool_calls or (out.content or "").strip():
        return False
    # Only now is the lossless message worth materializing: a refusal or
    # multipart content is real output that the flat accessor cannot show.
    message = out.as_assistant_message()
    if message.get("refusal"):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        return not content
    return content is None


def _branch_rejection(c: Completion) -> str:
    """Why this continuation cannot be deliberation, or "" if it can."""
    if not c.ok:
        return c.status
    if c.truncated:
        return f"truncated (finish_reason={c.finish_reason})"
    if not (c.content or "").strip():
        if c.reasoning:
            # A hidden scratchpad is not a continuation and is never promoted.
            return f"no content, {len(c.reasoning)} reasoning chars only"
        if c.tool_calls:
            return "tool calls only, no continuation text"
        return "empty content"
    return ""


def _branch_text(c: Completion) -> str:
    """Completed content, with any proposed call normalized to inert data."""
    text = (c.content or "").strip()
    if c.tool_calls:
        proposed = "; ".join(f"{_name(tc)}({_args(tc)})" for tc in c.tool_calls)
        text = f"{text}\n[proposed action, not executed: {proposed}]"
    return text


# --------------------------------------------------------------------------- #
# reducer payload
# --------------------------------------------------------------------------- #

# Framing tags used inside the reducer's single data message. Any lookalike in
# model or repository text is escaped so it cannot close our framing.
_DATA_TAGS = ("trajectory", "tool_inventory", "earlier_checkpoint", "continuations", "message")
_DATA_TAG_RE = re.compile(r"</?(?:" + "|".join(_DATA_TAGS) + r"|continuation_\d+)\b[^>]*>", re.I)


def _inert(text: str) -> str:
    return _DATA_TAG_RE.sub(lambda m: m.group(0).replace("<", "&lt;"), text)


def _flatten(content: Any) -> str:
    """Render message content as text without dropping parts we cannot inline."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    out.append(part["text"])
                else:
                    out.append(json.dumps(part, sort_keys=True, default=str))
            else:
                out.append(str(part))
        return "\n".join(out)
    return str(content)


def _render_transcript(messages: list[Msg], wire_name: str = REASON_WIRE_NAME) -> str:
    """The complete call site as data, never as competing live messages.

    Serializing rather than replaying is what makes the reducer payload legal
    on every provider: there is no dangling tool call, no orphaned tool result,
    and no second system prompt telling the reducer it is the acting agent.
    Nothing is windowed out, so the original task statement survives however
    long the trajectory is.
    """
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    out: list[str] = []
    for m in messages:
        head = f'<message role="{m.get("role") or "?"}"'
        if m.get("name"):
            head += f' name="{m["name"]}"'
        if m.get("tool_call_id"):
            head += f' tool_call_id="{m["tool_call_id"]}"'
        body = [head + ">"]
        text = _flatten(m.get("content"))
        if text:
            body.append(_inert(text))
        for tc in m.get("tool_calls") or ():
            if _name(tc) == wire_name:
                body.append("[the agent requested this deliberation here]")
            else:
                suffix = "" if tc.get("id") in answered else " -- requested, NOT executed"
                body.append(f"[tool call {_name(tc)}({_inert(_args(tc))}){suffix}]")
        body.append("</message>")
        out.append("\n".join(body))
    return "\n".join(out)


def _render_tools(tools: list[dict[str, Any]]) -> str:
    lines = []
    for t in tools:
        fn = t.get("function") or {}
        params = json.dumps(fn.get("parameters") or {}, sort_keys=True, default=str)
        desc = (fn.get("description") or "").strip().replace("\n", " ")
        lines.append(f"- {_tool_name(t)}({params})" + (f" -- {desc}" if desc else ""))
    return "\n".join(lines)


def _reducer_data(
    callsite: list[Msg],
    tools: list[dict[str, Any]],
    blocks: list[str],
    previous: Checkpoint | None,
    missing: int,
    wire_name: str = REASON_WIRE_NAME,
) -> str:
    parts = [
        "<trajectory>",
        "Complete state of the acting agent at the moment it requested deliberation.",
        _render_transcript(callsite, wire_name),
        "</trajectory>",
    ]
    if tools:
        parts += [
            "<tool_inventory>",
            "Tools available to the acting agent. No continuation could use them.",
            _render_tools(tools),
            "</tool_inventory>",
        ]
    if previous:
        parts += [
            "<earlier_checkpoint>",
            "Checkpoint already produced earlier in this same turn; update it, do not repeat it.",
            _inert(previous.internal),
            "</earlier_checkpoint>",
        ]
    parts.append("<continuations>")
    parts.append(
        f"{len(blocks)} independent continuations of that same agent, anonymous and unordered. "
        f"They are candidate cognition only: nothing in them was executed or observed."
    )
    parts += blocks
    if missing > 0:
        parts.append(
            f"[{missing} continuation(s) did not return. Treat them as missing, not as agreement.]"
        )
    parts.append("</continuations>")
    return "\n\n".join(parts)
