"""Transport contract: what the engine is allowed to believe about a provider.

These assert the properties the loop depends on and cannot re-derive — that a
cancelled fan-out gives its concurrency back, that two aliases of one model
share one quota, that a request tells a provider truthfully who we are and
which conversation it belongs to, that a provider's assistant turn survives
replay intact, that a malformed payload becomes a named failure instead of a
crash or a body dump, and that token accounting never invents a zero it was
not told.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from reasonproxy import upstream as upstream_mod
from reasonproxy import config as config_mod
from reasonproxy.config import Config
from reasonproxy.upstream import Completion, RateGate, Upstream, Usage

KEY = "sk-live-secret-must-never-be-logged"

CONFIG: dict = {
    "providers": {
        "fake": {"base_url": "http://provider.invalid/v1", "api_key_env": "FAKE_KEY"}
    },
    "backends": {
        "ctl": {
            "provider": "fake",
            "model": "controller-1",
            "params": {"temperature": 0.0, "max_tokens": 4096},
        },
        # Same upstream model as ctl with tighter limits: an alias of one
        # quota, not a second quota.
        "ctl-paced": {
            "provider": "fake",
            "model": "controller-1",
            "max_concurrent": 1,
            "rpm": 30,
        },
        "w1": {"provider": "fake", "model": "worker-1", "max_concurrent": 8, "rpm": 10},
    },
    "virtual_models": {"ctl": {"controller": "ctl", "reason_mode": "off"}},
    "trace_path": None,
}

# Two aliases of one model, unpaced: isolates gate sharing from rate pacing.
SHARED: dict = {
    "providers": CONFIG["providers"],
    "backends": {
        "a": {"provider": "fake", "model": "shared-1", "max_concurrent": 1},
        "b": {"provider": "fake", "model": "shared-1", "max_concurrent": 4},
    },
    "virtual_models": {"ctl": {"controller": "a", "reason_mode": "off"}},
    "trace_path": None,
}

MSGS = [{"role": "user", "content": "go"}]

OK_PAYLOAD = {
    "id": "chatcmpl-1",
    "model": "controller-1",
    "choices": [
        {"index": 0, "finish_reason": "stop",
         "message": {"role": "assistant", "content": "done"}}
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
}


def upstream_for(monkeypatch, handler, config: dict = CONFIG) -> Upstream:
    """A real Upstream whose provider client speaks to a mock transport."""
    monkeypatch.setenv("FAKE_KEY", KEY)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        upstream_mod.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return Upstream(Config.model_validate(config))


def instant_retries(monkeypatch) -> None:
    monkeypatch.setattr(upstream_mod, "BACKOFF_S", 0.0)
    monkeypatch.setattr(upstream_mod.random, "random", lambda: 0.0)


@pytest.fixture(autouse=True)
def no_retry_backoff(monkeypatch):
    instant_retries(monkeypatch)


def replies(payload: dict, status: int = 200):
    return lambda request: httpx.Response(status, json=payload)


# --------------------------------------------------------------------------- #
# rate gate: cancellation and alias sharing
# --------------------------------------------------------------------------- #

async def test_cancelling_a_paced_entry_hands_the_permit_back():
    """Cancellation between acquiring the permit and clearing the rate delay.

    `__aexit__` never runs for a body that was never entered, so if entry does
    not release on its own the model's capacity is gone for the process.
    """
    gate = RateGate(max_concurrent=1, rpm=600)  # 100ms pacing

    async def enter() -> str:
        async with gate:
            return "in"

    assert await enter() == "in"  # consumes the first slot; next entrant paces

    paced = asyncio.create_task(enter())
    await asyncio.sleep(0.02)  # inside the pacing sleep, permit held
    paced.cancel()
    with pytest.raises(asyncio.CancelledError):
        await paced

    # A leaked permit would make this hang rather than fail an assertion.
    assert await asyncio.wait_for(enter(), timeout=2.0) == "in"


async def test_a_cancelled_paced_entry_never_rewinds_the_schedule():
    """A reservation is not rolled back on cancellation.

    Later entrants may already be queued behind the abandoned slot, so
    rewinding the clock would let two requests share one rate slot. The
    observable consequence: the next entrant still waits a full interval
    instead of inheriting the abandoned one.
    """
    gate = RateGate(max_concurrent=2, rpm=600)  # 100ms pacing, no queueing

    async def enter() -> None:
        async with gate:
            return None

    await enter()  # takes slot 1

    paced = asyncio.create_task(enter())  # reserves slot 2, then is abandoned
    await asyncio.sleep(0.01)
    paced.cancel()
    with pytest.raises(asyncio.CancelledError):
        await paced

    t0 = time.monotonic()
    await enter()  # must wait for slot 3, not reuse slot 2
    assert time.monotonic() - t0 >= 0.1


def test_aliases_of_one_upstream_model_share_the_strictest_gate():
    up = Upstream(Config.model_validate(CONFIG))
    assert up.gate("ctl") is up.gate("ctl-paced")

    shared = up.gate("ctl")
    assert shared.max_concurrent == 1  # min(default 4, 1)
    # ctl declares no rpm; that absence must not relax the alias that does.
    assert shared.rpm == 30

    other = up.gate("w1")
    assert other is not shared
    assert (other.max_concurrent, other.rpm) == (8, 10)


async def test_alias_traffic_counts_against_the_same_model_concurrency(monkeypatch):
    inflight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.02)
        inflight -= 1
        return httpx.Response(200, json=OK_PAYLOAD)

    up = upstream_for(monkeypatch, handler, SHARED)
    results = await asyncio.gather(
        up.complete("a", MSGS), up.complete("b", MSGS), up.complete("a", MSGS)
    )
    assert all(c.ok for c in results)
    assert peak == 1


async def test_a_cancelled_call_does_not_consume_the_models_slot(monkeypatch):
    """Branch deadlines cancel in-flight calls; the next task must still run."""
    started = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.sleep(30)
        return httpx.Response(200, json=OK_PAYLOAD)

    up = upstream_for(monkeypatch, handler, SHARED)
    stuck = asyncio.create_task(up.complete("a", MSGS))
    await asyncio.wait_for(started.wait(), timeout=2.0)
    stuck.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stuck

    admitted = await asyncio.wait_for(up.complete("b", MSGS), timeout=2.0)
    assert admitted.ok and admitted.content == "done"
    await up.aclose()


# --------------------------------------------------------------------------- #
# request body: precedence and proxy-owned wiring
# --------------------------------------------------------------------------- #

async def test_overrides_beat_backend_params_but_cannot_retarget_the_call(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=OK_PAYLOAD)

    up = upstream_for(monkeypatch, handler)
    tools = [{"type": "function", "function": {"name": "shell", "parameters": {}}}]
    await up.complete(
        "ctl",
        MSGS,
        tools=tools,
        tool_choice="none",
        overrides={
            "temperature": 0.9,
            "response_format": {"type": "json_object"},
            "stop": None,
            # A caller must not be able to redirect the call or blank the
            # tool contract through generic parameters.
            "model": "somewhere-else",
            "messages": [],
            "tools": [],
            "tool_choice": "auto",
            "stream": True,
            "n": 4,
        },
    )

    assert seen["temperature"] == 0.9  # explicit override wins over params 0.0
    assert seen["max_tokens"] == 4096  # untouched backend param survives
    assert seen["response_format"] == {"type": "json_object"}
    assert seen["model"] == "controller-1"
    assert seen["messages"] == MSGS
    assert [t["function"]["name"] for t in seen["tools"]] == ["shell"]
    # `none` is an instruction (a branch must not act), not an absence.
    assert seen["tool_choice"] == "none"
    assert "stop" not in seen  # an explicit null is not a value to forward
    assert "stream" not in seen and "n" not in seen


async def test_an_explicit_tool_choice_is_forwarded_even_with_no_tools(monkeypatch):
    """The caller's choice is an instruction in its own right; dropping it
    because the tool list happens to be empty silently changes the request."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=OK_PAYLOAD)

    up = upstream_for(monkeypatch, handler)
    await up.complete("ctl", MSGS, tool_choice="none")

    assert seen["tool_choice"] == "none"
    assert "tools" not in seen


