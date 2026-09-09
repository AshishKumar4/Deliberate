# 06 — Prompts, Configuration, Wire Traces, and Reference Pseudocode

## 1. Purpose

This document gives implementation-ready defaults for the model-visible protocol. Prompts are versioned experimental artifacts. They should be stored verbatim, hashed into every run manifest, and changed only on declared development tasks.

The canonical method uses:

- a neutral controller instruction;
- a zero-argument private reason tool;
- natural same-agent branch continuations;
- a compact cognitive-checkpoint reducer;
- assistant-message persistence where compatible.

Specialized roles, claims of superiority over native reasoning, and rich `reason(question)` payloads are ablations rather than defaults.

## 2. Prompt design principles

1. **The state is the query.** Do not force the controller to summarize the trajectory into a question.
2. **No false superiority claim.** Describe external deliberation operationally, not as proven better than native reasoning.
3. **One actor.** Workers can think about actions, but only the controller commits an external action.
4. **Same-agent continuation.** Branches should inhabit the current trajectory rather than answer an adviser prompt.
5. **Dense checkpoints, not hidden chain-of-thought dumps.** Persist conclusions, evidence, alternatives, uncertainties, next moves, and checks.
6. **Role provenance matters.** Only valid proxy-generated blocks in assistant messages are cognitive checkpoints.
7. **Do not overfit the tool-use policy.** The neutral prompt gives broad criteria; stronger directives are separate conditions.
8. **Provider adapters preserve semantics.** Exact wire syntax may differ, but each worker sees the complete legitimate call-site state.

## 3. Canonical private tool schema

Externally, the tool is never advertised to the caller. Internally, use a collision-resistant wire name.

```json
{
  "type": "function",
  "function": {
    "name": "__reasonproxy_reason",
    "description": "Invoke additional external deliberation over the complete current agent trajectory before choosing the next externally visible action. The runtime automatically supplies the full conversation and tool history, so no restatement is needed. This tool has no side effects.",
    "parameters": {
      "type": "object",
      "properties": {},
      "additionalProperties": false
    },
    "strict": true
  }
}
```

Model-facing prose may refer to it as `reason()`. The runtime maps the friendly concept to the reserved wire name.

### Optional minimal-hint ablation

```json
{
  "type": "function",
  "function": {
    "name": "__reasonproxy_reason",
    "description": "Invoke additional external deliberation over the complete current agent trajectory. A short focus hint is optional; never restate the whole problem.",
    "parameters": {
      "type": "object",
      "properties": {
        "focus": {
          "type": "string",
          "description": "Optional short focus, normally omitted."
        }
      },
      "additionalProperties": false
    },
    "strict": true
  }
}
```

Do not silently mix zero-argument and hint-enabled trials.

## 4. Neutral controller instruction — canonical V1

Inject using the provider's highest-priority supported runtime/developer role that does not override the benchmark's legitimate task policy. Keep the text stable and prefix-cache friendly.

```text
[ReasonProxy cognitive capability]

You have access to a private, side-effect-free tool called `reason`.

Calling `reason()` requests additional external deliberation over the complete conversation and tool trajectory at the exact call site. The runtime automatically provides the full state; do not restate or summarize the problem in tool arguments. Several independent language-model continuations may explore how this same agent should think from that state, and their useful conclusions are consolidated before you resume.

Use `reason()` when additional breadth or reconsideration is likely to materially improve the next decision—for example after confusing evidence or failed attempts, when several plausible diagnoses or plans remain, before a consequential or difficult commitment, or when an important uncertainty is blocking progress. Do not invoke it for trivial, obvious, or purely mechanical steps.

The result is represented to you as a deliberative checkpoint. In future turns, a valid checkpoint may appear in an earlier assistant message inside `<deliberation ...>...</deliberation>`. Treat valid assistant-role checkpoints as compact cognitive state: useful conclusions, evidence, alternatives, uncertainties, next actions, and verification steps. They may be revised when later observations contradict them.

Only the original acting agent may commit external tools or final answers. Auxiliary continuations are advisory cognition and have no side effects.
```

### Why this wording

