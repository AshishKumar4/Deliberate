# 05 — Implementation Roadmap and Engineering Plan

## 1. Objective

Build a general-purpose, semantically stateless OpenAI-compatible proxy that can be placed between an existing agent harness and an arbitrary controller LLM endpoint. The proxy adds one private cognitive tool, resolves it through parallel commodity-model continuations, and returns an ordinary assistant response that the original harness can consume without understanding the mechanism.

V1 should optimize for:

- correctness and inspectability;
- strict separation between cognitive and external tools;
- reproducible benchmarking;
- provider portability;
- no server-side conversation store;
- precise usage/latency accounting;
- simple failure semantics.

It should **not** optimize first for high-scale multi-tenancy, adaptive learned routing, semantic caching, or complex multi-agent behavior.

## 2. Proposed repository layout

```text
reasonproxy/
├── pyproject.toml
├── README.md
├── LICENSE
├── CHANGELOG.md
├── configs/
│   ├── reasonproxy.example.yaml
│   ├── benchmark.deep-swe.yaml
│   ├── benchmark.terminal-bench.yaml
│   └── providers.example.yaml
├── src/reasonproxy/
│   ├── __init__.py
│   ├── app.py
│   ├── settings.py
│   ├── errors.py
│   ├── version.py
│   ├── http/
│   │   ├── chat_completions.py
│   │   ├── models.py
│   │   ├── streaming.py
│   │   ├── auth.py
│   │   └── compatibility.py
│   ├── core/
│   │   ├── engine.py
│   │   ├── state.py
│   │   ├── reason_loop.py
│   │   ├── brancher.py
│   │   ├── reducer.py
│   │   ├── persistence.py
│   │   ├── budgets.py
│   │   ├── deadlines.py
│   │   └── validation.py
│   ├── protocol/
│   │   ├── openai_types.py
│   │   ├── canonical_events.py
│   │   ├── tool_calls.py
│   │   ├── deliberation_tags.py
│   │   └── structured_outputs.py
│   ├── providers/
│   │   ├── base.py
│   │   ├── openai_compatible.py
│   │   ├── zai.py
│   │   ├── moonshot.py
│   │   ├── openrouter.py
│   │   ├── anthropic.py       # later/optional
│   │   └── registry.py
│   ├── prompts/
│   │   ├── controller.py
│   │   ├── branch.py
│   │   ├── reducer.py
│   │   └── versions.py
│   ├── telemetry/
│   │   ├── events.py
│   │   ├── usage.py
│   │   ├── tracing.py
│   │   ├── redaction.py
│   │   └── exporters.py
│   ├── security/
│   │   ├── provenance.py
│   │   ├── injection.py
│   │   ├── secrets.py
│   │   └── limits.py
│   └── cli/
│       ├── main.py
│       ├── serve.py
│       ├── validate_config.py
│       └── replay.py
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── golden/
│   └── benchmark_smoke/
├── experiments/
│   ├── manifests/
│   ├── analysis/
│   ├── task_splits/
│   └── notebooks/
└── docs/
    ├── architecture.md
    ├── protocol.md
    ├── providers.md
    └── evaluation.md
```

The core engine must not depend on FastAPI request objects. HTTP is one adapter over a pure request-to-response inference function.

## 3. Recommended implementation stack

A practical Python stack:

- Python 3.12 or newer;
- FastAPI or Starlette for the HTTP surface;
- Pydantic v2 for request/config validation;
- `httpx.AsyncClient` for upstream calls;
- `asyncio.TaskGroup` or AnyIO task groups for branch fan-out;
- `orjson` for wire serialization where compatible;
- OpenTelemetry-compatible spans plus JSONL traces;
- `pytest`, `pytest-asyncio`, Hypothesis, and respx for testing.

LiteLLM can be supported as an optional adapter, but the first core should not make correctness depend on a large compatibility layer. Native OpenAI-compatible calls to Z.AI and Moonshot are enough for the initial model set.

## 4. Architectural boundaries

## 4.1 Pure core interface

The key interface should look conceptually like:

```python
class ReasonEngine(Protocol):
    async def complete(
        self,
        request: ChatCompletionRequest,
        *,
        trace: TraceContext,
    ) -> ChatCompletionResponse:
        ...
```

It receives the complete caller-supplied request and returns one ordinary response. It does not read or write conversation state outside that invocation.

## 4.2 Provider interface

```python
class ModelBackend(Protocol):
    name: str

    async def complete(
        self,
        request: CanonicalModelRequest,
        *,
        deadline: float | None,
        cancellation: CancellationToken,
    ) -> CanonicalModelResponse:
        ...
```

Backend responsibilities:

- serialize canonical messages/tools into provider-specific wire format;
- preserve assistant tool-call history correctly;
- parse responses into canonical content/tool calls/usage;
- expose model limits and capability flags;
- classify retryable and terminal errors;
- report actual usage fields without fabricating missing values.

Backends should **not** decide when to call `reason()` or how to reduce branches.

## 4.3 Canonical event model

Use a provider-neutral event type:

```python
@dataclass(frozen=True)
class CanonicalMessage:
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: list[ContentPart]
    tool_calls: tuple[CanonicalToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)
```

Retain unknown safe fields where possible for pass-through compatibility. Avoid reducing all content to strings: coding and ARC tasks may contain images, files, or structured parts.

## 4.4 No action authority in workers

Workers may output proposed tool calls because ordinary agent continuations may naturally do so, but those proposals are inert data. Only the original controller response after reduction may be returned as an executable action.

Never forward a worker's tool call directly to the benchmark harness.

## 5. The request lifecycle

### Step 1 — validate and classify the incoming request

- authenticate caller if configured;
- reject unsupported stateful-only patterns;
- parse messages, tools, response format, streaming, and sampling parameters;
- classify output contract as free-text/tool-native/strict-schema;
- choose persistence mode conservatively;
- resolve the virtual model alias into controller/worker/reducer configuration;
- create an internal request-local trace ID.

### Step 2 — prepare controller request

- copy caller messages without mutating their in-memory representation;
- inject a stable controller instruction using the least disruptive supported role;
- add private `reason()` tool with a collision-resistant name or reject collision;
- keep caller tool order stable; append the private tool predictably;
- ensure strict schema requests are not made invalid by the new tool.

### Step 3 — invoke controller

Call the frozen controller model. Validate that the response is structurally legal.

### Step 4 — classify response

- no `reason()` call: return/sanitize/persist as required;
- one `reason()` call: enter the branch-and-collapse loop;
- mixed real tools and `reason()` in one response: apply a documented policy;
- multiple `reason()` calls: reject or collapse to one in V1;
- malformed call: retry once only under a pinned policy, then fail/fallback.

Recommended V1 mixed-call policy:

> If `reason()` appears with any externally visible tool call in the same assistant response, do not execute either. Treat the response as an internal reason request, include the proposed external calls as branch context, and ask the resumed controller to choose again after receiving the checkpoint.

This avoids racing cognition against side effects.

### Step 5 — construct call-site state

Create an assistant tool-call event containing exactly the controller's `reason()` call, including any minimal optional hint. The call-site state is:

```text
incoming caller transcript
+ injected runtime semantics (model-visible but not persisted verbatim)
+ controller reason tool call
```

Provider adapters then render this state in a legal continuation format.

### Step 6 — fan out branches

Run configured branches concurrently under per-branch and global deadlines.

Each branch receives:

- the complete legitimate caller-visible trajectory;
- previous valid assistant deliberation checkpoints already in that trajectory;
- the terminal `reason()` event or provider-equivalent representation;
- a short runtime instruction to continue the same agent's cognition;
- the external tool schemas as information, but no authority to execute them;
- no hidden tests/reference solutions/provider secrets.

### Step 7 — collect quorum

V1 can use `all` for scientific simplicity or `min_successes=N` with a deadline for production. Record every timeout/cancellation. Do not pretend a missing branch voted for the majority.

### Step 8 — reduce

The reducer receives:

- a compact rendering of the call-site state;
- branch outputs in randomized/anonymized order;
- optional proposed actions as inert structured data;
- the checkpoint schema and token budget.

It returns a validated `DeliberationCheckpoint`.