# --------------------------------------------------------------------------- #
# request identity: who we are, and which conversation this is
# --------------------------------------------------------------------------- #

# The one endpoint on the roster that routes by conversation. Concurrency is
# left open here (and no rpm declared) so two turns can really be in flight.
ZEN: dict = {
    "providers": {
        "fake": {"base_url": "https://opencode.ai/zen/v1", "api_key_env": "FAKE_KEY"}
    },
    "backends": {"ctl": {"provider": "fake", "model": "controller-1", "max_concurrent": 4}},
    "virtual_models": {"ctl": {"controller": "ctl", "reason_mode": "off"}},
    "trace_path": None,
}


def capturing() -> tuple[list[httpx.Request], object]:
    """A mock provider that answers everything and keeps the requests."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_PAYLOAD)

    return seen, handler


async def test_the_bound_conversation_reaches_a_session_routed_provider(monkeypatch):
    """Zen rejects a request that names no conversation, so the id this turn is
    bound to has to arrive on the wire: as a header, under our own name, and
    without disturbing the credential the provider actually authenticates."""
    seen, handler = capturing()
    up = upstream_for(monkeypatch, handler, ZEN)

    with upstream_mod.session_scope("ses_native_7f3a"):
        assert (await up.complete("ctl", MSGS)).ok

    [request] = seen
    assert request.headers["x-opencode-session"] == "ses_native_7f3a"
    assert request.headers["user-agent"] == "ReasonProxy/0.1"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    # A conversation is transport routing, never a model-visible request field.
    assert "ses_native_7f3a" not in json.dumps(body)
    assert [k for k in body if "session" in k.lower()] == []


async def test_a_provider_that_never_asked_is_not_told_the_conversation(monkeypatch):
    """The id goes to the endpoint that documented the need for it, not to
    every endpoint the roster happens to contain. Our identity is not
    conditional, though: it is who we are on any request we make."""
    seen, handler = capturing()
    up = upstream_for(monkeypatch, handler)  # CONFIG: an unrelated endpoint

    with upstream_mod.session_scope("ses_native_7f3a"):
        assert (await up.complete("ctl", MSGS)).ok

    [request] = seen
    assert "x-opencode-session" not in request.headers
    assert request.headers["user-agent"] == "ReasonProxy/0.1"


async def test_a_retry_keeps_its_own_conversation_while_another_turn_runs(monkeypatch):
    """One client carries every request to a provider, and Zen answers 429 under
    exactly the load this proxy creates -- so a turn's retry goes out while
    other turns are mid-flight. A session held on the client, or on this object,
    would be whichever turn set it last, and the retry would route as that one.
    """
    instant_retries(monkeypatch)
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:  # the first turn is throttled and must come back
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=OK_PAYLOAD)

    up = upstream_for(monkeypatch, handler, ZEN)

    async def turn(session: str, messages: list[dict]) -> None:
        with upstream_mod.session_scope(session):
            assert (await up.complete("ctl", messages)).ok

    await asyncio.gather(
        turn("ses_alpha", MSGS),
        turn("ses_beta", [{"role": "user", "content": "beta"}]),
    )

    assert [
        (json.loads(r.content)["messages"][0]["content"], r.headers["x-opencode-session"])
        for r in seen
    ] == [("go", "ses_alpha"), ("beta", "ses_beta"), ("go", "ses_alpha")]


async def test_an_unbound_call_derives_one_id_per_conversation(monkeypatch):
    """A conformance probe drives this class directly, with no request to
    inherit a conversation from. It must still route, every step of one
    trajectory must route as one conversation, and two tasks must not collapse
    into one -- which is what the opening of a trajectory identifies."""
    seen, handler = capturing()
    up = upstream_for(monkeypatch, handler, ZEN)
    opening = [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "Fix the path-normalization cache bug."},
    ]
    later_step = [
        *opening,
        {"role": "assistant", "content": "Looking at the cache key."},
        {"role": "user", "content": "exit code 1"},
    ]
    other_task = [opening[0], {"role": "user", "content": "Fix the retry backoff."}]

    for messages in (opening, later_step, other_task):
        assert (await up.complete("ctl", messages)).ok

    first, second, third = (r.headers["x-opencode-session"] for r in seen)
    assert first == second != third
    assert first.startswith("rp-") and len(first) == len("rp-") + 32
    assert "path-normalization" not in first  # a digest, never the task text


async def test_a_roster_header_cannot_dress_the_proxy_up_as_another_client(monkeypatch):
    """`Provider.headers` is a generic passthrough, so it is also the one way a
    config edit could make this proxy claim to be somebody else. It cannot."""
    seen, handler = capturing()
    spoofing = {
        **ZEN,
        "providers": {
            "fake": {
                "base_url": "https://opencode.ai/zen/v1",
                "api_key_env": "FAKE_KEY",
                "headers": {"User-Agent": "opencode/1.2.3"},
            }
        },
    }
    up = upstream_for(monkeypatch, handler, spoofing)

    with upstream_mod.session_scope("ses_native_7f3a"):
        assert (await up.complete("ctl", MSGS)).ok

    assert seen[0].headers["user-agent"] == "ReasonProxy/0.1"

# --------------------------------------------------------------------------- #
# provider turn: lossless replay
# --------------------------------------------------------------------------- #

REPLAY_PAYLOAD = {
    "id": "chatcmpl-2",
    "model": "nemotron-3.5-lightning-free",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "logprobs": {"content": [{"token": "ok", "logprob": -0.25}]},
            "message": {
                "role": "assistant",
                "content": "Checking the cache key.",
                "refusal": None,
                "reasoning_content": "Insertion skips realpath; lookup does not.",
                "reasoning_details": [{"type": "text", "text": "private trace"}],
                "annotations": [],
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "index": 0,
                        "function": {"name": "shell", "arguments": '{"cmd":"rg -n normalize"}'},
                    }
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 284, "completion_tokens": 134,
              "completion_tokens_details": {"reasoning_tokens": 139}},
}


async def test_the_provider_turn_replays_verbatim_and_independently(monkeypatch):
    up = upstream_for(monkeypatch, replies(REPLAY_PAYLOAD))
    c = await up.complete("ctl", MSGS)

    assert c.ok and c.model == "nemotron-3.5-lightning-free"
    assert c.content == "Checking the cache key."
    # Hidden reasoning stays in its own field, never folded into content.
    assert c.reasoning == "Insertion skips realpath; lookup does not."
    assert "realpath" not in c.content

    msg = c.as_assistant_message()
    assert msg["role"] == "assistant"
    assert msg["content"] == "Checking the cache key."
    assert msg["reasoning_content"] == c.reasoning
    assert msg["reasoning_details"] == [{"type": "text", "text": "private trace"}]
    assert "refusal" in msg and msg["refusal"] is None
    # Provider-specific fields on the call survive, including `index`.
    assert msg["tool_calls"][0]["index"] == 0
    assert msg["tool_calls"][0]["function"]["arguments"] == '{"cmd":"rg -n normalize"}'

    # Independent: a caller extending the replayed turn cannot corrupt the record.
    msg["content"] = "mutated"
    msg["tool_calls"][0]["function"]["arguments"] = "{}"
    assert c.raw_message["content"] == "Checking the cache key."
    fresh = c.as_assistant_message()
    assert fresh["tool_calls"][0]["function"]["arguments"] == '{"cmd":"rg -n normalize"}'

    # Per-choice data the message does not carry stays available.
    assert c.raw_response["choices"][0]["logprobs"]["content"][0]["token"] == "ok"


async def test_a_refusal_is_a_replayable_turn_not_content(monkeypatch):
    payload = {
        "model": "controller-1",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": None,
                            "refusal": "I cannot help with that."},
            }
        ],
    }
    up = upstream_for(monkeypatch, replies(payload))
    c = await up.complete("ctl", MSGS)

    assert c.ok and c.content == "" and c.text == ""
    msg = c.as_assistant_message()
    assert msg["refusal"] == "I cannot help with that."
    assert msg["content"] is None


def test_completions_without_a_payload_replay_in_canonical_shape():
    call = {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}
    stub = Completion("ctl", "m", "hi", [call], "tool_calls", {}, 5)
    assert stub.as_assistant_message() == {
        "role": "assistant", "content": "hi", "tool_calls": [call]
    }
    stub.as_assistant_message()["tool_calls"].clear()
    assert stub.tool_calls == [call]

    failed = Completion.failed("ctl", "m", "timeout", "deadline exceeded", 12)
    assert failed.as_assistant_message() == {"role": "assistant", "content": None}


def test_text_is_completed_public_content_only():
    """The loop must never present a partial or hidden thought as an answer."""
    assert Completion("w1", "m", "done", [], "stop", {}, 5).text == "done"

    cut = Completion("w1", "m", "half a thought", [], "length", {}, 5)
    assert cut.truncated and cut.content == "half a thought" and cut.text == ""

    hidden = Completion("w1", "m", "", [], "stop", {}, 5, reasoning="all the thinking")
    assert hidden.reasoning_only and hidden.text == ""

    failed = Completion.failed("w1", "m", "error", "HTTP 500", 5)
    assert not failed.ok and failed.text == ""


# --------------------------------------------------------------------------- #
# malformed payloads and provider quirks
# --------------------------------------------------------------------------- #

BODY_MARKER = "echoed-request-fragment-and-key"

MALFORMED = [
    ({"choices": None, "debug": BODY_MARKER}, "choices is null"),
    ({"choices": [], "debug": BODY_MARKER}, "choices is an empty list"),
    ({"choices": ["oops"], "debug": BODY_MARKER}, "choices[0] is a str"),
    ({"choices": [{"message": None}], "debug": BODY_MARKER}, "choices[0].message is null"),
    ({"choices": [{"message": "text"}], "debug": BODY_MARKER},
     "choices[0].message is a str"),
    ([{"choices": []}], "top level is a list"),
]


@pytest.mark.parametrize("payload, fragment", MALFORMED)
async def test_a_malformed_success_body_becomes_a_named_failure(monkeypatch, payload, fragment):
    up = upstream_for(monkeypatch, replies(payload))
    c = await up.complete("ctl", MSGS)

    assert not c.ok and c.status == "error"
    assert c.error is not None and fragment in c.error
    assert BODY_MARKER not in c.error  # shape only: never a body dump
    # Safe for the loop to inspect without type checks of its own.
    assert c.content == "" and c.tool_calls == [] and c.usage == {}


async def test_a_non_json_body_is_reported_by_size_not_content(monkeypatch):
    body = b"<html>gateway error for " + BODY_MARKER.encode() + b"</html>"
    up = upstream_for(monkeypatch, lambda r: httpx.Response(200, content=body))
    c = await up.complete("ctl", MSGS)

    assert not c.ok and c.error is not None
    assert "body is not JSON" in c.error and f"{len(body)} bytes" in c.error
    assert BODY_MARKER not in c.error and "html" not in c.error


async def test_provider_quirks_are_normalized_without_losing_the_payload(monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "part one "},
                        {"type": "text", "text": "part two"},
                        {"type": "image_url", "image_url": {"url": "x"}},
                    ],
                    "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "shell", "arguments": "{}"}}
                    ],
                }
            }
        ],
        "model": 12,
        "usage": None,
    }
    up = upstream_for(monkeypatch, replies(payload))
    c = await up.complete("ctl", MSGS)

    assert c.ok
    assert c.content == "part one part two"
    assert [tc["id"] for tc in c.tool_calls] == ["c1"]
    # A tool call is not a stop, even when the provider forgets to say so.
    assert c.finish_reason == "tool_calls"
    assert c.model == "controller-1"  # unusable echo -> the backend's own model
    assert c.usage == {}
    # Nothing dropped from the record itself.
    assert c.raw_message["content"][2]["type"] == "image_url"


BROKEN_TOOL_SURFACE = [
    ("a str instead of a list", "not-a-list", "tool_calls is a str"),
    (
        "a junk entry beside a real call",
        [{"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
         "not-a-call"],
        "tool_calls[1] is a str",
    ),
    (
        "an entry with no callable name",
        [{"id": "c1", "type": "function", "function": {"arguments": "{}"}}],
        "tool_calls[0] has no function.name",
    ),
]


@pytest.mark.parametrize(
    "case, tool_calls, fragment", BROKEN_TOOL_SURFACE, ids=[c[0] for c in BROKEN_TOOL_SURFACE]
)
async def test_a_broken_tool_surface_is_a_failure_not_a_filtered_success(
    monkeypatch, case, tool_calls, fragment
):
    """Trimming bad entries would either hide an action the model believes it
    requested or hand the caller a call it cannot execute."""
    payload = {
        "choices": [
            {"finish_reason": "tool_calls",
             "message": {"role": "assistant", "content": None, "tool_calls": tool_calls}}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    up = upstream_for(monkeypatch, replies(payload))
    c = await up.complete("ctl", MSGS)

    assert not c.ok and c.status == "error"
    assert c.error is not None and fragment in c.error
    assert c.tool_calls == []
    # A failed completion never presents itself as a replayable provider turn.
    assert c.raw_message == {} and c.as_assistant_message() == {
        "role": "assistant", "content": None
    }


# --------------------------------------------------------------------------- #
# failures: retries, redaction, attempt accounting
# --------------------------------------------------------------------------- #

async def test_retryable_status_is_bounded_and_reported_without_the_body(monkeypatch):
    instant_retries(monkeypatch)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            503,
            json={
                "error": {"message": "Upstream request failed: Model is unavailable.",
                          "type": "server_error"},
                "request_echo": {"authorization": f"Bearer {KEY}", "prompt": BODY_MARKER},
            },
        )

    up = upstream_for(monkeypatch, handler)
    c = await up.complete("ctl", MSGS)
    assert attempts == config_mod.DEFAULT_MAX_ATTEMPTS
    assert c.attempts == config_mod.DEFAULT_MAX_ATTEMPTS and c.status == "error"
    assert c.error is not None
    assert "HTTP 503" in c.error and "Model is unavailable" in c.error
    assert BODY_MARKER not in c.error and KEY not in c.error


async def test_a_transient_status_that_recovers_records_the_real_attempt_count(monkeypatch):
    instant_retries(monkeypatch)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json=OK_PAYLOAD)

    up = upstream_for(monkeypatch, handler)
    c = await up.complete("ctl", MSGS)

    assert c.ok and c.content == "done" and c.attempts == 3


async def test_a_quota_rejection_is_distinguishable_from_a_burst_limit(monkeypatch):
    """One provider message covers two different verdicts: an exhausted
    free-tier allowance and an ordinary burst limit both arrive as 429 "Rate
    limit exceeded. Please try again later.", named apart only by error.type.
    A trace that keeps only the message charges a blocked account to whichever
    condition happened to be running."""
    instant_retries(monkeypatch)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        error: dict = {"message": "Rate limit exceeded. Please try again later."}
        if calls == 1:
            error["type"] = "FreeUsageLimitError"
        return httpx.Response(429, json={"error": error})

    up = upstream_for(monkeypatch, handler, ZEN)
    blocked = await up.complete("ctl", MSGS)
    assert calls == 1 and blocked.attempts == 1
    throttled = await up.complete("ctl", MSGS)

    assert blocked.error is not None and throttled.error is not None
    assert "FreeUsageLimitError" in blocked.error
    assert "FreeUsageLimitError" not in throttled.error
    assert blocked.error != throttled.error
    # The provider's own words are still there, unparaphrased.
    assert "Rate limit exceeded" in blocked.error and "HTTP 429" in blocked.error


async def test_credentials_echoed_by_a_provider_are_redacted(monkeypatch):
    up = upstream_for(
        monkeypatch,
        replies({"error": {"message": f"invalid api key {KEY} (Bearer {KEY})"}}, status=401),
    )
    c = await up.complete("ctl", MSGS)

    assert not c.ok and c.attempts == 1  # 401 is not transient
    assert c.error is not None
    assert KEY not in c.error and "[redacted]" in c.error


async def test_a_credential_at_the_length_bound_cannot_leak_a_prefix(monkeypatch):
    """Redaction happens before the length bound, never after.

    Bounding first would cut a key mid-string, and the exact-match redactor
    would then walk straight past the surviving prefix.
    """
    up = upstream_for(
        monkeypatch,
        replies({"error": {"message": "x" * 290 + KEY + " trailing detail"}}, status=400),
    )
    c = await up.complete("ctl", MSGS)

    assert c.error is not None
    assert KEY[:8] not in c.error  # not even the first characters
    assert c.error.endswith("...")  # the bound still applied


async def test_cloudflare_error_envelopes_are_read_structurally(monkeypatch):
    up = upstream_for(
        monkeypatch,
        replies({"success": False, "errors": [{"code": 3036, "message": "capacity exceeded"}],
                 "result": None, "debug": BODY_MARKER}, status=400),
    )
    c = await up.complete("ctl", MSGS)

    assert c.error is not None
    assert "HTTP 400" in c.error and "capacity exceeded" in c.error
    assert BODY_MARKER not in c.error


async def test_exhausted_timeout_retries_preserve_timeout_identity(monkeypatch):
    instant_retries(monkeypatch)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timed out", request=request)

    up = upstream_for(monkeypatch, handler)
    c = await up.complete("ctl", MSGS)

    assert attempts == config_mod.DEFAULT_MAX_ATTEMPTS and c.attempts == attempts
    assert c.status == "timeout" and not c.ok and c.model == "controller-1"


async def test_transport_errors_retry_and_report_the_exception_kind(monkeypatch):
    instant_retries(monkeypatch)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError(f"refused while using {KEY}", request=request)

    up = upstream_for(monkeypatch, handler)
    c = await up.complete("ctl", MSGS)
    assert attempts == config_mod.DEFAULT_MAX_ATTEMPTS and c.attempts == config_mod.DEFAULT_MAX_ATTEMPTS
    assert KEY not in c.error

async def test_workers_ai_null_choices_and_error_finish_retry(monkeypatch):
    """The flaky shapes a 200 can carry: an empty answer envelope.

    A 200 with `choices: null` ran a billed generation and answered nothing,
    and Cloudflare also reports provider-side failures as a normal envelope
    whose one choice ends `finish_reason: "error"`. Both are transient provider
    failures: retrying them cannot fish for a different model answer because
    there was no answer. The discarded attempt's usage stays on the record.
    """
    instant_retries(monkeypatch)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={"id": "c1", "model": "controller-1", "choices": None,
                      "usage": {"prompt_tokens": 10}},
            )
        if calls == 2:
            return httpx.Response(
                200,
                json={"id": "c2", "model": "controller-1",
                      "choices": [{"index": 0, "finish_reason": "error",
                                   "message": {"role": "assistant", "content": None}}],
                      "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
            )
        return httpx.Response(200, json=OK_PAYLOAD)

    up = upstream_for(monkeypatch, handler)
    c = await up.complete("ctl", MSGS)

    assert c.ok and c.content == "done" and c.attempts == 3
    usage = Usage()
    usage.add("controller", c)
    assert usage.accounting() == {
        "prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34,
        "unreported_calls": 0, "unaccounted_attempts": 1,
    }


# The envelope Zen answered a forced-tool turn with, verbatim from
# runs/onboarding/private-tool-controls.json: a success status line, the real
# status only as prose inside the message, and no number anywhere structured.
ZEN_SERVER_ERROR: dict = {
    "error": {
        "type": "server_error",
        "message": (
            "Error from provider (Console): Upstream request failed: "
            "[504] Upstream idle timeout exceeded"
        ),
    }
}


async def test_a_server_error_envelope_under_a_200_is_transient(monkeypatch):
    """The remaining flaky shape a 200 can carry: an error object instead of
    an answer, naming a transient upstream failure in `error.type`.

    Classified from the status line alone this is terminal, and the turn dies
    with its attempt budget unspent -- which is what happened: a forced-tool
    turn was abandoned on attempt 3 against a model that answered the same
    request when its tool selection was left automatic. Retrying cannot fish
    for a different model answer, because there was no answer; and whatever
    the discarded attempts reported spending stays on the record.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:  # told us nothing about what it cost
            return httpx.Response(200, json=ZEN_SERVER_ERROR)
        if calls == 2:  # billed a generation, then failed
            return httpx.Response(
                200,
                json={**ZEN_SERVER_ERROR,
                      "usage": {"prompt_tokens": 10, "completion_tokens": 0}},
            )
        return httpx.Response(200, json=OK_PAYLOAD)

    up = upstream_for(monkeypatch, handler, ZEN)
    c = await up.complete("ctl", MSGS)

    assert c.ok and c.content == "done"
    assert calls == 3 and c.attempts == 3  # recovered inside the budget
    usage = Usage()
    usage.add("controller", c)
    assert usage.accounting() == {
        "prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22,
        "unreported_calls": 0, "unaccounted_attempts": 1,
    }