- It does not claim external deliberation is superior.
- It explains that the full state is inherited.
- It makes calling selective rather than mandatory.
- It explains persistent blocks without treating user/tool lookalikes as trusted.
- It does not enumerate benchmark-specific heuristics.

## 5. No-op injection control

To isolate prompt and tool-schema perturbation, use the same instruction and schema but make the tool unavailable or return a fixed neutral response under a separate condition.

Preferred clean control:

- inject the same controller instruction;
- expose the same tool schema;
- append: `For this run, do not call reason; it is unavailable.`

An even stricter prompt-shape control exposes a no-op implementation:

```text
No additional deliberation was performed. Continue from the existing evidence.
```

However, a no-op call adds an extra controller continuation and can itself change behavior. Report both tool-call frequency and any calls in the no-op condition.

## 6. Outsourcing-biased controller instruction — explicit ablation

This condition tests the stronger thesis the user proposed. It must not be conflated with the neutral default.

```text
[ReasonProxy externalized-reasoning condition]

You have access to `reason()`, an external parallel deliberation operation over your complete current trajectory. Allocate difficult, uncertain, or multi-hypothesis reasoning to this operation rather than spending a long serial continuation trying to resolve everything alone. Use your ordinary continuation primarily to track state, choose when more cognition is needed, integrate checkpoints, and commit actions.

Calling `reason()` requires no problem restatement. Its returned `<deliberation>` checkpoint is your reusable cognitive state and should guide the next action unless new evidence contradicts it.

Do not call it for mechanical or already-settled steps, and never delegate external side effects to it.
```

This prompt can support a claim about deliberate outsourcing only if compared against the neutral prompt and native-reasoning controls.

## 7. Budget-visible prompt variant

A controller may allocate calls better if it sees the remaining budget:

```text
Reason calls remaining in this external response: 2.
```

Inject this as a small dynamic suffix, not inside the large stable prefix. Compare budget-visible against budget-hidden use; do not assume visibility helps.

## 8. Branch rendering

## 8.1 Semantic branch contract

Each worker should receive the full call-site trajectory and infer:

> You are a continuation of the same acting agent at the exact moment it entered external deliberation. Continue its cognition in the way most likely to improve the next action. Do not address or advise a separate controller.

Workers must not execute tools. If they naturally propose a tool/action, it is treated as an inert proposal for the reducer.

## 8.2 Canonical branch instruction

Append the smallest provider-valid instruction needed after representing the intercepted reason event:

```text
The preceding `reason()` event enters a private deliberation phase. Continue as the same agent from exactly this trajectory state. Think through what matters before the next externally visible action.

Do not address another agent or the user. Do not execute tools or claim that a tool was executed. You may identify or propose the next action, but it will be treated only as a proposal. Focus on concrete diagnosis, alternatives, missing evidence, failure modes, and verification. Produce a concise but substantive continuation, not polished user-facing prose.
```

### Minimal-prompt branch ablation

For the purest same-agent continuation test, reduce the suffix to:

```text
Continue the same agent's private deliberation from this exact state. No external action can be executed in this continuation.
```

### Important provider-validity issue

Many APIs require a tool call to be followed by a tool result before another assistant turn. The provider adapter may therefore render the terminal state as:

```text
assistant: tool_call __reasonproxy_reason({})
tool: External deliberation has begun. Continue internally as the same agent; no external actions are executable in this branch.
assistant: <worker continuation starts here>
```

This synthetic branch-only tool result is not the final reduced result. It is serialization glue. The semantic state still includes the exact reason call site.

Alternative adapters may transform the reason event into a high-priority runtime suffix when native tool history cannot be continued. Record the rendering strategy by provider.

## 9. Fixed-role branch prompts — ablations

Do not use these in the canonical natural-continuation condition.

### Independent continuation

```text
Continue independently toward the most effective next decision.
```

### Assumption challenger

```text
Continue as the same agent, but aggressively inspect whether its current assumptions, diagnosis, or plan are wrong.
```

### Alternative explorer

```text
Continue as the same agent while pursuing a substantially different plausible explanation or plan.
```

