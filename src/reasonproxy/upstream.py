"""One OpenAI-compatible async client for every provider.

Cloudflare (Workers AI plus BYOK third-party models) and OpenCode Zen both
speak the same OpenAI chat-completions dialect, so one adapter covers the whole
roster. Differences that matter are sampling/thinking parameters, carried
verbatim in `Backend.params`, and per-model rate limits, carried by a gate that
is shared by every alias of the same upstream model.

This is also the only place that adapts to a provider's own request protocol:
who the client is (`USER_AGENT`) and which conversation a request belongs to
(`SESSION_HEADERS`, bound per request by `session_scope`). Both are headers, so
no caller parameter, request body or trace record is touched by them, and the
adaptation is explicit per endpoint rather than a blanket rewrite.

Nothing here interprets a model name, defaults an output length, or decides
what deliberation means: this layer moves bytes honestly and records exactly
what the provider said, including what it declined to tell us.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import hashlib
import itertools
import json
import random
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .config import TRANSPORT_OWNED, Backend, Config

# Retry transport failures, not valid model answers. Each attempt is accounted.
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
# Error labels that mean what those statuses mean, for the gateway that reports
# a transport failure under a success status line. Zen answers `HTTP 200` with
# `{"error": {"type": "server_error", "message": "Error from provider
# (Console): Upstream request failed: [504] Upstream idle timeout exceeded"}}`:
# the real status is prose inside the message and `error.code` carries no
# number, so nothing numeric identifies the condition. Exactly the one label
# observed doing this, matched as a structured field -- the message beside it
# is free text, and a valid answer must never be retried for what it says.
RETRY_ERROR_KINDS = frozenset({"server_error"})
BACKOFF_S = 1.5
BACKOFF_CAP_S = 8.0
BACKOFF_JITTER_S = 0.4
_UNSET = object()

QUOTA_MARKERS = (
    "freeusagelimiterror",
    "free usage limit",
    "insufficient_quota",
    "quota exceeded",
    "quota exhausted",
    "credit balance is too low",
)

TRUNCATED_FINISH = frozenset({"length", "max_tokens"})

# Who we are, on every provider request. A coding client is asked to identify
# itself with its own user agent rather than a generic SDK or HTTP-library name
# (https://opencode.ai/docs/go/#where-can-i-use-it). ReasonProxy is a client in
# its own right, so it says exactly that and never borrows another client's
# name; it is a constant rather than config, so no roster file can turn our
# identity into a claim about somebody else.
USER_AGENT = "ReasonProxy/0.1"

# Providers that route by conversation, and the header each one reads. Zen
# refuses a session-less request outright ("cannot be routed efficiently") and
# is the one endpoint that documented the requirement, so the conversation id
# goes to it and is not broadcast to endpoints that never asked. Keyed by the
# parsed URL host, never by a substring of the URL.
SESSION_HEADERS: dict[str, str] = {"opencode.ai": "x-opencode-session"}

# Provider error messages are bounded, not because output should be trimmed,
# but because an error string lands in a shared trace file.
_DETAIL_CHARS = 300
_BEARER = re.compile(r"bearer\s+\S+", re.IGNORECASE)
# A caller's session id has to survive as a header value, and the caller
# controls its bytes: Starlette hands us whatever arrived, latin-1 decoded, so
# a stray byte would otherwise fail inside the HTTP client and lose the turn.
# Printable ASCII, bounded, is what a header value can carry.
_ROUTABLE_SESSION = re.compile(r"[\x20-\x7e]{1,200}\Z")


# One conversation id per in-flight caller request, read by every provider call
# that request makes. A ContextVar is copied into a task as the task is created,
# which is exactly the scope needed here: the controller turn, the K-way branch
# fan-out and the reducer all inherit the same id without threading an argument
# through the engine, and two concurrent requests cannot observe each other's.
_SESSION: contextvars.ContextVar[str] = contextvars.ContextVar(
    "reasonproxy_session", default=""
)


@contextmanager
def session_scope(session: str) -> Iterator[None]:
    """Bind one caller conversation to the provider calls made inside.

    The HTTP handler wraps engine task creation in this: the task inherits the
    binding when it is created, and the reset keeps the binding from outliving
    the request on a worker that goes on to serve a different conversation.
    """
    token = _SESSION.set(session)
    try:
        yield
    finally:
        _SESSION.reset(token)


def current_session() -> str:
    """The conversation bound to this request, or "" outside a request."""
    return _SESSION.get()


def conversation_id(messages: list[dict[str, Any]]) -> str:
    """A stable, opaque id for the conversation these messages belong to.

    Used when the caller declared no session of its own. An agent resends its
    whole growing trajectory every step, so what identifies the conversation is
    the block it opened with: everything before the first assistant turn. A
    digest of that block is identical for every step of one task and differs
    between tasks, unlike a per-request uuid, which would defeat the routing
    and prompt caching the id exists for. Only the digest travels, so no task
    text is put on the wire in a header.
    """
    opening = list(itertools.takewhile(lambda m: m.get("role") != "assistant", messages))
    payload = json.dumps(
        [[m.get("role"), m.get("content")] for m in opening or messages[:1]],
        sort_keys=True,
        default=str,
        ensure_ascii=False,
    )
    return "rp-" + hashlib.sha256(payload.encode()).hexdigest()[:32]


def routing_session(declared: str | None, messages: list[dict[str, Any]]) -> str:
    """The conversation this turn routes under.

    The caller's own id when it sent one that can travel as a header value,
    otherwise a digest of the trajectory's opening. An id that cannot be a
    header value does not fail the request: it is simply not usable for
    routing, so the derived id takes over and the turn still reaches a
    provider that requires one.
    """
    if declared and _ROUTABLE_SESSION.match(declared):
        return declared
    return conversation_id(messages)


@dataclass(slots=True)
class Completion:
    backend: str
    model: str
    content: str
    tool_calls: list[dict[str, Any]]
    finish_reason: str
    usage: dict[str, Any]
    latency_ms: int
    status: str = "ok"  # ok | timeout | error
    error: str | None = None
    attempts: int = 1
    reasoning: str = ""
    # Verbatim provider payloads. `raw_message` is the assistant turn exactly
    # as the provider wrote it (reasoning blocks, refusals, provider extras);
    # `raw_response` is the whole body, so per-choice data such as `logprobs`
    # and the untouched `usage` object survive for analysis.
    raw_message: dict[str, Any] = field(default_factory=dict)
    raw_response: dict[str, Any] = field(default_factory=dict)
    # Provider usage objects from attempts this call threw away. A gateway
    # that answers 200 with no assistant turn in the body has still run (and
    # billed) a generation, so retrying it must not make that spend vanish
    # from the accounting. Verbatim, one entry per discarded attempt.
    discarded_usage: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def truncated(self) -> bool:
        return self.finish_reason in TRUNCATED_FINISH

    @property
    def reasoning_only(self) -> bool:
        """The turn went entirely into hidden reasoning: no usable content.

        Thinking-by-default models can spend a whole output budget in
        `reasoning_content` and return `content: null`. That is a diagnosis to
        report, never a substitute for the answer.
        """
        return not self.content.strip() and bool(self.reasoning)

    @property
    def text(self) -> str:
        """Public assistant content, and only when it is safe to consume.

        Empty unless the call succeeded and the model actually finished, so a
        partial thought is never mistaken for a complete one. Hidden reasoning
        is never promoted into content here or anywhere else: it stays in
        `reasoning` and in the raw payloads.
        """
        return self.content if self.ok and not self.truncated else ""

    def as_assistant_message(self) -> dict[str, Any]:
        """This turn, replayable as an assistant message.

        The provider's own message is preferred and returned deep-copied, so a
        caller can extend it without corrupting this record. Replaying it
        verbatim keeps refusals, reasoning blocks and provider-specific fields
        attached to the turn they belong to; the canonical shape is the
        fallback for completions built without a payload (stubs, `failed`).
        """
        if self.raw_message:
            msg = copy.deepcopy(self.raw_message)
            msg["role"] = "assistant"
            msg.setdefault("content", None)
            return msg
        canonical: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            canonical["tool_calls"] = copy.deepcopy(self.tool_calls)
        return canonical

    @classmethod
    def failed(
        cls, backend: str, model: str, status: str, error: str, ms: int, attempts: int = 1
    ) -> Completion:
        return cls(backend, model, "", [], "error", {}, ms, status, error, attempts)


@dataclass(slots=True)
class Usage:
    """Per-component token accounting. Never collapsed into one number.

    Unknown is recorded as unknown, at two granularities. A row whose provider
    reported only part of its usage (or none of it) is incomplete and counts
    in `unreported_calls`, which rides along with the token sums so no reader
    of them can mistake them for a complete bill. Separately, a completion's
    own usage object describes only the attempt that answered, so retried
    attempts — a 429 before the 200, or every attempt failing — are real
    provider work; that count lives in `accounting()`, not in `totals()`,
    because a client reading the standard usage object would take any field
    there for a billing quantity.

    An attempt the transport threw away is not automatically unaccounted for.
    A gateway that answers 200 with no assistant turn in it still reports what
    the discarded generation cost, and that spend is as real as the answering
    one: it is summed into the row (`retried_*`, and into the token totals)
    and stops counting as an unaccounted attempt. Only attempts that told us
    nothing stay unaccounted.
    """

    rows: list[dict[str, Any]] = field(default_factory=list)

    def add(self, role: str, c: Completion) -> None:
        raw = c.usage if isinstance(c.usage, dict) else {}
        prompt_details = _dict(raw.get("prompt_tokens_details"))
        completion_details = _dict(raw.get("completion_tokens_details"))
        input_tokens = _int(raw.get("prompt_tokens"))
        output_tokens = _int(raw.get("completion_tokens"))
        # Partial usage is not usage: a row missing either side cannot be
        # summed into an honest bill.
        complete = input_tokens is not None and output_tokens is not None
        discarded = [d for d in c.discarded_usage if isinstance(d, dict) and d]
        billed = [
            d
            for d in discarded
            if _int(d.get("prompt_tokens")) is not None
            and _int(d.get("completion_tokens")) is not None
        ]
        self.rows.append(
            {
                "role": role,
                "backend": c.backend,
                "model": c.model,
                "input_tokens": input_tokens,
                "cached_input_tokens": _int(prompt_details.get("cached_tokens")),
                "cache_write_tokens": _int(prompt_details.get("cache_write_tokens")),
                "output_tokens": output_tokens,
                "reasoning_tokens": _int(completion_details.get("reasoning_tokens")),
                "neurons": _num(raw.get("neurons")),  # Workers AI bills in these
                # Spend on attempts that produced no answer, kept apart from
                # the answering attempt's so neither can be mistaken for the
                # other, and summed into the totals so it cannot vanish.
                "retried_input_tokens": sum(_int(d.get("prompt_tokens")) or 0 for d in discarded),
                "retried_output_tokens": sum(
                    _int(d.get("completion_tokens")) or 0 for d in discarded
                ),
                "retried_raw": discarded,  # verbatim, neurons and all
                "latency_ms": c.latency_ms,
                "attempts": c.attempts,
                "usage_complete": complete,
                # Every attempt no usage object here describes.
                "unaccounted_attempts": c.attempts - (1 if complete else 0) - len(billed),
                "status": c.status,
                "finish_reason": c.finish_reason,
                # The provider's own usage object, untouched, as the record of
                # truth for anything this schema does not anticipate.
                "raw": raw,
            }
        )

    def totals(self, roles: set[str] | None = None) -> dict[str, int]:
        """Token sums for the standard usage object.

        Sums cover reported data only -- from the answering attempt and from
        any discarded attempt that reported what it cost -- and
        `unreported_calls` says how much is missing from them, so this can
        never claim to be exhaustive.
        """
        rows = self._rows(roles)
        prompt = sum((r["input_tokens"] or 0) + r["retried_input_tokens"] for r in rows)
        completion = sum((r["output_tokens"] or 0) + r["retried_output_tokens"] for r in rows)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "unreported_calls": sum(1 for r in rows if not r["usage_complete"]),
        }

    def accounting(self, roles: set[str] | None = None) -> dict[str, int]:
        """Full honesty block, for `reasonproxy_usage` and the trace only.

        Never the standard usage object: `unaccounted_attempts` is a count of
        upstream attempts, not tokens, and must not sit where a client parses
        billing fields.
        """
        return {**self.totals(roles), "unaccounted_attempts": self.unaccounted_attempts(roles)}

    def unreported(self, roles: set[str] | None = None) -> int:
        """Component calls whose provider usage was absent or incomplete."""
        return sum(1 for r in self._rows(roles) if not r["usage_complete"])

    def unaccounted_attempts(self, roles: set[str] | None = None) -> int:
        """Upstream attempts no usage object accounts for."""
        return sum(r["unaccounted_attempts"] for r in self._rows(roles))

    def _rows(self, roles: set[str] | None) -> list[dict[str, Any]]:
        return [r for r in self.rows if roles is None or r["role"] in roles]


class RateGate:
    """Bounds in-flight requests and request rate for one upstream model.

    Provider limits are per model, not per account-wide: Workers AI caps
    frontier models at 20 rpm (50 with prepaid AI Gateway credits). A single
    agent never approaches that, but a benchmark running many tasks at once
    does, so the gate is what keeps a K-way fan-out from self-throttling into
    429s.
    """

    __slots__ = ("_lock", "_min_interval", "_next_slot", "_sem", "max_concurrent", "rpm")

    def __init__(self, max_concurrent: int, rpm: int | None) -> None:
        self.max_concurrent = max(1, max_concurrent)
        self.rpm = rpm if rpm and rpm > 0 else None
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._min_interval = 60.0 / self.rpm if self.rpm else 0.0
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> RateGate:
        await self._sem.acquire()
        try:
            if self._min_interval:
                async with self._lock:
                    now = time.monotonic()
                    start = max(now, self._next_slot)
                    self._next_slot = start + self._min_interval
                # A reserved slot is never rewound. Later entrants may already
                # be queued behind it, and moving the clock back would let two
                # requests share one slot; a cancelled entrant just leaves a
                # gap in the schedule.
                if start > now:
                    await asyncio.sleep(start - now)
        except BaseException:
            # Cancelled or failed *during entry*, so `__aexit__` will never
            # run: hand the permit back here or the gate loses capacity for
            # the lifetime of the process.
            self._sem.release()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._sem.release()


class Upstream:
    """Issues chat completions against configured backends."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._secrets: list[str] = []
        # One gate per (provider, actual model). Two backend aliases of the
        # same upstream model share the provider's quota, so they share the
        # gate, taking the most restrictive limits either alias configured.
        limits: dict[tuple[str, str], tuple[int, int | None]] = {}
        for be in cfg.backends.values():
            key = (be.provider, be.model)
            current = limits.get(key)
            if current is None:
                limits[key] = (be.max_concurrent, be.rpm)
                continue
            rpms = [r for r in (current[1], be.rpm) if r]
            limits[key] = (
                min(current[0], be.max_concurrent),
                min(rpms) if rpms else None,
            )
        self._gates = {key: RateGate(c, r) for key, (c, r) in limits.items()}
        # Resolved once per provider, from the parsed URL host rather than a
        # substring of the URL, so only the endpoint that documented the
        # requirement is ever told which conversation a request belongs to.
        self._session_headers = {
            name: SESSION_HEADERS[host]
            for name, p in cfg.providers.items()
            if (host := (httpx.URL(p.base_url).host or "").lower()) in SESSION_HEADERS
        }

    def gate(self, backend_name: str) -> RateGate:
        """The gate governing a backend, shared with every alias of its model."""
        be = self.cfg.backends[backend_name]
        return self._gates[(be.provider, be.model)]

    def _client(self, provider_name: str) -> httpx.AsyncClient:
        if provider_name not in self._clients:
            p = self.cfg.providers[provider_name]
            key = p.api_key()
            self._secrets.append(key)
            self._clients[provider_name] = httpx.AsyncClient(
                base_url=p.base_url.rstrip("/"),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    **p.headers,
                    # Last, so a roster file cannot dress us up as another
                    # client: our identity is not a configurable field.
                    "User-Agent": USER_AGENT,
                },
                timeout=httpx.Timeout(p.timeout_s, connect=15.0),
            )
        return self._clients[provider_name]

    def _headers(self, provider_name: str, messages: list[dict[str, Any]]) -> dict[str, str]:
        """Per-request headers for one provider call.

        Empty for a provider that never asked about conversations. Sent per
        request rather than written onto the shared client, because one client
        carries concurrent requests belonging to different conversations.

        The value is the conversation bound to this request. A call made
        outside a request -- a conformance probe driving this class directly --
        has none, so its own message prefix identifies it by the same rule
        rather than the call going out unroutable.
        """
        header = self._session_headers.get(provider_name)
        if header is None:
            return {}
        return {header: current_session() or conversation_id(messages)}

    def _redact(self, text: str) -> str:
        """Strip credentials that a gateway echoed back into an error string."""
        out = _BEARER.sub("Bearer [redacted]", text)
        for secret in self._secrets:
            if secret in out:
                out = out.replace(secret, "[redacted]")
        return out

    async def aclose(self) -> None:
        for c in self._clients.values():
            await c.aclose()

    async def complete(
        self,
        backend_name: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        overrides: dict[str, Any] | None = None,
    ) -> Completion:
        be: Backend = self.cfg.backends[backend_name]
        body = _body(be, messages, tools, tool_choice, overrides)
        # One conversation for every attempt: a retry is the same turn.
        headers = self._headers(be.provider, messages)

        gate = self.gate(backend_name)
        t0 = time.perf_counter()
        discarded: list[dict[str, Any]] = []
        delay = 0.0
        max_attempts = self.cfg.providers[be.provider].max_attempts
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                await asyncio.sleep(delay)
            retryable = True
            retry_after = 0.0
            try:
                async with gate:
                    r = await self._client(be.provider).post(
                        "/chat/completions", json=body, headers=headers
                    )
            except httpx.TimeoutException as exc:
                result = Completion.failed(
                    backend_name, be.model, "timeout",
                    _bound(self._redact(str(exc))) or "deadline exceeded", _ms(t0), attempt,
                )
            except httpx.HTTPError as exc:
                result = Completion.failed(
                    backend_name, be.model, "error",
                    _bound(f"{type(exc).__name__}: {self._redact(str(exc))}"), _ms(t0), attempt,
                )
            else:
                try:
                    data = r.json()
                except ValueError:
                    data = _UNSET
                raw = data if isinstance(data, dict) else {}
                if r.status_code >= 400 or raw.get("error"):
                    detail = _bound(self._redact(_provider_error(r)))
                    hard_quota = any(marker in detail.lower() for marker in QUOTA_MARKERS)
                    result = Completion.failed(
                        backend_name, be.model, "error", detail, _ms(t0), attempt
                    )
                    retryable = (
                        r.status_code in RETRY_STATUS
                        or (r.status_code < 400 and _transient_envelope(raw))
                    ) and not hard_quota
                else:
                    result = _parse(r, backend_name, be, _ms(t0), attempt, data=data)
                    missing_turn = not isinstance(raw.get("choices"), list) or not raw["choices"]
                    provider_error = (
                        not missing_turn
                        and isinstance(raw["choices"][0], dict)
                        and raw["choices"][0].get("finish_reason") == "error"
                    )
                    retryable = missing_turn or provider_error
                    if result.ok and provider_error:
                        result.status = "error"
                        result.error = "provider returned finish_reason=error"
                result.usage = _dict(raw.get("usage"))
                retry_after = _retry_after(r.headers.get("Retry-After"))
            if not retryable or attempt == max_attempts:
                result.discarded_usage = discarded
                return result
            discarded.append(result.usage)
            delay = max(
                retry_after,
                min(BACKOFF_CAP_S, BACKOFF_S * 2 ** min(attempt - 1, 16))
                + random.random() * BACKOFF_JITTER_S,
            )
        raise AssertionError("positive attempt budget exhausted without a result")