async def test_a_quota_rejection_wearing_the_transient_label_stays_terminal(monkeypatch):
    """A blocked account outranks the label the gateway happened to use.

    A 200-status envelope is classified from its label, and nothing stops a
    gateway from putting its generic server-error label on a refusal it will
    keep making. Retrying that spends the whole budget of a quota that is
    already gone, and buries the verdict under a transport story.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"error": {
            "type": "server_error",
            "message": "Free usage limit reached. Add credits to continue.",
        }})

    up = upstream_for(monkeypatch, handler, ZEN)
    c = await up.complete("ctl", MSGS)

    assert calls == 1 and c.attempts == 1 and not c.ok
    assert c.error is not None
    assert "HTTP 200 server_error" in c.error and "Free usage limit" in c.error


async def test_a_200_envelope_that_names_no_transient_failure_is_terminal(monkeypatch):
    """Only the labels observed reporting a transport failure are transient.

    Treating any error under a 200 as retryable would send a request the
    provider rejects on its merits the full budget of times and report the
    last rejection as the outcome of a retry storm.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"error": {
            "type": "invalid_request_error",
            "message": "tool_choice: function tools are not supported by this model",
        }})

    up = upstream_for(monkeypatch, handler, ZEN)
    c = await up.complete("ctl", MSGS)

    assert calls == 1 and c.attempts == 1 and not c.ok
    assert c.error is not None and "invalid_request_error" in c.error