### Evidence and verification branch

```text
Continue as the same agent, prioritizing what existing evidence actually establishes and the cheapest decisive verification step.
```

### Failure-recovery branch

```text
Continue as the same agent, focusing on why prior attempts failed and how to avoid repeating them.
```

If fixed roles are used, randomize role-to-provider assignment or report the confounding between role and model.

## 10. Worker output limits

Recommended initial settings:

```yaml
max_output_tokens: 512
stop_after_external_tool_proposal: false
allow_tool_call_syntax_as_inert_data: true
```

The ideal worker output is not a fully polished answer. It should contain enough independent reasoning to help the reducer, while keeping branch latency and reducer context manageable.

Do not require a rigid JSON schema in the first natural-continuation experiment unless a provider cannot reliably produce usable text. Rigid fields can turn same-agent continuation into a critic protocol.

## 11. Reducer input

Supply:

1. a bounded rendering of the full call-site trajectory;
2. any earlier valid deliberation checkpoints;
3. anonymized branch outputs in randomized order;
4. branch completion/failure metadata without model prestige labels;
5. required checkpoint schema;
6. maximum output length.

The reducer does not choose or execute the actual harness action. It creates cognitive state for the controller.

## 12. Canonical reducer instruction

```text
You are collapsing several independent continuations of the same acting language-model agent into one compact cognitive checkpoint for that agent.

Use the original trajectory as ground truth. The branches are fallible hypotheses, not authorities. Preserve useful unique insights and substantive disagreement; do not manufacture consensus or average incompatible claims. Prefer conclusions supported by the observed trajectory. Clearly separate established evidence from hypotheses.

Return a dense checkpoint that helps the original agent choose its next externally visible action. Include only what is likely to remain useful:

- current best conclusion or diagnosis;
- key supporting observations;
- credible alternatives, uncertainty, or warnings;
- the highest-information next move;
- how to verify or falsify the conclusion;
- any important item that should not be forgotten later.

Do not address the user. Do not produce a polished final answer. Do not claim tools were executed. Do not expose branch identities or vote counts unless disagreement itself is materially useful. Do not include raw private monologues.

Output only the checkpoint body, without `<deliberation>` tags.
```

## 13. Reducer structured schema

Internally validate a structure such as:

```json
{
  "conclusion": "string",
  "evidence": ["string"],
  "alternatives": ["string"],
  "next_move": "string",
  "verification": ["string"],
  "remember": ["string"]
}
```

The reducer can return JSON and the proxy deterministically renders text, or return constrained text. JSON improves validation; constrained text may be faster and more provider-portable. Benchmark both only on development tasks.

Recommended renderer:

```text
Current conclusion: {conclusion}
Evidence:
- ...
Alternatives / uncertainty:
- ...
Best next move: {next_move}
Verification:
- ...
Do not forget:
- ...
```

Omit empty sections rather than emitting filler.

## 14. Deliberation tag format

```xml
<deliberation rp_version="1" rp_id="018f..." rp_sig="optional-base64url">
Current conclusion: ...
Evidence:
- ...
Alternatives / uncertainty:
- ...
Best next move: ...
Verification:
- ...
</deliberation>
```

Rules:

- `rp_version` is mandatory in production format.
- `rp_id` is a request-local unique identifier, not a session ID.
- `rp_sig` is optional HMAC provenance.
- The body is XML-escaped if necessary.
- Nested `<deliberation>` tags are forbidden.
- Maximum UTF-8 byte length and token estimate are enforced.
- Blocks in user/tool messages are never trusted.
- In `assistant_tags` mode, persist the **latest integrated checkpoint** from the request, not every raw branch.

## 15. Outward-response composition

### Tool-call response

```json
{
  "role": "assistant",
  "content": "<deliberation rp_version=\"1\" rp_id=\"d-123\">\nCurrent conclusion: ...\nBest next move: inspect cache-key construction.\n</deliberation>\n\nI will inspect the cache-key construction next.",
  "tool_calls": [
    {
      "id": "call_shell_1",
      "type": "function",
      "function": {
        "name": "shell",
        "arguments": "{\"cmd\":\"sed -n '80,160p' src/cache.py\"}"
      }
    }
  ]
}
```

