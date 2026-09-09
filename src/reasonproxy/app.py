"""OpenAI-compatible HTTP surface. Point any client's base_url here.

The surface is deliberately thin: validate the caller's protocol, run one
deliberated completion, replay it, append one trace line. Everything that can
fail interestingly fails inside the engine, so this module's only jobs are to
classify failures honestly (400 caller / 502 upstream / 500 us) and to make
sure nothing private -- the reason tool, a provider payload, an API key -- ever
reaches the wire.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.requests import ClientDisconnect

from .config import Config
from .engine import Engine, UpstreamError
from .upstream import Upstream, routing_session, session_scope

DEFAULT_CONFIG = "configs/research.yaml"
CLIENT_CLOSED = 499  # nginx's "client closed request": nobody is listening

log = logging.getLogger("reasonproxy")
STATE: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = Config.load(os.environ.get("REASONPROXY_CONFIG", DEFAULT_CONFIG))
    upstream = Upstream(cfg)
    # The bearer key is read once here and compared in constant time; it is
    # never echoed into a response, a header, a trace record or a log line.
    STATE.update(
        cfg=cfg,
        upstream=upstream,
        engine=Engine(cfg, upstream),
        api_key=os.environ.get("REASONPROXY_API_KEY", ""),
    )
    if cfg.trace_path:
        Path(cfg.trace_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        # Drop the state first so a failing close cannot leave a half-torn-down
        # proxy answering requests, then close the sockets we opened (tests
        # swap STATE["upstream"] for a stub, which is not ours to close).
        STATE.clear()
        await upstream.aclose()


app = FastAPI(title="ReasonProxy", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    """Liveness only. The roster and the config hash are experiment metadata,
    not something an unauthenticated prober gets to enumerate."""
    return {"ok": True}


@app.get("/v1/models")
async def models(request: Request) -> Any:
    if (denied := _denied(request)) is not None:
        return denied
    cfg: Config = STATE["cfg"]
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "owned_by": "reasonproxy", "created": 0}
            for name in sorted(cfg.virtual_models)
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    if (denied := _denied(request)) is not None:
        return denied
    cfg: Config = STATE["cfg"]
    engine: Engine = STATE["engine"]

    try:
        body = await request.json()
    except ClientDisconnect:
        return _error(CLIENT_CLOSED, "reasonproxy_client_disconnect",
                      "caller disconnected while sending the request")
    except ValueError:
        return _error(400, "reasonproxy_invalid_json", "request body is not valid JSON")
    if (invalid := _validate(body, cfg)) is not None:
        return invalid

    name: str = body["model"]
    stream: bool = bool(body.get("stream"))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    # What the caller declared this conversation to be, in either spelling a
    # client may use: the harness stamps one id per trial (mini-swe-agent sends
    # X-Session-ID through extra_headers) and an OpenCode-aware client sends
    # x-opencode-session. It is read from the header and never invented, so a
    # record says exactly what the caller said, and the value leaves this proxy
    # as a provider header only -- never as a request body field.
    session: str | None = (
        request.headers.get("x-opencode-session")
        or request.headers.get("x-session-id")
        or ""
    ).strip() or None
    # The conversation every provider call of this turn routes under. Zen
    # routes and caches by session and refuses a request that carries none
    # (https://opencode.ai/docs/go/#where-can-i-use-it), so a turn always has
    # one: the caller's own id when it sent a usable one, otherwise a digest of
    # the opening of this trajectory, which every step of one task repeats.
    routing = routing_session(session, body["messages"])

    # `body` is handed to the engine exactly as it arrived: the engine owns
    # which parameters are proxy-owned (model/messages/tools/stream/
    # stream_options/n) and everything else is the caller's sampling contract.
    # Nothing here mutates it, so the record and the request agree.
    trace: dict[str, Any] = {"virtual_model": name, "session_id": session}
    t0 = time.perf_counter()
    # Bound around task creation only: the engine task and every branch task it
    # spawns inherit the conversation, concurrent turns each keep their own,
    # and the binding does not outlive this handler.
    with session_scope(routing):
        task = asyncio.create_task(engine.complete(body, name, trace=trace))

    try:
        finished = await _race_disconnect(request, task)
    except Exception as exc:  # only a broken ASGI channel gets here
        return await _fail(cfg, _finish(trace, name, t0, "internal_error", stream, session),
                           500, "reasonproxy_internal_error",
                           f"internal error ({type(exc).__name__})", exc)
    if not finished:
        # The caller hung up mid-deliberation and the fan-out has been
        # cancelled and awaited, so no branch keeps burning tokens unobserved.
        return await _fail(cfg, _finish(trace, name, t0, "client_disconnect", stream, session),
                           CLIENT_CLOSED, "reasonproxy_client_disconnect",
                           "caller disconnected; deliberation cancelled")

    try:
        response, trace = task.result()
    except UpstreamError as exc:
        record = _finish(exc.trace or trace, name, t0, "error", stream, session)
        record.setdefault("stage", exc.stage)
        record.setdefault("detail", exc.detail)
        return await _fail(cfg, record, exc.status_code,
                           f"reasonproxy_{exc.stage}_failed", exc.detail)
    except Exception as exc:
        return await _fail(cfg, _finish(trace, name, t0, "internal_error", stream, session),
                           500, "reasonproxy_internal_error",
                           f"internal error ({type(exc).__name__})", exc)

    _finish(trace, name, t0, "ok", stream, session)
    await _write_trace(cfg, trace)
    headers = _headers(cfg, trace)
    if stream:
        return StreamingResponse(_replay_sse(response, include_usage),
                                 media_type="text/event-stream", headers=headers)
    return JSONResponse(response, headers=headers)


# --------------------------------------------------------------------------- #
# caller protocol
# --------------------------------------------------------------------------- #

def _denied(request: Request) -> JSONResponse | None:
    """Optional bearer gate for /v1/*.

    With REASONPROXY_API_KEY unset the proxy is open, which is what a
    localhost benchmark wants; when it is set, every /v1 route requires it and
    the comparison is constant-time so the socket cannot be used as an oracle.
    """
    expected: str = STATE.get("api_key") or ""
    if not expected:
        return None
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and hmac.compare_digest(
        token.strip().encode(), expected.encode()
    ):
        return None
    return _error(401, "reasonproxy_unauthorized",
                  "missing or invalid bearer token for /v1")


def _validate(body: Any, cfg: Config) -> JSONResponse | None:
    """Reject caller protocol errors with a status, never a traceback.

    The engine assumes message dicts and a single completion; anything it
    would trip over is the caller's mistake and is named as such here.
    """
    if not isinstance(body, dict):
        return _error(400, "reasonproxy_invalid_body", "request body must be a JSON object")

    name = body.get("model")
    if not isinstance(name, str) or not name:
        return _error(400, "reasonproxy_invalid_model", "'model' must be a model id string")
    if name not in cfg.virtual_models:
        return _error(404, "reasonproxy_model_not_found",
                      f"unknown model {name!r}; see GET /v1/models")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _error(400, "reasonproxy_invalid_messages",
                      "'messages' must be a non-empty array")
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or not isinstance(m.get("role"), str):
            return _error(400, "reasonproxy_invalid_messages",
                          f"messages[{i}] must be an object with a string 'role'")

    if body.get("n") not in (None, 1):
        return _error(400, "reasonproxy_unsupported_n",
                      "n>1 is not supported: one deliberated completion per request")

    tools = body.get("tools")
    if tools is not None and not (
        isinstance(tools, list) and all(isinstance(t, dict) for t in tools)
    ):
        return _error(400, "reasonproxy_invalid_tools",
                      "'tools' must be an array of tool objects")

    stream = body.get("stream")
    if stream is not None and not isinstance(stream, bool):
        return _error(400, "reasonproxy_invalid_stream", "'stream' must be a boolean")

    opts = body.get("stream_options")
    if opts is not None:
        if not isinstance(opts, dict):
            return _error(400, "reasonproxy_invalid_stream_options",
                          "'stream_options' must be an object")
        if not stream:
            return _error(400, "reasonproxy_invalid_stream_options",
                          "'stream_options' requires stream=true")
    return None


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #

async def _race_disconnect(request: Request, task: asyncio.Task) -> bool:
    """Await the engine, or the caller hanging up -- whichever comes first.

    Deliberation is buffered (a turn may end in a private reason() call, so
    controller tokens cannot be forwarded live), which means a disconnect is
    invisible unless somebody watches for it. Returns True if the engine
    finished; otherwise the engine task has been cancelled *and awaited*, so
    the K-way fan-out is really over before we answer.
    """
    watch = asyncio.create_task(_watch_disconnect(request))
    try:
        await asyncio.wait({task, watch}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        # Our own request was cancelled (server shutdown, an outer deadline):
        # the fan-out must not outlive the turn that asked for it.
        await _stop(task)
        raise
    finally:
        await _stop(watch)
    if task.done():
        return True
    await _stop(task)
    return False


async def _watch_disconnect(request: Request) -> None:
    """Resolve when the caller goes away.

    Event-driven rather than polled: the body is already buffered, so the only
    ASGI message left on this connection is http.disconnect. A receive channel
    that breaks instead of reporting is the same event.
    """
    try:
        while (await request.receive()).get("type") != "http.disconnect":
            pass
    except Exception:
        return


async def _stop(task: asyncio.Task) -> None:
    """Cancel and reap. The outcome is already decided, so a dying task's
    exception is noise -- but it must be retrieved, and its cleanup awaited."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


# --------------------------------------------------------------------------- #
# responses
# --------------------------------------------------------------------------- #

async def _replay_sse(response: dict[str, Any], include_usage: bool):
    """Buffer-then-SSE.

    The proxy cannot forward controller tokens live because a completion may
    terminate in a private reason() call that must not escape, so the resolved
    turn is replayed. Every field the engine composed is replayed as-is,
    including native tool calls and provider-specific message fields.
    """
    choice = response["choices"][0]
    msg = dict(choice.get("message") or {})
    base: dict[str, Any] = {
        "id": response["id"], "object": "chat.completion.chunk",
        "created": response["created"], "model": response["model"],
    }
    if include_usage:
        base["usage"] = None  # OpenAI: null on every chunk but the usage chunk

    def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
        payload = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield chunk({"role": msg.pop("role", None) or "assistant"})
    content = msg.pop("content", None)
    tool_calls = msg.pop("tool_calls", None) or []
    if content:
        yield chunk({"content": content})
    for i, tc in enumerate(tool_calls):
        yield chunk({"tool_calls": [{"index": i, **tc}]})
    if msg:  # refusal, annotations, reasoning fields: replayed in their own keys
        yield chunk(msg)
    yield chunk({}, choice.get("finish_reason") or "stop")
    if include_usage:
        usage_chunk: dict[str, Any] = {**base, "choices": [], "usage": response.get("usage")}
        if "reasonproxy_usage" in response:
            usage_chunk["reasonproxy_usage"] = response["reasonproxy_usage"]
        yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def _headers(cfg: Config, trace: dict[str, Any]) -> dict[str, str]:
    return {
        "X-ReasonProxy-Trace-Id": str(trace.get("id", "")),
        "X-ReasonProxy-Reason-Calls": str(trace.get("reason_calls", 0)),
        "X-ReasonProxy-Config-Sha": cfg.sha,
        "X-ReasonProxy-Persistence": str(trace.get("persistence", "")),
    }


def _error(status: int, code: str, message: str, trace_id: str | None = None) -> JSONResponse:
    """OpenAI-shaped error. `message` is proxy-authored text or an upstream
    detail the transport already redacted -- never a traceback or a payload."""
    return JSONResponse(
        {"error": {"message": message, "type": code, "code": code}},
        status_code=status,
        headers={"X-ReasonProxy-Trace-Id": trace_id} if trace_id else None,
    )


async def _fail(
    cfg: Config,
    record: dict[str, Any],
    status: int,
    code: str,
    message: str,
    exc: BaseException | None = None,
) -> JSONResponse:
    """One exit for every unhappy path: the trace lands before the caller is
    told, and an unexpected exception is logged here and nowhere else."""
    if exc is not None:
        record["error_type"] = type(exc).__name__
        log.exception("unhandled error serving %s", record.get("virtual_model"),
                      exc_info=exc)
    await _write_trace(cfg, record)
    return _error(status, code, message, record.get("id"))


# --------------------------------------------------------------------------- #
# trace persistence
# --------------------------------------------------------------------------- #

def _finish(
    trace: dict[str, Any], name: str, t0: float, outcome: str, stream: bool,
    session: str | None,
) -> dict[str, Any]:
    """Stamp the HTTP-level verdict onto whatever the engine recorded, so
    success, upstream failure and cancellation all carry the same joinable
    schema and no record is ever published still saying "pending". Degradation
    is the engine's own field, not a softer outcome. `session_id` is None when
    the caller declared no session in either header spelling; the id this turn
    actually routed under is never invented into that field."""
    trace.setdefault("virtual_model", name)
    trace["outcome"] = outcome
    trace["session_id"] = session
    trace["wall_s"] = round(time.perf_counter() - t0, 3)
    trace["stream"] = stream
    return trace


async def _write_trace(cfg: Config, record: dict[str, Any]) -> None:
    """Append one complete JSON line.

    A research trace carries whole branch transcripts, so encoding happens off
    the event loop, and one O_APPEND write keeps concurrent proxy workers from
    interleaving records. Nothing is dropped or truncated: a trace that lies
    about what ran is worse than no trace.
    """
    if not cfg.trace_path or not record:
        return
    record.setdefault("ts", time.time())
    try:
        await asyncio.to_thread(_append_line, cfg.trace_path, record)
    except OSError as exc:
        log.warning("trace append failed (%s): %s", cfg.trace_path, exc)


def _append_line(path: str, record: dict[str, Any]) -> None:
    data = (json.dumps(record, default=str, ensure_ascii=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(prog="reasonproxy")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    ap.add_argument("--host", default="127.0.0.1")
    # 8100 is the pinned endpoint every consumer assumes: the benchmark
    # preflight, the conformance fixture, and hosted_vllm/<alias> inside the
    # sandbox (http://host.docker.internal:8100/v1).
    ap.add_argument("-p", "--port", type=int, default=8100)
    args = ap.parse_args()
    Config.load(args.config)  # fail fast on bad config
    os.environ["REASONPROXY_CONFIG"] = args.config
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