async def test_retry_after_header_is_honoured_over_backoff(monkeypatch):
    """The provider's own pacing beats our exponential guess."""
    waits: list[float] = []
    real_sleep = asyncio.sleep

    async def sleepy(delay: float) -> None:
        waits.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(upstream_mod.asyncio, "sleep", sleepy)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429, headers={"Retry-After": "7"}, json={"error": {"message": "busy"}}
            )
        return httpx.Response(200, json=OK_PAYLOAD)

    up = upstream_for(monkeypatch, handler)
    c = await up.complete("ctl", MSGS)

    assert c.ok and calls == 2
    assert waits and waits[0] == 7.0


async def test_retry_after_http_date_is_parsed(monkeypatch):
    from datetime import UTC, datetime, timedelta

    waits: list[float] = []
    real_sleep = asyncio.sleep

    async def sleepy(delay: float) -> None:
        waits.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(upstream_mod.asyncio, "sleep", sleepy)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            when = datetime.now(UTC) + timedelta(seconds=5)
            stamp = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
            return httpx.Response(
                429, headers={"Retry-After": stamp}, json={"error": {"message": "busy"}}
            )
        return httpx.Response(200, json=OK_PAYLOAD)

    up = upstream_for(monkeypatch, handler)
    c = await up.complete("ctl", MSGS)

    assert c.ok and calls == 2
    assert waits and 4.0 <= waits[0] <= 5.0