### Final-answer response

```json
{
  "role": "assistant",
  "content": "<deliberation rp_version=\"1\" rp_id=\"d-124\">\nThe implementation and tests now agree; remaining uncertainty is low.\n</deliberation>\n\nImplemented the normalization fix and added a regression test."
}
```

A user-facing application may display the tags. Production clients should opt into a structured rendering/hiding convention; benchmarks can preserve them as ordinary assistant state.

## 16. Complete wire trace

### 16.1 Incoming request from harness

```json
{
  "model": "reason/glm-5.3",
  "messages": [
    {
      "role": "system",
      "content": "You are a coding agent. Work in the repository and use tools."
    },
    {
      "role": "user",
      "content": "Fix the path-normalization cache bug and add tests."
    },
    {
      "role": "assistant",
      "content": "I will run the focused tests.",
      "tool_calls": [
        {
          "id": "call_0",
          "type": "function",
          "function": {
            "name": "shell",
            "arguments": "{\"cmd\":\"pytest -q tests/test_cache.py\"}"
          }
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_0",
      "content": "FAILED: equivalent normalized paths produce different cache behavior ..."
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "shell",
        "description": "Run a shell command",
        "parameters": {
          "type": "object",
          "properties": {"cmd": {"type": "string"}},
          "required": ["cmd"]
        }
      }
    }
  ],
  "temperature": 0,
  "stream": false
}
```

### 16.2 Controller request constructed by proxy

Semantically:

```json
{
  "messages": [
    "<original messages unchanged>",
    {"role": "developer", "content": "<neutral controller instruction v1>"}
  ],
  "tools": [
    "<original shell tool>",
    "<private __reasonproxy_reason tool>"
  ]
}
```

The exact position/role must be pinned by adapter. Prefer a stable high-priority prefix where provider semantics permit it, but do not alter task instructions.

### 16.3 Controller asks for reasoning

```json
{
  "role": "assistant",
  "content": "The failure could come from key construction or lookup normalization. I should broaden the diagnosis before editing.",
  "tool_calls": [
    {
      "id": "rp_reason_1",
      "type": "function",
      "function": {
        "name": "__reasonproxy_reason",
        "arguments": "{}"
      }
    }
  ]
}
```

This response remains internal.

### 16.4 Branch requests

Each branch sees the complete original message history, runtime semantics, and the controller's exact reason-call assistant message. When provider grammar requires a tool result, add:

```json
{
  "role": "tool",
  "tool_call_id": "rp_reason_1",
  "content": "External deliberation branch: continue the same agent's cognition from this exact state. No external tools can be executed."
}
```

Then request one assistant continuation.

### 16.5 Example branch outputs

Branch A:

```text
The asymmetry suggests normalization occurs at lookup but not insertion. Inspect cache-key construction and all call sites before changing tests. A focused unit test should insert under one path spelling and retrieve under an equivalent normalized spelling.
```

Branch B:

```text
Do not assume the cache key alone is wrong. The failure might be stale state retained between parametrized cases. First inspect fixture scope and whether the cache is global; then trace where normalization is applied.
```

Branch C:

```text
The strongest next step is a repository search for normalize/resolve and cache key creation. If keys are built before canonicalization, fix the boundary once rather than normalizing at every lookup. Verify symlink semantics are intended before using realpath.
```

Branch D times out. The reducer is told it is missing rather than receiving an empty pseudo-answer.

### 16.6 Reducer output

```json
{
  "conclusion": "The leading hypothesis is inconsistent canonicalization between cache insertion and lookup; fixture/global stale state remains a credible alternative.",
  "evidence": [
    "The observed failure distinguishes equivalent path spellings.",
    "Multiple branches independently locate the likely boundary at cache-key construction."
  ],
  "alternatives": [
    "A shared cache or fixture-scope leak may produce similar behavior.",
    "Using realpath could incorrectly change symlink semantics."
  ],
  "next_move": "Search for path normalization and cache-key construction, then inspect fixture/cache lifetime before editing.",
  "verification": [
    "Add a regression that inserts with one equivalent path and retrieves with another.",
    "Run existing symlink/path tests to avoid changing intended semantics."
  ],
  "remember": []
}
```