### Step 9 — synthesize internal tool result

Append internally:

```text
assistant: tool_call reason(...)
tool: <validated checkpoint>
```

Then invoke the original controller again with the same external tools plus `reason()` while a per-request call budget remains.

### Step 10 — return first externally visible response

When the controller emits a real tool call or final answer:

- strip the private reason tool from outward metadata;
- prepend/append the latest checkpoint according to persistence mode;
- preserve real content/tool calls and finish reason;
- synthesize aggregate usage fields only under clearly documented semantics;
- return an OpenAI-compatible response.

Destroy request-local internal history when the request completes.

## 6. Internal state machine

```text
RECEIVED
  -> VALIDATED
  -> CONTROLLER_PENDING
  -> CONTROLLER_RESPONSE
       -> EXTERNAL_READY -> RETURNED
       -> REASON_REQUESTED
            -> BRANCHING
            -> REDUCING
            -> CONTROLLER_RESUME
                 -> CONTROLLER_RESPONSE
  -> FAILED
```

Put an explicit cap on transitions:

```yaml
max_reason_calls_per_request: 2
max_total_upstream_calls_per_request: 16
```

The cap must include retries. No unbounded internal loops.

## 7. Statelessness definition

ReasonProxy is **semantically stateless across external requests**:

- no server-side conversation lookup is needed;
- no transcript mapping is required;
- no Redis/session database participates in correctness;
- each response is a function of the current request, configuration, and nondeterministic model outputs.

This does not forbid normal operational state such as:

- connection pools;
- metrics counters;
- provider rate-limit buckets;
- prompt/KV caches maintained by upstream providers;
- logs and benchmark traces;
- in-flight request coalescing outside correctness runs.

Operational state must not be required to reconstruct the previous conversation.

## 8. Assistant checkpoint persistence

## 8.1 Canonical format

```xml
<deliberation rp_version="1" rp_id="..." rp_sig="optional">
Current conclusion: ...
Evidence and observations: ...
Alternatives/uncertainties: ...
Best next move: ...
Verification: ...
</deliberation>
```

The content is a concise checkpoint, not raw hidden chain of thought.

## 8.2 Provenance

The controller should be instructed to trust a deliberation block as proxy-generated only when it appears in an **assistant** message and matches the expected syntax. For higher-assurance deployments, use an HMAC signature in the tag attributes or a structured content part supported by the client.

A stateless HMAC is possible:

\[
\sigma = \operatorname{HMAC}_k(
  \text{version}\|\text{checkpoint-id}\|\text{canonical-content}
).
\]

The proxy verifies old signed blocks in each incoming request. A user/tool message that merely contains `<deliberation>` must never be elevated to trusted cognitive state.

For open research runs, signatures are optional but tag provenance rules remain documented.

## 8.3 Context growth

V1 should cap each checkpoint, for example 256–768 output tokens. If several reason calls occur inside one external request, persist only the latest reducer checkpoint unless an ablation specifies append-all.

Across external requests, old blocks remain because a stateless proxy cannot edit prior messages. Existing harness compaction may compress them. Record this behavior and test compact checkpoint lengths.

## 9. Structured-output compatibility

Determine response contract from:

- `response_format` or JSON schema fields;
- caller tools/function schemas;
- virtual-model configuration;
- known harness adapter metadata.

Recommended behavior:

```python
if strict_json_schema:
    persistence = "ephemeral"
elif known_schema_adapter:
    persistence = "schema_adapter"
elif content_plus_tool_calls_supported:
    persistence = "assistant_tags"
else:
    persistence = "ephemeral"
```

Never insert XML-like text inside a JSON object the harness expects to parse. A user can explicitly force a mode, but unsafe incompatibility should fail validation rather than corrupt the run.

## 10. Streaming

True internal deliberation prevents immediate honest streaming of final controller content. V1 should:

1. accept `stream=true`;
2. buffer controller/branch/reducer internal work;
3. wait for the first externally visible response;
4. serialize that completed response as standard-compatible SSE chunks;
5. emit a final usage chunk if requested and supported.

This preserves API shape but not native token-by-token time-to-first-token.