async def test_per_provider_attempt_budget_overrides_the_default(monkeypatch):
    """A flaky endpoint gets a bigger budget; a stable one is not widened."""
    instant_retries(monkeypatch)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"message": "cold gateway"}})

    flaky = upstream_for(
        monkeypatch, handler,
        {**CONFIG, "providers": {"fake": {
            "base_url": "http://provider.invalid/v1",
            "api_key_env": "FAKE_KEY",
            "max_attempts": 8,
        }}},
    )
    c = await flaky.complete("ctl", MSGS)

    assert calls == 8 and c.attempts == 8 and c.status == "error"


# --------------------------------------------------------------------------- #
# usage accounting
# --------------------------------------------------------------------------- #

CF_USAGE = {
    "prompt_tokens": 276,
    "completion_tokens": 271,
    "total_tokens": 547,
    "prompt_tokens_details": {"cached_tokens": 0},
    "neurons": 49.276,
}
ZEN_USAGE = {
    "prompt_tokens": 284,
    "completion_tokens": 134,
    "total_tokens": 418,
    "prompt_tokens_details": {"audio_tokens": 0, "cached_tokens": 192,
                              "cache_write_tokens": 8},
    "completion_tokens_details": {"audio_tokens": 0, "reasoning_tokens": 139},
}


