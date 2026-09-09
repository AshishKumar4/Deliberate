#!/usr/bin/env python3
"""A local OpenAI-compatible server that plays controller / workers / reducer.

Lets the whole proxy be exercised over real sockets with no provider spend, and
unlike an in-process stub it also exercises Upstream's HTTP path, rate gate and
retry logic.

The reducer answers with the engine's checkpoint JSON schema (`conclusion` and
`next_action` are required non-empty strings; `evidence`, `alternatives`,
`verification` and `carry_forward` are lists), because the engine asks the
reducer for `response_format: json_object` and validates the object before it
renders and publishes a checkpoint. Returning prose here would silently make
every local smoke exercise the degraded-reduction path instead of a successful
collapse.

    python scripts/fake_upstream.py --port 9099
    REASONPROXY_CONFIG=configs/local-smoke.yaml python -m reasonproxy.app
"""

from __future__ import annotations

import argparse
import json
import time
import uuid

from fastapi import FastAPI, Request

app = FastAPI()

CHECKPOINT = {
    "conclusion": "Cache keys are built before canonicalization, so equivalent path "
                  "spellings hash differently.",
    "evidence": [
        "The failing test distinguishes './a/../b' from 'b'.",
        "Independent continuations both locate the boundary at key construction.",
    ],
    "alternatives": [
        "A module-level cache leaking across fixtures would look similar.",
        "realpath() would change intended symlink semantics.",
    ],
    "next_action": "Read cache-key construction and every call site before editing.",
    "verification": [
        "Insert under one spelling, retrieve under an equivalent one.",
        "Re-run the symlink tests to confirm semantics are unchanged.",
    ],
    "carry_forward": ["Two mechanical fixes already failed; do not repeat them."],
}

WORKERS = {
    "fake-worker-a": "Normalization looks like it happens on lookup but not on insertion. "
                     "Read the key-construction helper before touching tests.",
    "fake-worker-b": "Do not assume the key is wrong. A module-level cache surviving "
                     "between parametrized cases produces the same symptom. Check fixture "
                     "scope first.",
    "fake-worker-c": "Search for normalize/resolve/realpath and cache-key creation. If keys "
                     "are built pre-canonicalization, fix the boundary once instead of "
                     "normalizing at every lookup.",
    "fake-worker-d": "Prior attempts probably patched the lookup path only. Confirm whether "
                     "insertion is reached at all for the failing input.",
}


def envelope(model: str, content: str | None, tool_calls=None, finish="stop") -> dict:
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 180,
            "total_tokens": 1380,
            "prompt_tokens_details": {"cached_tokens": 900},
        },
    }


@app.post("/v1/chat/completions")
async def completions(request: Request) -> dict:
    body = await request.json()
    model, messages = body["model"], body["messages"]

    if model in WORKERS:
        assert body.get("tool_choice") == "none", "workers must not be able to act"
        return envelope(model, WORKERS[model])

    if model == "fake-reducer":
        assert (body.get("response_format") or {}).get("type") == "json_object", (
            "the engine must ask the reducer for a JSON object"
        )
        return envelope(model, json.dumps(CHECKPOINT))

    # controller: deliberate once, then commit a real action. Deliberation is
    # detected from the protocol (a tool result answering a reason call), not
    # from the checkpoint's wording, so the engine is free to change how a
    # validated checkpoint renders.
    tool_names = {t["function"]["name"] for t in body.get("tools") or []}
    reason_ids = {
        tc.get("id")
        for m in messages
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
        if str((tc.get("function") or {}).get("name", "")).startswith("__reasonproxy")
    }
    already_deliberated = any(
        m.get("role") == "tool" and m.get("tool_call_id") in reason_ids for m in messages
    )
    can_deliberate = any(n.startswith("__reasonproxy") for n in tool_names)

    if can_deliberate and not already_deliberated:
        return envelope(
            model,
            "Two fixes already failed and several diagnoses remain open. Deliberating.",
            [{"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
              "function": {"name": "__reasonproxy_reason", "arguments": "{}"}}],
            "tool_calls",
        )
    return envelope(
        model,
        "Reading cache-key construction before changing behaviour.",
        [{"id": "call_shell_1", "type": "function",
          "function": {"name": "shell",
                       "arguments": '{"cmd": "rg -n \'cache_key|normalize\' src/"}'}}],
        "tool_calls",
    )


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9099)
    a = ap.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
