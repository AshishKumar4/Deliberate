"""HTTP contract: what an unmodified OpenAI client sees.

The proxy's whole claim is that a caller changes only base_url and model, so the
wire shape, the status classification (400 caller / 401 stranger / 502 upstream
/ 500 us), the SSE replay and the trace record are the deliverable. These tests
drive the real startup path, and where TestClient cannot express the situation
-- a caller that hangs up while the fan-out is running, two turns in flight at
once -- they speak raw ASGI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from reasonproxy import app as app_module
from reasonproxy.engine import Engine, UpstreamError
from reasonproxy.prompts import REASON_WIRE_NAME, TAG
from reasonproxy.upstream import Completion, current_session
from tests.test_engine import Stub, ok, reason_call, shell_call

CONFIG: dict[str, Any] = {
    "providers": {"fake": {"base_url": "http://x/v1", "api_key_env": "FAKE_KEY"}},
    "backends": {
        "ctrl": {"provider": "fake", "model": "controller-1"},
        "w1": {"provider": "fake", "model": "worker-1"},
        "w2": {"provider": "fake", "model": "worker-2"},
        "red": {"provider": "fake", "model": "reducer-1"},
    },
    "virtual_models": {
        "vm": {
            "controller": "ctrl",
            "branches": ["w1", "w2"],
            "reducer": "red",
            "max_reason_calls": 2,
            "min_branches": 1,
        },
        # Both no-deliberation conditions: noop advertises the same prompt and
        # tool but returns a fixed neutral result, off never injects at all.
        "vm-noop": {"controller": "ctrl", "reason_mode": "noop"},
        "vm-off": {"controller": "ctrl", "reason_mode": "off"},
    },
}

REQ: dict[str, Any] = {
    "model": "vm",
    "messages": [{"role": "user", "content": "Fix the path-normalization cache bug."}],
    "tools": [{"type": "function", "function": {"name": "shell", "parameters": {}}}],
}

# The reducer answers with the checkpoint schema; the engine renders the public
# block from these fields, so the payload itself must never reach the wire.
REDUCED = json.dumps({
    "conclusion": "inconsistent canonicalization",
    "next_action": "read the cache key builder",
})


def script() -> dict[str, list[Completion]]:
    """One deliberated turn: reason(), two continuations, a checkpoint, an action."""
    return {
        "ctrl": [
            ok("ctrl", "Broadening the diagnosis.", [reason_call()], "tool_calls"),
            ok("ctrl", "Inspecting the cache key.", [shell_call("rg -n cache_key")],
               "tool_calls"),
        ],
        "w1": [ok("w1", "Normalization is applied at lookup but not insertion.")],
        "w2": [ok("w2", "A global cache leaking across fixtures explains it too.")],
        "red": [ok("red", REDUCED)],
    }


@contextlib.contextmanager
def serving(tmp_path, monkeypatch, *, script_=None, engine=None, api_key: str = "",
            upstream=None):
    """Boot the app through its real lifespan, then swap in a scripted upstream."""
    trace = tmp_path / "trace.jsonl"
    cfg_path = tmp_path / "test.yaml"
    cfg_path.write_text(yaml.safe_dump(CONFIG | {"trace_path": str(trace)}))
    monkeypatch.setenv("REASONPROXY_CONFIG", str(cfg_path))
    if api_key:
        monkeypatch.setenv("REASONPROXY_API_KEY", api_key)
    else:
        monkeypatch.delenv("REASONPROXY_API_KEY", raising=False)

    stub = upstream if upstream is not None else Stub(script_ or script())
    with TestClient(app_module.app) as c:
        cfg = app_module.STATE["cfg"]
        assert str(cfg.trace_path) == str(trace)  # lifespan really read our config
        app_module.STATE.update(
            upstream=stub,  # type: ignore[arg-type]
            engine=engine if engine is not None else Engine(cfg, stub),  # type: ignore[arg-type]
        )
        c.stub = stub  # type: ignore[attr-defined]
        c.trace_file = trace  # type: ignore[attr-defined]
        yield c


@pytest.fixture
def client(tmp_path, monkeypatch):
    with serving(tmp_path, monkeypatch) as c:
        yield c


def records(c) -> list[dict[str, Any]]:
    if not c.trace_file.exists():
        return []
    return [json.loads(line) for line in c.trace_file.read_text().splitlines()]


def sse(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


class FakeEngine:
    """The engine seam, for cases where the interesting thing is what HTTP does
    with a result rather than how deliberation produced it."""

    def __init__(self, *, result: dict[str, Any] | None = None,
                 raises: BaseException | None = None) -> None:
        self.result = result
        self.raises = raises
        self.seen: dict[str, Any] | None = None

    async def complete(self, req, vm_name, *, trace=None):
        self.seen = req
        if trace is not None:
            trace.update(id="rp-fake-0001", config_sha="sha-test", usage=[],
                         outcome="pending")
        if self.raises is not None:
            raise self.raises
        return self.result, (trace if trace is not None else {})


class HangingEngine:
    """Deliberates forever, and records whether its cleanup actually ran."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def complete(self, req, vm_name, *, trace=None):
        if trace is not None:
            trace.update(id="rp-hang-0001", config_sha="sha-test", usage=[],
                         outcome="pending")
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0)  # only completes if the app awaits the cancel
            self.cancelled = True
            raise
        raise AssertionError("a hung deliberation must be cancelled, not finish")

    async def hangup(self) -> dict[str, str]:
        await self.started.wait()
        return {"type": "http.disconnect"}