def test_usage_rows_keep_the_provider_accounting_verbatim():
    u = Usage()
    u.add("controller", Completion("ctl", "@cf/nvidia/nemotron-3-120b-a12b", "x", [],
                                   "stop", CF_USAGE, 2070))
    u.add("branch", Completion("w1", "nemotron-3.5-lightning-free", "y", [], "stop",
                               ZEN_USAGE, 6570))

    cf, zen = u.rows
    assert cf["raw"] is CF_USAGE  # untouched record of truth
    assert cf["neurons"] == 49.276  # Workers AI bills in neurons, not tokens
    assert cf["reasoning_tokens"] is None  # not reported, not zero
    assert cf["cached_input_tokens"] == 0  # reported zero stays zero

    assert zen["reasoning_tokens"] == 139
    assert zen["cached_input_tokens"] == 192 and zen["cache_write_tokens"] == 8
    assert zen["role"] == "branch" and zen["status"] == "ok"
    assert zen["latency_ms"] == 6570 and zen["finish_reason"] == "stop"


def test_totals_sum_known_data_and_declare_what_is_unknown():
    u = Usage()
    # A clean call: everything reported, nothing unknown.
    u.add("controller", Completion("ctl", "m", "x", [], "stop", CF_USAGE, 10))
    # A call that 429'd twice before succeeding: the usage object describes
    # the third attempt only, so two real attempts are unaccounted for.
    u.add("branch", Completion("w1", "m", "y", [], "stop", ZEN_USAGE, 10, attempts=3))
    # Partial usage is incomplete, not complete-with-a-zero.
    u.add("branch", Completion("w2", "m", "z", [], "stop", {"prompt_tokens": 120}, 10))
    # A failed call still cost the provider work; claiming zero would
    # understate the run.
    u.add("branch", Completion.failed("w3", "m", "timeout", "branch deadline", 120000,
                                      attempts=config_mod.DEFAULT_MAX_ATTEMPTS))

    clean, retried, partial, dead = u.rows
    assert clean["usage_complete"] and clean["unaccounted_attempts"] == 0
    assert retried["usage_complete"] and retried["unaccounted_attempts"] == 2
    assert partial["input_tokens"] == 120 and partial["output_tokens"] is None
    assert not partial["usage_complete"] and partial["unaccounted_attempts"] == 1
    assert dead["input_tokens"] is None and dead["raw"] == {} and dead["status"] == "timeout"
    assert not dead["usage_complete"] and dead["unaccounted_attempts"] == config_mod.DEFAULT_MAX_ATTEMPTS

    # Standard usage object: token sums plus the incomplete-call count, and
    # nothing else. An attempt count in here would be read as a billing field.
    assert u.totals() == {
        "prompt_tokens": 680,
        "completion_tokens": 405,
        "total_tokens": 1085,
        "unreported_calls": 2,
    }
    assert u.totals(roles={"controller"}) == {
        "prompt_tokens": 276,
        "completion_tokens": 271,
        "total_tokens": 547,
        "unreported_calls": 0,
    }

    # Full honesty block, for reasonproxy_usage and the trace only.
    assert u.accounting() == {
        "prompt_tokens": 680,
        "completion_tokens": 405,
        "total_tokens": 1085,
        "unreported_calls": 2,
        "unaccounted_attempts": 7,
    }
    assert u.accounting(roles={"controller"})["unaccounted_attempts"] == 0
    assert u.unreported() == 2 and u.unreported(roles={"controller"}) == 0
    assert u.unaccounted_attempts() == 7
    assert u.unaccounted_attempts(roles={"controller"}) == 0