def _retry_after(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        seconds = float(value)
        return seconds if 0 <= seconds < float("inf") else 0.0
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            return max(0.0, (date - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def _transient_envelope(raw: dict[str, Any]) -> bool:
    """True when an error envelope names a transient upstream failure.

    Two forms, both read from structured fields only. A gateway that puts the
    real status in `error.code` is read as that status; one that names the
    condition instead is matched against `RETRY_ERROR_KINDS` using the same
    label `_provider_error` reports, so the decision to retry and the recorded
    failure can never disagree about why.

    Consulted only for a status line that claimed success, because a provider
    that did commit to a status is the authority on its own response: Zen
    labels a 400 `server_error` too, and a rejected request must not be sent
    the full budget of times for wearing a transient label.
    """
    err = _dict(raw.get("error"))
    if _int(err.get("code")) in RETRY_STATUS:
        return True
    return _error_kind(raw).lower() in RETRY_ERROR_KINDS


def _body(
    be: Backend,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: Any,
    overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    """Backend params first, explicit caller overrides second, proxy-owned
    wiring last and unconditional.

    `Backend.params` stays a generic passthrough, so an override that names a
    proxy-owned key (`model`, `messages`, the tool surface, streaming, `n`)
    cannot redirect a call to another model or blank the tool contract. Keys
    explicitly set to null are dropped rather than sent as JSON nulls.
    """
    merged = {**be.params, **(overrides or {})}
    body = {k: v for k, v in merged.items() if v is not None and k not in TRANSPORT_OWNED}
    body["model"] = be.model
    body["messages"] = messages
    if tools:
        body["tools"] = tools
    # An explicit choice is an instruction, including `"none"` (a branch must
    # not act) and including the case where no tools are offered at all.
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


def _parse(
    r: httpx.Response, backend: str, be: Backend, ms: int, attempts: int,
    *, data: Any = _UNSET,
) -> Completion:
    """Turn a 2xx body into a Completion, or a shape complaint into a failure.

    Providers under load return `choices: null`, empty choice lists, and
    string-typed fields where objects belong. Those are reported by shape only:
    an error string must never carry a response body, which can contain echoed
    request fragments or credentials.
    """
    if data is _UNSET:
        try:
            data = r.json()
        except ValueError:
            return Completion.failed(
                backend, be.model, "error",
                f"malformed response: body is not JSON ({len(r.content)} bytes)", ms, attempts,
            )
    if not isinstance(data, dict):
        return _malformed(backend, be, f"top level is {_shape(data)}", ms, attempts)

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return _malformed(backend, be, f"choices is {_shape(choices)}", ms, attempts)
    choice = choices[0]
    if not isinstance(choice, dict):
        return _malformed(backend, be, f"choices[0] is {_shape(choice)}", ms, attempts)
    msg = choice.get("message")
    if not isinstance(msg, dict):
        return _malformed(backend, be, f"choices[0].message is {_shape(msg)}", ms, attempts)

    tool_calls, broken = _tool_calls(msg.get("tool_calls"))
    if broken:
        return _malformed(backend, be, broken, ms, attempts)
    finish = choice.get("finish_reason")
    if not isinstance(finish, str) or not finish:
        finish = "tool_calls" if tool_calls else "stop"
    model = data.get("model")
    return Completion(
        backend=backend,
        model=model if isinstance(model, str) and model else be.model,
        content=_text(msg.get("content")),
        reasoning=_text(msg.get("reasoning_content")) or _text(msg.get("reasoning")),
        tool_calls=tool_calls,
        finish_reason=finish,
        usage=_dict(data.get("usage")),
        latency_ms=ms,
        attempts=attempts,
        raw_message=msg,
        raw_response=data,
    )


def _malformed(backend: str, be: Backend, why: str, ms: int, attempts: int) -> Completion:
    return Completion.failed(backend, be.model, "error", f"malformed response: {why}", ms, attempts)


def _tool_calls(value: Any) -> tuple[list[dict[str, Any]], str]:
    """Validated tool calls, or the reason the tool surface is unusable.

    A provider that emits a broken call has produced invalid output, not
    output to be quietly trimmed: dropping the bad entries would either hide
    an action the model believes it requested, or hand the caller a call it
    cannot execute. Both are failures, and they are named here.
    """
    if value is None:
        return [], ""
    if not isinstance(value, list):
        return [], f"choices[0].message.tool_calls is {_shape(value)}"
    calls: list[dict[str, Any]] = []
    for i, tc in enumerate(value):
        if not isinstance(tc, dict):
            return [], f"choices[0].message.tool_calls[{i}] is {_shape(tc)}"
        name = _dict(tc.get("function")).get("name")
        if not isinstance(name, str) or not name:
            return [], f"choices[0].message.tool_calls[{i}] has no function.name"
        calls.append(tc)
    return calls, ""


def _provider_error(r: httpx.Response) -> str:
    """Describe a failed response using the provider's own error object.

    Never the body: gateway errors echo request fragments and headers, and a
    trace file is read by people who should not have to see either.

    The provider's error *kind* is kept beside its message because the two
    answer different questions and one message can cover both: Zen reports an
    exhausted free-tier allowance and an ordinary burst limit with the same
    "Rate limit exceeded. Please try again later." text, naming the difference
    only in `error.type` (`FreeUsageLimitError`). Analysis that cannot see the
    kind charges a blocked account to the condition under test.
    """
    try:
        data = r.json()
    except ValueError:
        return f"HTTP {r.status_code} (non-JSON body, {len(r.content)} bytes)"
    kind = _error_kind(data)
    message = _error_message(data)
    head = f"HTTP {r.status_code} {kind}" if kind else f"HTTP {r.status_code}"
    if message:
        return f"{head}: {message}"
    if kind:
        return head
    return f"HTTP {r.status_code} (no error message in provider JSON)"


def _error_kind(data: Any) -> str:
    """The label the provider gave its own error, if it gave one.

    A short structured field only: `type` is where OpenAI-shaped errors name
    the kind, and `code` is what some gateways send instead. Never free text,
    so this cannot become a second copy of the message.
    """
    err = _dict(data).get("error")
    if not isinstance(err, dict):
        return ""
    for key in ("type", "code"):
        val = err.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _error_message(data: Any) -> str:
    """Only structured error fields, so this can never become a body dump.

    Returned unbounded: the length limit belongs after redaction, never
    before, or a truncation boundary can strand the prefix of a credential
    the redactor would otherwise have removed.
    """
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        # The kind (`type`/`code`) is reported separately, by `_error_kind`.
        val = err.get("message")
        if isinstance(val, str) and val:
            return val
    # Cloudflare's REST envelope: {"success": false, "errors": [{code, message}]}
    parts = [
        e["message"]
        for e in _list(data.get("errors"))
        if isinstance(e, dict) and isinstance(e.get("message"), str) and e["message"]
    ]
    if parts:
        return "; ".join(parts)
    top = data.get("message")
    return top if isinstance(top, str) else ""


def _bound(detail: str) -> str:
    """Bound an error string, applied only to already-redacted text."""
    return detail if len(detail) <= _DETAIL_CHARS else detail[:_DETAIL_CHARS] + "..."


def _text(value: Any) -> str:
    """String content, or the text parts of OpenAI's content-part list form."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            p["text"]
            for p in value
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
        )
    return ""


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    """Reported integer, or None for "the provider did not say"."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _shape(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, list) and not value:
        return "an empty list"
    return f"a {type(value).__name__}"


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