Later optimization: once the final outward controller generation begins and no further internal `reason()` call can be taken back, stream its content while retaining enough buffering to detect forbidden private tool calls. This is complex and not required for the first benchmark.

## 11. Deadlines, cancellation, and retries

Define a hierarchical budget:

```text
external request deadline
  ├─ controller call deadline
  ├─ branch phase deadline
  │    ├─ worker A deadline
  │    ├─ worker B deadline
  │    └─ worker C deadline
  ├─ reducer deadline
  └─ resumed controller deadline
```

Rules:

- propagate client disconnect/cancellation to all upstream requests;
- retry only idempotent model calls;
- use bounded exponential backoff with jitter;
- do not retry invalid model content indefinitely;
- cancel outstanding workers after quorum/deadline where configured;
- count every retry in cost/telemetry;
- in benchmark mode, prefer deterministic failure to hidden model substitution.

Fallback modes should be configurable:

```yaml
on_branch_quorum_failure: reduce_available | resume_without_reason | fail
on_reducer_failure: deterministic_concat | resume_without_reason | fail
on_controller_resume_failure: fail
```

For the primary experiment, pin one policy and report it.

## 12. Caching

## 12.1 Provider prompt caching

Arrange stable prompt prefixes consistently:

```text
stable system/developer policy
stable tool schemas
stable task/repository context
stable prior transcript
new observation
runtime reason semantics / call-site suffix
```

Provider prompt caches are independent; a Kimi cache does not help GLM. Preserve provider-specific assistant messages exactly when required for cache and tool-call continuity.

## 12.2 Request-local reuse

Within one reason phase, homogeneous samples share an exact prefix. Use provider batch/multi-sample APIs only when semantics and usage accounting are equivalent and pinned.

## 12.3 Exact response cache

Disable exact final-response caching during correctness benchmark runs unless it is itself an experimental condition. It can incorrectly collapse nominally independent trials.

## 12.4 Semantic cache

Out of scope for V1 benchmark runs. Similar prompts can require different actions based on subtle environment state.

## 12.5 Single-flight

Useful in production, but disable or key it by trial identity in benchmark runs so independent samples remain independent.

## 13. Telemetry model

Emit one request summary and child events:

```json
{
  "event": "reasonproxy.request.completed",
  "trace_id": "...",
  "external_model": "reason/glm-5.3",
  "controller_calls": 2,
  "reason_calls": 1,
  "worker_calls": 4,
  "worker_successes": 4,
  "reducer_calls": 1,
  "latency_ms": 7342,
  "usage": {
    "controller": {},
    "workers": {},
    "reducer": {},
    "aggregate_actual_cost_usd": 0.0
  },
  "persistence_mode": "assistant_tags",
  "outcome": "external_tool_call"
}
```

Required span/event types:

- request received/validated;
- controller request/response;
- reason detected;
- branch started/completed/failed/cancelled;
- reducer request/response;
- checkpoint validated;
- controller resumed;
- outward response serialized;
- usage/cost reconciled;
- request failed.

Benchmark traces may retain branch text under access controls. Production default should redact or omit prompts and model content.

## 14. Cost accounting

Track actual provider usage separately by component. Never present aggregate tokens as though the apparent controller generated them.

```python
@dataclass
class ComponentUsage:
    provider: str
    model: str
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    billed_cost_usd: Decimal | None
    latency_ms: int
```

Price tables must be dated configuration, not hard-coded timeless facts. Store the exact pricing snapshot used for analysis and prefer provider-reported billed cost where available.

Expose optional response metadata only in a namespaced field or header so generic clients are not broken:

```text
X-ReasonProxy-Trace-Id: ...
X-ReasonProxy-Reason-Calls: 1
```

Do not overload OpenAI `usage` fields without documenting whether they mean outward controller usage or total internal usage. For experiments, save full sidecar telemetry.

## 15. Security and trust boundaries

### 15.1 Prompt injection through tags

Only proxy-validated assistant-role blocks are trusted. User/tool content can contain arbitrary tag-like strings and must remain untrusted data.

### 15.2 Tool-name collision