# --------------------------------------------------------------------------- #
# responses protocol: same contract, second wire shape
# --------------------------------------------------------------------------- #

RCONFIG: dict = {
    "providers": {
        "resp": {
            "base_url": "http://provider.invalid/v1",
            "api_key_env": "FAKE_KEY",
            "protocol": "responses",
        }
    },
    "backends": {
        "ctl": {"provider": "resp", "model": "controller-1"},
    },
    "virtual_models": {"ctl": {"controller": "ctl", "reason_mode": "off"}},
    "trace_path": None,
}

R_OK = {
    "id": "resp_1",
    "object": "response",
    "status": "completed",
    "model": "controller-1",
    "output": [
        {
            "type": "message",
            "id": "m1",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}],
        }
    ],
    "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
}


def rcall(content="go ahead", args='{"command": "ls"}'):
    return {
        "id": "resp_2",
        "object": "response",
        "status": "completed",
        "model": "controller-1",
        "output": [
            {
                "type": "message",
                "id": "m2",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content}],
            },
            {
                "type": "function_call",
                "id": "f1",
                "call_id": "call_r1",
                "name": "bash",
                "arguments": args,
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }


async def test_responses_path_posts_to_responses_and_parses_turn(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=R_OK)

    up = upstream_for(monkeypatch, handler, RCONFIG)
    c = await up.complete("ctl", MSGS)

    assert seen[0].url.path == "/v1/responses"
    assert c.ok and c.content == "done" and c.finish_reason == "stop"
    assert c.usage == {"prompt_tokens": 10, "completion_tokens": 2}