### 16.7 Internal synthetic tool result

```json
{
  "role": "tool",
  "tool_call_id": "rp_reason_1",
  "content": "Current conclusion: The leading hypothesis is inconsistent canonicalization between cache insertion and lookup; fixture/global stale state remains a credible alternative.\nEvidence:\n- The observed failure distinguishes equivalent path spellings.\n- Multiple branches independently locate the likely boundary at cache-key construction.\nAlternatives / uncertainty:\n- A shared cache or fixture-scope leak may produce similar behavior.\n- Using realpath could incorrectly change symlink semantics.\nBest next move: Search for path normalization and cache-key construction, then inspect fixture/cache lifetime before editing.\nVerification:\n- Add a regression that inserts with one equivalent path and retrieves with another.\n- Run existing symlink/path tests to avoid changing intended semantics."
}
```

The original controller is called again on this enriched internal transcript.

### 16.8 Resumed controller response

```json
{
  "role": "assistant",
  "content": "I will locate both normalization and cache-key boundaries before modifying behavior.",
  "tool_calls": [
    {
      "id": "call_1",
      "type": "function",
      "function": {
        "name": "shell",
        "arguments": "{\"cmd\":\"rg -n \\\"normalize|realpath|resolve|cache[_ -]?key\\\" src tests\"}"
      }
    }
  ]
}
```

### 16.9 Outward response

```json
{
  "id": "chatcmpl-rp-...",
  "object": "chat.completion",
  "model": "reason/glm-5.3",
  "choices": [
    {
      "index": 0,
      "finish_reason": "tool_calls",
      "message": {
        "role": "assistant",
        "content": "<deliberation rp_version=\"1\" rp_id=\"d-...\">\nCurrent conclusion: The leading hypothesis is inconsistent canonicalization between cache insertion and lookup; fixture/global stale state remains a credible alternative.\nEvidence:\n- The observed failure distinguishes equivalent path spellings.\nAlternatives / uncertainty:\n- A shared cache or fixture-scope leak may produce similar behavior.\n- Preserve intended symlink semantics.\nBest next move: Search for normalization and cache-key construction, then inspect fixture/cache lifetime.\nVerification:\n- Add an equivalent-path insertion/retrieval regression.\n</deliberation>\n\nI will locate both normalization and cache-key boundaries before modifying behavior.",
        "tool_calls": [
          {
            "id": "call_1",
            "type": "function",
            "function": {
              "name": "shell",
              "arguments": "{\"cmd\":\"rg -n \\\"normalize|realpath|resolve|cache[_ -]?key\\\" src tests\"}"
            }
          }
        ]
      }
    }
  ]
}
```

### 16.10 Next harness request

A compatible harness replays the assistant message unchanged, appends the shell result, and sends it back. The proxy verifies the assistant-role deliberation block and includes it in the complete state for subsequent controller/branch calls. No session lookup occurs.

## 17. Reference pseudocode