def completion(content: str = "done") -> dict[str, Any]:
    return {
        "id": "chatcmpl-rp-test",
        "object": "chat.completion",
        "created": 1,
        "model": "vm",
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


async def asgi_post(payload: dict[str, Any], *, next_event=None,
                    headers: tuple[tuple[bytes, bytes], ...] = ()) -> list[dict[str, Any]]:
    """Drive the ASGI app directly. TestClient cannot express a caller that
    disappears mid-request, which is exactly what has to be observable."""
    body = json.dumps(payload).encode()
    queue: list[dict[str, Any]] = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if queue:
            return queue.pop(0)
        if next_event is not None:
            return await next_event()
        await asyncio.Event().wait()  # a connection that simply stays open
        raise AssertionError("unreachable")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    *headers],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    await app_module.app(scope, receive, send)
    return sent


def http_of(sent: list[dict[str, Any]]) -> tuple[int, Any]:
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body) if body else None


# --------------------------------------------------------------------------- #
# surface
# --------------------------------------------------------------------------- #

def test_models_endpoint_lists_every_condition(client):
    body = client.get("/v1/models").json()
    assert body["object"] == "list"
    assert {m["id"] for m in body["data"]} == {"vm", "vm-noop", "vm-off"}


def test_health_is_liveness_only(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    # an unauthenticated prober learns neither the roster nor the condition hash
    assert app_module.STATE["cfg"].sha not in r.text
    assert "vm" not in r.text


def test_unknown_model_is_an_openai_shaped_error(client):
    r = client.post("/v1/chat/completions", json={"model": "nope", "messages": [
        {"role": "user", "content": "go"}]})
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "reasonproxy_model_not_found"


def test_deliberated_turn_returns_a_normal_chat_completion(client):
    r = client.post("/v1/chat/completions", json=REQ)
    assert r.status_code == 200
    body = r.json()

    assert body["object"] == "chat.completion"
    assert body["model"] == "vm"
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"

    # the real action survived; the private tool did not
    names = [tc["function"]["name"] for tc in choice["message"]["tool_calls"]]
    assert names == ["shell"]
    assert REASON_WIRE_NAME not in r.text

    # the checkpoint rode out in ordinary assistant content, rendered from the
    # reducer's fields -- the reducer's own JSON payload never reaches a caller
    content = choice["message"]["content"]
    assert content.startswith(f"<{TAG}")
    assert "inconsistent canonicalization" in content
    assert '"conclusion"' not in r.text
    assert "Inspecting the cache key." in content

    assert r.headers["x-reasonproxy-reason-calls"] == "1"
    assert r.headers["x-reasonproxy-config-sha"] == app_module.STATE["cfg"].sha


# --------------------------------------------------------------------------- #
# caller protocol: a status, never a traceback
# --------------------------------------------------------------------------- #

PROTOCOL_ERRORS: list[tuple[Any, str]] = [
    ([{"model": "vm"}], "reasonproxy_invalid_body"),
    ({"messages": [{"role": "user", "content": "hi"}]}, "reasonproxy_invalid_model"),
    ({"model": "vm"}, "reasonproxy_invalid_messages"),
    ({"model": "vm", "messages": []}, "reasonproxy_invalid_messages"),
    ({"model": "vm", "messages": ["just a string"]}, "reasonproxy_invalid_messages"),
    ({"model": "vm", "messages": [{"role": "user"}], "n": 2}, "reasonproxy_unsupported_n"),
    ({"model": "vm", "messages": [{"role": "user"}], "tools": {"shell": {}}},
     "reasonproxy_invalid_tools"),
    ({"model": "vm", "messages": [{"role": "user"}], "stream": "yes"},
     "reasonproxy_invalid_stream"),
    ({"model": "vm", "messages": [{"role": "user"}], "stream_options": {"include_usage": True}},
     "reasonproxy_invalid_stream_options"),
]


@pytest.mark.parametrize("payload,expected", PROTOCOL_ERRORS)
def test_caller_protocol_errors_are_400_and_never_reach_the_engine(client, payload, expected):
    r = client.post("/v1/chat/completions", json=payload)
    assert r.status_code == 400
    assert r.json()["error"]["type"] == expected
    assert client.stub.calls == []  # no upstream call was made
    assert records(client) == []  # nothing ran, so there is nothing to trace


def test_a_malformed_json_body_is_a_400(client):
    r = client.post("/v1/chat/completions", content=b'{"model": "vm", "messages"',
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "reasonproxy_invalid_json"
    assert client.stub.calls == []


def test_the_bearer_gate_guards_v1_and_never_echoes_the_key(tmp_path, monkeypatch):
    key = "rp-secret-token-value"
    with serving(tmp_path, monkeypatch, api_key=key) as c:
        anon = c.get("/v1/models")
        assert anon.status_code == 401
        assert anon.json()["error"]["type"] == "reasonproxy_unauthorized"
        assert key not in anon.text

        assert c.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/v1/models", headers={"Authorization": key}).status_code == 401  # no scheme
        assert c.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code == 200

        blocked = c.post("/v1/chat/completions", json=REQ)
        assert blocked.status_code == 401
        assert c.stub.calls == []  # rejected before any provider call
        assert records(c) == []  # and a stranger's request is not traced

        allowed = c.post("/v1/chat/completions", json=REQ,
                         headers={"Authorization": f"Bearer {key}"})
        assert allowed.status_code == 200
        assert key not in allowed.text
        # liveness stays reachable for an orchestrator that holds no key
        assert c.get("/healthz").json() == {"ok": True}


# --------------------------------------------------------------------------- #
# failure classification and trace completeness
# --------------------------------------------------------------------------- #

def test_upstream_failure_is_502_with_a_complete_trace(tmp_path, monkeypatch):
    broken = {"ctrl": [Completion.failed("ctrl", "controller-1", "error",
                                         "HTTP 500: upstream is sad", 7)]}
    with serving(tmp_path, monkeypatch, script_=broken) as c:
        r = c.post("/v1/chat/completions", json=REQ,
                   headers={"X-Session-ID": "trial-7f3a"})
        assert r.status_code == 502
        assert r.json()["error"]["type"] == "reasonproxy_controller_failed"
        assert "upstream is sad" in r.json()["error"]["message"]

        [t] = records(c)
        assert t["outcome"] == "error" and t["stage"] == "controller"
        assert t["id"] == r.headers["x-reasonproxy-trace-id"]
        assert t["virtual_model"] == "vm" and t["config_sha"]
        assert t["usage"]  # the failed controller call is still accounted for
        assert "wall_s" in t
        assert t["session_id"] == "trial-7f3a"  # a failed turn still joins


def test_the_engines_status_code_is_honoured(tmp_path, monkeypatch):
    exc = UpstreamError("request", "tool name 'reason' is reserved by the proxy",
                        trace={"id": "rp-protocol", "config_sha": "sha-test"},
                        status_code=400)
    with serving(tmp_path, monkeypatch, engine=FakeEngine(raises=exc)) as c:
        r = c.post("/v1/chat/completions", json=REQ)
        assert r.status_code == 400  # a caller protocol error, not a bad gateway
        assert r.json()["error"]["type"] == "reasonproxy_request_failed"
        assert r.headers["x-reasonproxy-trace-id"] == "rp-protocol"

        [t] = records(c)
        assert t["id"] == "rp-protocol" and t["outcome"] == "error"
        assert t["detail"].startswith("tool name")


def test_an_unexpected_internal_error_is_generic_and_traced(tmp_path, monkeypatch):
    boom = RuntimeError("Bearer sk-live-DEADBEEF rejected by provider")
    with serving(tmp_path, monkeypatch, engine=FakeEngine(raises=boom)) as c:
        r = c.post("/v1/chat/completions", json=REQ)
        assert r.status_code == 500
        assert r.json()["error"]["type"] == "reasonproxy_internal_error"
        assert "RuntimeError" in r.json()["error"]["message"]  # classification only
        assert "sk-live-DEADBEEF" not in r.text  # never the payload

        [t] = records(c)
        assert t["outcome"] == "internal_error" and t["error_type"] == "RuntimeError"
        assert t["id"] == "rp-fake-0001"


def test_the_trace_record_is_appended_once_and_matches_the_response(client):
    r = client.post("/v1/chat/completions", json=REQ,
                    headers={"X-Session-ID": "trial-7f3a"})
    [t] = records(client)

    assert t["id"] == r.headers["x-reasonproxy-trace-id"]
    assert t["virtual_model"] == "vm" and t["outcome"] == "ok"
    assert t["reason_calls"] == 1 and t["stream"] is False
    assert t["config_sha"] == app_module.STATE["cfg"].sha
    assert isinstance(t["wall_s"], float) and "ts" in t
    assert t["session_id"] == "trial-7f3a"  # joins the harness trial span
    # component accounting survives the round trip, per role
    assert {row["role"] for row in t["usage"]} >= {"controller", "branch", "reducer"}


def test_a_missing_session_header_is_recorded_as_null(client):
    client.post("/v1/chat/completions", json=REQ)
    [t] = records(client)
    assert t["session_id"] is None  # never invented, and never absent from the schema


# --------------------------------------------------------------------------- #
# streaming replay
# --------------------------------------------------------------------------- #

def test_streaming_replay_carries_the_action_and_not_the_private_call(client):
    r = client.post("/v1/chat/completions", json=REQ | {"stream": True})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text.rstrip().endswith("data: [DONE]")
    assert REASON_WIRE_NAME not in r.text

    chunks = sse(r.text)
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert f"<{TAG}" in text and "Inspecting the cache key." in text

    deltas = [d for c in chunks for d in c["choices"][0]["delta"].get("tool_calls", [])]
    assert len(deltas) == 1
    call = deltas[0]
    assert call["index"] == 0 and call["id"] == "call_s1" and call["type"] == "function"
    assert call["function"] == {"name": "shell", "arguments": '{"cmd": "rg -n cache_key"}'}

    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert all("usage" not in c for c in chunks)  # not requested, not invented


def test_streaming_usage_chunk_appears_only_when_requested(client):
    r = client.post("/v1/chat/completions", json=REQ | {
        "stream": True, "stream_options": {"include_usage": True}})
    chunks = sse(r.text)

    tail = [c for c in chunks if not c["choices"]]
    assert len(tail) == 1 and chunks[-1] == tail[0]  # last chunk before [DONE]
    usage = tail[0]["usage"]
    assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    # composite per-component accounting rides along, as in the buffered response
    assert tail[0]["reasonproxy_usage"]["components"]
    assert all(c["usage"] is None for c in chunks if c["choices"])


# --------------------------------------------------------------------------- #
# request fidelity and disconnect
# --------------------------------------------------------------------------- #

def test_the_caller_request_reaches_the_engine_unmodified(tmp_path, monkeypatch):
    engine = FakeEngine(result=completion())
    body = {
        "model": "vm",
        "messages": [{"role": "user", "content": "go"}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0.3,
        "seed": 7,
        "response_format": {"type": "json_object"},
        "metadata": {"task": "ytt-jsonpath-query-api"},
    }
    with serving(tmp_path, monkeypatch, engine=engine) as c:
        r = c.post("/v1/chat/completions", json=body)
    assert r.status_code == 200
    # HTTP strips nothing and rewrites nothing: which keys are proxy-owned is
    # the engine's decision, and the trace must describe the request that came in
    assert engine.seen == body


async def test_a_client_hangup_cancels_the_deliberation(tmp_path, monkeypatch):
    engine = HangingEngine()
    with serving(tmp_path, monkeypatch, engine=engine) as c:
        sent = await asgi_post(REQ, next_event=engine.hangup,
                               headers=((b"x-session-id", b"trial-7f3a"),))
        status, body = http_of(sent)

        assert status == 499
        assert body["error"]["type"] == "reasonproxy_client_disconnect"
        # cancelled *and* awaited: the fan-out cannot keep spending unobserved
        assert engine.cancelled

        [t] = records(c)
        assert t["outcome"] == "client_disconnect"
        assert t["id"] == "rp-hang-0001" and t["config_sha"] == "sha-test"
        assert "wall_s" in t
        assert t["session_id"] == "trial-7f3a"  # a cancelled turn still joins


# --------------------------------------------------------------------------- #
# conversation routing: one session per caller turn
# --------------------------------------------------------------------------- #

PROVIDER_KEY = "sk-provider-only-secret"
CALLER_KEY = "rp-caller-bearer-token"

# The same conditions, on an endpoint that routes by conversation and rejects a
# request naming none.
ZEN_CONFIG: dict[str, Any] = CONFIG | {
    "providers": {
        "fake": {"base_url": "https://opencode.ai/zen/v1", "api_key_env": "FAKE_KEY"}
    }
}


class SessionStub:
    """The upstream seam, recording the conversation each call ran under.

    Replies are decided from the messages of the call itself instead of from a
    shared script, so any number of turns can be in flight at once and each
    still gets its reason() call, its two continuations and its checkpoint.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(self, backend, messages, *, tools=None, tool_choice=None,
                       overrides=None) -> Completion:
        self.calls.append({
            "backend": backend,
            "session": current_session(),
            "text": json.dumps(messages, default=str),
        })
        await asyncio.sleep(0)  # a concurrent turn gets to interleave here
        if backend == "red":
            return ok("red", REDUCED)
        if backend != "ctrl":
            return ok(backend, f"{backend} continuation.")
        # The controller's second turn already has the deliberation result in
        # its history, so it acts instead of asking again.
        if any(m.get("role") == "tool" for m in messages):
            return ok("ctrl", "Inspecting the cache key.", [shell_call("rg -n cache_key")],
                      "tool_calls")
        return ok("ctrl", "Broadening the diagnosis.", [reason_call()], "tool_calls")


def provider_turn(model: str, content: str, tool_calls: list[dict] | None = None) -> dict:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{model}",
        "model": model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
    }


def zen_provider(seen: list[httpx.Request]):
    """A Zen-shaped endpoint that keeps every request it is sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        model = body["model"]
        if model == "controller-1":
            if any(m.get("role") == "tool" for m in body["messages"]):
                return httpx.Response(200, json=provider_turn(
                    model, "Inspecting the cache key.", [shell_call("rg -n cache_key")]))
            return httpx.Response(200, json=provider_turn(
                model, "Broadening the diagnosis.", [reason_call()]))
        if model == "reducer-1":
            return httpx.Response(200, json=provider_turn(model, REDUCED))
        return httpx.Response(200, json=provider_turn(model, f"{model} continuation."))

    return handler


@contextlib.contextmanager
def serving_live(tmp_path, monkeypatch, handler, *, config: dict, api_key: str = ""):
    """Boot the app with its own real transport, on a mock socket.

    Nothing is swapped in here: the lifespan builds the Upstream itself, so
    what the provider receives is what a real run would send it.
    """
    trace = tmp_path / "trace.jsonl"
    cfg_path = tmp_path / "live.yaml"
    cfg_path.write_text(yaml.safe_dump(config | {"trace_path": str(trace)}))
    monkeypatch.setenv("REASONPROXY_CONFIG", str(cfg_path))
    monkeypatch.setenv("FAKE_KEY", PROVIDER_KEY)
    if api_key:
        monkeypatch.setenv("REASONPROXY_API_KEY", api_key)
    else:
        monkeypatch.delenv("REASONPROXY_API_KEY", raising=False)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    with TestClient(app_module.app) as c:
        c.trace_file = trace  # type: ignore[attr-defined]
        yield c


def test_every_request_of_a_zen_turn_names_the_callers_conversation(tmp_path, monkeypatch):
    """The routing contract end to end on the real transport: each provider
    request of one turn -- both controller turns, both continuations and the
    reducer -- names the caller's conversation, says truthfully who we are, and
    authenticates with the provider's key rather than the caller's bearer."""
    seen: list[httpx.Request] = []
    with serving_live(tmp_path, monkeypatch, zen_provider(seen),
                      config=ZEN_CONFIG, api_key=CALLER_KEY) as c:
        r = c.post("/v1/chat/completions", json=REQ, headers={
            "Authorization": f"Bearer {CALLER_KEY}",
            "x-opencode-session": "ses_native_7f3a",
        })

        assert r.status_code == 200
        assert sorted(json.loads(q.content)["model"] for q in seen) == [
            "controller-1", "controller-1", "reducer-1", "worker-1", "worker-2",
        ]
        for q in seen:
            assert q.headers["x-opencode-session"] == "ses_native_7f3a"
            assert q.headers["user-agent"] == "ReasonProxy/0.1"
            assert q.headers["authorization"] == f"Bearer {PROVIDER_KEY}"
            # The caller's own credential is consumed at the gate, never
            # forwarded and never echoed into a provider request.
            assert CALLER_KEY not in str(dict(q.headers)) + q.content.decode()

        [t] = records(c)
        assert t["session_id"] == "ses_native_7f3a"


async def test_two_concurrent_turns_never_share_a_conversation(tmp_path, monkeypatch):
    """Trials run in parallel through one proxy, so a session kept anywhere but
    the request would attach one trial's conversation to another's calls."""
    stub = SessionStub()
    other = REQ | {"messages": [{"role": "user", "content": "Fix the flaky retry backoff."}]}
    with serving(tmp_path, monkeypatch, upstream=stub):
        alpha, beta = await asyncio.gather(
            asgi_post(REQ, headers=((b"x-opencode-session", b"ses_alpha"),)),
            asgi_post(other, headers=((b"x-opencode-session", b"ses_beta"),)),
        )

    assert http_of(alpha)[0] == 200 and http_of(beta)[0] == 200
    assert {call["session"] for call in stub.calls} == {"ses_alpha", "ses_beta"}
    for session, task in (("ses_alpha", "path-normalization"),
                          ("ses_beta", "flaky retry backoff")):
        calls = [call for call in stub.calls if call["session"] == session]
        # Both controller turns, both branches and the reducer: an auxiliary
        # call inherits the conversation instead of losing or swapping it.
        assert sorted(call["backend"] for call in calls) == ["ctrl", "ctrl", "red", "w1", "w2"]
        assert all(task in call["text"] for call in calls)


async def test_the_conversation_binding_does_not_outlive_the_turn(tmp_path, monkeypatch):
    """Driven inside this task, so a binding that is never reset leaks into it
    -- which on a worker means the next caller's calls route as this one."""
    stub = SessionStub()
    with serving(tmp_path, monkeypatch, upstream=stub) as c:
        status, _ = http_of(await asgi_post(REQ, headers=((b"x-session-id", b"trial-7f3a"),)))

        assert status == 200
        assert stub.calls and all(call["session"] == "trial-7f3a" for call in stub.calls)
        assert current_session() == ""
        [t] = records(c)
        assert t["session_id"] == "trial-7f3a"


def test_a_caller_that_declares_no_session_still_routes_one_conversation(tmp_path, monkeypatch):
    """mini-swe-agent sends no session header of its own and Zen rejects a
    request that names none, so the proxy derives one: the same for every step
    of one trajectory, different between trajectories, and never written into
    the trace as though the caller had declared it."""
    stub = SessionStub()
    step_two = REQ | {"messages": [
        *REQ["messages"],
        {"role": "assistant", "content": "Looking at the cache key."},
        {"role": "user", "content": "exit code 1"},
    ]}
    other = REQ | {"messages": [{"role": "user", "content": "Fix the flaky retry backoff."}]}
    with serving(tmp_path, monkeypatch, upstream=stub) as c:
        for payload in (REQ, step_two, other):
            assert c.post("/v1/chat/completions", json=payload).status_code == 200

        trajectory = {call["session"] for call in stub.calls
                      if "path-normalization" in call["text"]}
        unrelated = {call["session"] for call in stub.calls
                     if "flaky retry backoff" in call["text"]}
        assert len(trajectory) == 1  # step one and step two are one conversation
        assert len(unrelated) == 1 and unrelated != trajectory
        assert all(call["session"].startswith("rp-") for call in stub.calls)
        assert all("path-normalization" not in call["session"] for call in stub.calls)
        # What the caller declared is still the only thing the record claims.
        assert [t["session_id"] for t in records(c)] == [None, None, None]


async def test_a_session_id_that_cannot_be_a_header_does_not_lose_the_turn(tmp_path, monkeypatch):
    """Header bytes are the caller's to choose and reach us latin-1 decoded, so
    passing one straight through to a provider header would fail inside the
    HTTP client and turn a working trial into a 500. The turn routes on the
    derived id instead, and the record still reports what the caller sent."""
    stub = SessionStub()
    with serving(tmp_path, monkeypatch, upstream=stub) as c:
        status, _ = http_of(await asgi_post(
            REQ, headers=((b"x-session-id", "trial-\xe9".encode("latin-1")),)))

        assert status == 200
        assert stub.calls and all(call["session"].startswith("rp-") for call in stub.calls)
        [t] = records(c)
        assert t["session_id"] == "trial-\xe9"