async def test_responses_tool_call_arrives_in_chat_shape(monkeypatch):
    up = upstream_for(monkeypatch, replies(rcall()), RCONFIG)
    c = await up.complete(
        "ctl", MSGS, tools=[{"type": "function", "function": {"name": "bash"}}]
    )

    assert c.ok and c.finish_reason == "tool_calls"
    assert c.tool_calls == [
        {
            "id": "call_r1",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"command": "ls"}'},
        }
    ]
    # Replays through the same translation the next request uses.
    assert c.as_assistant_message()["tool_calls"] == c.tool_calls


async def test_responses_history_becomes_call_and_output_items(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=R_OK)

    up = upstream_for(monkeypatch, handler, RCONFIG)
    await up.complete(
        "ctl",
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_r1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_r1", "content": "ok"},
        ],
    )

    items = seen[0]["input"]
    assert items[0] == {"role": "user", "content": "go"}
    assert items[1] == {
        "type": "function_call",
        "call_id": "call_r1",
        "name": "bash",
        "arguments": "{}",
    }
    assert items[2] == {
        "type": "function_call_output",
        "call_id": "call_r1",
        "output": "ok",
    }


async def test_responses_reducer_format_becomes_text_format(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=R_OK)

    up = upstream_for(monkeypatch, handler, RCONFIG)
    await up.complete(
        "ctl", MSGS, overrides={"response_format": {"type": "json_object"}}
    )

    assert seen[0]["text"] == {"format": {"type": "json_object"}}
    assert "response_format" not in seen[0]


async def test_responses_empty_output_is_named_and_retried(monkeypatch):
    up = upstream_for(
        monkeypatch,
        replies({"status": "completed", "output": [], "model": "controller-1"}),
        RCONFIG,
    )
    c = await up.complete("ctl", MSGS)

    assert not c.ok and c.error == "empty output" and c.attempts == 4


async def test_responses_broken_call_is_malformed_not_trimmed(monkeypatch):
    bad = rcall()
    bad["output"][1] = {"type": "function_call", "call_id": "c9", "arguments": "{}"}
    up = upstream_for(monkeypatch, replies(bad), RCONFIG)
    c = await up.complete("ctl", MSGS)

    assert not c.ok and "no function name" in c.error


async def test_responses_server_error_envelope_is_transient(monkeypatch):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, json={"type": "error", "error": "boom"})
        return httpx.Response(200, json=R_OK)

    up = upstream_for(monkeypatch, handler, RCONFIG)
    c = await up.complete("ctl", MSGS)

    assert c.ok and calls == 2


# --------------------------------------------------------------------------
# reasoning budget: the one sampling-adjacent knob this project sets on purpose
# --------------------------------------------------------------------------


EFFORT: dict = {
    "providers": CONFIG["providers"],
    "backends": {
        "ctl": {
            "provider": "fake",
            "model": "controller-1",
            "params": {"reasoning_effort": "high", "reasoning": {"effort": "high"}},
        }
    },
    "virtual_models": {"ctl": {"controller": "ctl", "reason_mode": "off"}},
    "trace_path": None,
}


async def test_reasoning_budget_params_reach_the_wire(monkeypatch):
    """Effort is the variable under test, so it must arrive verbatim.

    Both spellings are in the wild - OpenAI-style `reasoning_effort` and the
    nested `reasoning: {effort}` - and neither is proxy-owned, so the transport
    forwards them unchanged while still owning model, messages and the tool
    surface.
    """
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=OK_PAYLOAD)

    up = upstream_for(monkeypatch, handler, EFFORT)
    c = await up.complete("ctl", MSGS)

    assert c.ok
    assert seen[0]["reasoning_effort"] == "high"
    assert seen[0]["reasoning"] == {"effort": "high"}
    assert seen[0]["model"] == "controller-1" and seen[0]["messages"] == MSGS


async def test_caller_cannot_override_declared_reasoning_budget_with_null(monkeypatch):
    """A null override drops the key instead of sending JSON null upstream.

    The experiment declares the budget in the roster; a caller that blanks it
    would silently change the condition under test, so the wire shows the
    absence rather than a null a provider might read as "default".
    """
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=OK_PAYLOAD)

    up = upstream_for(monkeypatch, handler, EFFORT)
    c = await up.complete("ctl", MSGS, overrides={"reasoning_effort": None})

    assert c.ok
    assert "reasoning_effort" not in seen[0]
    assert seen[0]["reasoning"] == {"effort": "high"}