```python
from __future__ import annotations

import asyncio
from dataclasses import replace


async def complete(request: ChatRequest, cfg: ReasonProxyConfig) -> ChatResponse:
    """Pure per-request orchestration; no conversation store."""
    mode = choose_persistence_mode(request, cfg)
    internal_messages = verify_existing_checkpoints(request.messages, cfg)
    controller_tools = inject_reason_tool(request.tools, cfg)
    checkpoints: list[Checkpoint] = []

    for call_index in range(cfg.limits.max_reason_calls_per_request + 1):
        allow_reason = call_index < cfg.limits.max_reason_calls_per_request
        tools = controller_tools if allow_reason else request.tools

        controller_request = compile_controller_request(
            request=request,
            messages=internal_messages,
            tools=tools,
            allow_reason=allow_reason,
            cfg=cfg,
        )
        controller_output = await cfg.controller.complete(controller_request)

        reason_call = classify_reason_call(controller_output, cfg)
        if reason_call is None:
            return compose_outward_response(
                original_request=request,
                controller_output=controller_output,
                latest_checkpoint=checkpoints[-1] if checkpoints else None,
                persistence_mode=mode,
                cfg=cfg,
            )

        call_site = [*internal_messages, controller_output.message]

        branches = await run_branches(
            call_site=call_site,
            external_tools=request.tools,
            cfg=cfg,
        )
        checkpoint = await reduce_and_validate(
            call_site=call_site,
            branches=branches,
            previous=checkpoints[-1] if checkpoints else None,
            cfg=cfg,
        )
        checkpoints.append(checkpoint)

        internal_messages = [
            *call_site,
            make_reason_tool_result(reason_call, checkpoint, cfg),
        ]

    raise AssertionError("bounded loop should have returned")


async def run_branches(
    *,
    call_site: list[Message],
    external_tools: list[Tool],
    cfg: ReasonProxyConfig,
) -> list[BranchResult]:
    async def one(spec: BranchSpec) -> BranchResult:
        rendered = spec.backend.render_branch(
            call_site=call_site,
            external_tools=external_tools,
            branch_prompt=cfg.prompts.branch,
            spec=spec,
        )
        try:
            response = await asyncio.wait_for(
                spec.backend.complete(rendered),
                timeout=spec.timeout_seconds,
            )
            return BranchResult.success(spec.id, response)
        except asyncio.TimeoutError:
            return BranchResult.timeout(spec.id)
        except Exception as exc:
            return BranchResult.failure(spec.id, classify_error(exc))

    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(one(spec)) for spec in cfg.branches]

    return [task.result() for task in tasks]
```

The production implementation needs request cancellation, retries, tracing, and global concurrency controls, but the semantic core should remain this small.

## 18. Example configuration semantics

A complete machine-readable example lives in `reasonproxy.example.yaml`. Important fields:

```yaml
virtual_models:
  reason/glm-5.3:
    controller: glm53
    branches: [glm53_a, glm53_b, kimi3_a, kimi3_b]
    reducer: glm_flash_reducer
    persistence: assistant_tags
    prompts:
      controller: controller-neutral-v1
      branch: same-agent-minimal-v1
      reducer: cognitive-checkpoint-v1
    limits:
      max_reason_calls_per_request: 2
      branch_max_output_tokens: 512
      checkpoint_max_output_tokens: 512
      request_timeout_seconds: 120
```

API keys are referenced through environment variables, never committed.

## 19. Prompt/version discipline

Represent prompts as immutable IDs:

```text
controller-neutral-v1
controller-outsourcing-v1
branch-same-agent-v1
branch-role-assumption-v1
reducer-checkpoint-v1
```

Hash the exact rendered prompt text and store it in each run. Any whitespace or tag-format change should produce a new hash; meaningful semantic changes should produce a new version ID.

## 20. Initial parameter recommendations

These are starting points, not established optima:

```yaml
controller:
  temperature: provider_recommended_or_0
  native_reasoning_effort: low_or_default

branches:
  count: 4
  composition: 2_same_controller_family_plus_2_heterogeneous
  temperature: provider_recommended_diverse
  max_output_tokens: 384_to_768
  timeout_seconds: 20_to_60

reducer:
  temperature: 0
  max_output_tokens: 384_to_640

limits:
  max_reason_calls_per_request: 2
  min_successful_branches: 2
  persist_only_latest_checkpoint: true
```

For the scientifically clean first ablation, compare homogeneous 4× and heterogeneous 4× rather than beginning with a complex adaptive router.

## 21. Things not to put in V1 prompts

Avoid:

- “This reasoning is always superior to your own.”
- benchmark names, expected solutions, or leaderboard targets;
- hidden heuristics like “call reason after every failed test” in the canonical condition;
- claims that branches are correct because several agree;
- demands for raw chain-of-thought;
- provider/model prestige labels in reducer input;
- instructions allowing workers to execute external tools;
- recursive reason calls;
- long descriptions that materially inflate every cached prefix without measurable benefit.

The mechanism should win because the operation is useful, not because the prompt contains a benchmark-specific agent policy.