Prefer a reserved internal name such as `__reasonproxy_reason` at the wire layer while describing it to the model as `reason`. If the caller already defines the reserved name, reject or deterministically namespace it.

### 15.3 Branch data exposure

ReasonProxy forwards the caller transcript to configured worker/reducer providers. This is a material data-sharing boundary. Configuration must make the provider set explicit, and production use should support allowlists, no-retention providers, or homogeneous same-provider branches.

### 15.4 Secrets

- never log authorization headers;
- strip internal endpoint credentials from traces;
- redact tool results under configurable patterns;
- do not pass proxy operational metadata to workers unless needed;
- avoid sending hidden harness data the controller did not receive.

### 15.5 Side effects

Workers and reducer have no executable tool channel. Even if they emit syntactic tool calls, treat them as inert text/metadata.

### 15.6 Resource abuse

Enforce:

- maximum branch count;
- maximum reason calls/request;
- maximum output tokens/component;
- maximum upstream calls;
- global concurrency and spend limits;
- user/API-key quotas;
- request deadline.

## 16. Provider adapters

## 16.1 OpenAI-compatible baseline

Implement first:

- `/v1/chat/completions` requests;
- standard messages/content/tool calls;
- streaming pass/buffer support;
- custom base URL and headers;
- provider-specific extra body fields;
- usage parsing.

## 16.2 Z.AI / GLM

Validate:

- tool-call serialization and IDs;
- reasoning-effort fields;
- prompt cache reporting if any;
- sampling restrictions/defaults;
- exact model version/alias behavior;
- content plus tool-call support.

## 16.3 Moonshot / Kimi

Validate:

- preservation of complete assistant messages during tool loops;
- reasoning-effort semantics;
- fixed or recommended sampling parameters;
- long-context behavior;
- cached-input usage fields;
- tool-call history requirements.

## 16.4 Additional endpoints

Add only after the canonical path works. Every adapter needs contract fixtures and a known-good captured response set.

## 17. Testing strategy

## 17.1 Unit tests

- reason tool injection/removal;
- collision handling;
- response classification;
- canonical message round trips;
- tag parse/sign/verify;
- persistence-mode selection;
- budget/deadline calculations;
- usage aggregation;
- reducer schema validation;
- mixed-tool policy.

## 17.2 Property tests

Use Hypothesis to generate message arrays and ensure:

- caller messages are not mutated;
- external tools round-trip unchanged;
- private tool never leaks outward;
- arbitrary user/tool tag strings are not trusted;
- serialization then parsing preserves supported content;
- request caps always terminate the internal state machine.

## 17.3 Golden wire tests

Store anonymized fixtures for:

- ordinary final answer;
- ordinary tool call;
- one reason call then real tool call;
- two reason calls then final answer;
- branch-proposed tools;
- strict JSON response format;
- multi-part image content;
- upstream error and retry;
- client cancellation;
- streaming response.

## 17.4 Fake-provider integration tests

Build deterministic fake models:

```text
controller A: always calls reason once, then shell
worker A/B: return predefined divergent continuations
reducer: returns valid checkpoint
controller B: consumes checkpoint and changes action
```

This proves the mechanism without spending API money or relying on nondeterminism.

## 17.5 Live provider contract tests

Run tiny low-cost checks nightly/on demand, never as mandatory unit tests. Pin expected structural properties, not exact prose.

## 17.6 Harness smoke tests

- mini-SWE-agent synthetic repository;
- Harbor/Terminus hello-world task;
- structured JSON parser fixture;
- final submission command path;
- context summarization path.

## 18. Milestones

### M0 — Specification and fixtures

Deliver:

- canonical schemas;
- prompt versions;
- fake transcript fixtures;
- experiment config schema;
- test plan.

Exit criterion: another engineer can implement the state machine from tests.

### M1 — Transparent pass-through proxy

Deliver:

- `/v1/models`;
- `/v1/chat/completions` non-streaming;
- auth/header forwarding policy;
- direct controller responses unchanged;
- basic telemetry.

Exit criterion: mini-SWE or a small tool client behaves identically direct versus pass-through.

### M2 — Internal reason loop with fake backends

Deliver:

- private tool injection;
- interception;
- branch TaskGroup;
- reducer interface;
- resumed controller;
- loop caps.

Exit criterion: golden end-to-end tests pass with zero private-tool leakage.

### M3 — GLM and Kimi live adapters

Deliver:

- provider configs;
- exact tool-history behavior;
- usage/latency collection;
- reasoning-effort support;
- provider conformance report.

Exit criterion: controlled tool-use examples complete reliably through each provider.

### M4 — Checkpoint persistence

Deliver:

- `<deliberation>` generator/parser;
- optional signatures;
- `assistant_tags`, `ephemeral`, and `auto` modes;
- strict-schema fallback.

Exit criterion: two-turn conformance tests prove replay or safe fallback.

### M5 — Benchmark-grade observability

Deliver:

- manifest capture;
- branch/reducer trace store;
- cost reconciliation;
- failure attribution;
- deterministic config hashing;
- replay CLI.

Exit criterion: one failed task can be reconstructed and diagnosed from artifacts.

### M6 — DeepSWE/mini-SWE integration

Deliver:

- example endpoint configuration;
- synthetic repo smoke suite;
- 5-task validation run;
- no-op injection control.

Exit criterion: no treatment-specific parser/submission defects.

### M7 — Preregistered pilot

Deliver:

- frozen 10–20 task split;
- condition matrix;
- aggregate report with confidence intervals and resource curves;
- failure taxonomy.

Exit criterion: decide go/no-go for full runs using predeclared thresholds.

### M8 — Full evaluation and release

Deliver:

- full DeepSWE/Terminal-Bench runs;
- reproducibility package;
- public source code where permitted;
- paper draft and artifacts.

## 19. Risk register

| Risk | Consequence | Mitigation |
|---|---|---|
| Controller rarely calls reason | No treatment effect | Neutral instruction tuning on development tasks; heuristic/oracle ablations |
| Controller calls reason constantly | Cost/latency explosion | Hard budgets; expose remaining budget; train policy later |
| Cheap branches are correlated | Little additional information | Heterogeneous endpoints; role ablations; measure disagreement |
| Reducer loses the one correct branch | Negative scaling | Preserve disagreement/evidence; compare concat/controller-sees-all |
| Tags break harness parser | Invalid comparison | Conformance gate; `ephemeral`/schema adapters |
| Client drops assistant content | No cross-turn persistence | Detect via integration config; ephemeral mode; harness-specific adapter |
| Provider changes model silently | Nonreproducible results | Date/version pinning; interleaved conditions; rerun calibration tasks |
| Prompt caching varies | Cost/latency instability | Record cache-hit usage; warm/cold analyses |
| Branch fan-out hits rate limits | Tail latency and failures | Provider concurrency controls; homogeneous batch; quota planning |
| Full context is expensive | Cost dominates | Provider caching; concise checkpoints; branch context ablation later |
| External deliberation leaks private data to more providers | Deployment blocker | Explicit provider allowlist; same-provider mode; data policy |
| Hidden reason prompt is a confound | Reviewer objection | No-op injection control; publish prompts |
| Second Thought overlaps strongly | Weak novelty | Direct comparison and precise differentiation |
| Method improves only next action, not task success | Weak practical result | Persistence ablation; trajectory analysis; report honestly |
| Benchmark scores depend on harness quirks | Poor generality | Two fixed harnesses and cross-domain transfer |

## 20. Definition of done for V1

V1 is complete when:

- an arbitrary compatible client can point its OpenAI Chat Completions base URL at the proxy;
- the proxy is stateless across requests;
- the controller can invoke a private zero-argument reason tool;
- the complete call-site transcript is branched to configured workers;
- workers cannot cause side effects or recurse;
- a reducer creates a validated concise checkpoint;
- the controller resumes and returns one real action/final answer;
- compatible clients receive a persistent assistant deliberation block;
- strict schemas fall back safely;
- all upstream usage and latency are attributable;
- DeepSWE and Terminus conformance tests pass;
- a frozen config can be reproduced from a run manifest.

Everything else—learned routing, semantic caching, speculative prefetch, branch tools, recursive deliberation, dynamic worker selection—is V2 or research ablation, not a prerequisite.
