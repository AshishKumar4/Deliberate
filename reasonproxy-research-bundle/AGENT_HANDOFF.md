# Agent Handoff — Implement and Evaluate ReasonProxy

## Mission

Implement a general-purpose, semantically stateless OpenAI-compatible proxy that gives an existing frozen LLM controller one private cognitive tool:

```text
reason({})
```

When the controller invokes it, the proxy must capture the **entire current messages/tool trajectory including the reason call**, run several cheap LLM continuations in parallel **as if each were the same acting agent**, reduce those continuations into a compact cognitive checkpoint, feed the checkpoint back as the synthetic reason-tool result, resume the original controller, and return only its first externally visible real action or final answer.

When compatible with the caller, the outward assistant message must persist the checkpoint in:

```xml
<deliberation rp_version="1" ...>
...
</deliberation>
```

The caller's normal transcript then carries the checkpoint into future requests. The server must require no conversation database or hidden session state.

## Read order

1. `README.md`
2. `03_STATELESS_PROXY_SPEC.md` — normative protocol
3. `06_PROMPTS_CONFIG_AND_TRACES.md` — exact prompts and wire behavior
4. `05_IMPLEMENTATION_ROADMAP.md` — modules, milestones, tests
5. `04_BENCHMARK_PLAN.md` — do not benchmark until conformance passes
6. `02_LITERATURE_REVIEW.md` — understand nearest prior work
7. `01_RESEARCH_SPEC.md` and `07_PAPER_PLAN.md`
8. `SOURCES.md`

When documents conflict, follow this precedence:

```text
03_STATELESS_PROXY_SPEC.md
> 06_PROMPTS_CONFIG_AND_TRACES.md
> 05_IMPLEMENTATION_ROADMAP.md
> README.md
> discussion/background documents
```

## Non-negotiable V1 semantics

1. External API: start with non-streaming `/v1/chat/completions`; add buffer-then-SSE after correctness.
2. Cross-request semantics: stateless. No Redis/session transcript mapping may be required for correctness.
3. Controller: frozen public API model.
4. Reason tool: zero-argument strict object schema by default.
5. State inheritance: complete caller-supplied messages, tool history, valid previous checkpoints, and the exact current reason call.
6. Branches: one level only; no external tool execution; no recursive reason calls.
7. Branch behavior: natural same-agent trajectory continuation, not “advise the controller.”
8. Action authority: only the resumed original controller can return a real tool call/final answer.
9. Reduction: compact cognitive checkpoint preserving evidence, alternatives, uncertainty, best next move, and verification.
10. Persistence: `assistant_tags` only when the response contract safely permits assistant text plus action; otherwise `ephemeral`.
11. Strict schemas: never corrupt JSON/XML/action parsers by prepending tags.
12. Limits: bounded reason calls, branches, upstream calls, output tokens, concurrency, and deadlines.
13. Telemetry: separately account for controller, worker, reducer, retry, cancellation, latency, and cost.
14. Evaluation: hold benchmark harness and controller fixed; endpoint is the primary changed variable.

## Do not implement in V1

- worker access to shell/browser/files;
- recursive deliberation;
- server-side conversation storage;
- semantic answer caching;
- learned routing or RL;
- automatic claim that external reasoning is superior;
- task-specific worker roles as the default;
- a bespoke benchmark agent replacing mini-SWE/Terminus/Proximus;
- silent fallback to a different model during benchmark runs.

## Required repository skeleton

```text
src/reasonproxy/
  app.py
  settings.py
  http/
  core/
  protocol/
  providers/
  prompts/
  telemetry/
  security/
tests/
  unit/
  contract/
  integration/
  golden/
  benchmark_smoke/
configs/
experiments/
```

Follow the detailed structure in `05_IMPLEMENTATION_ROADMAP.md`.

## First implementation sequence

### Task 1 — Schemas and deterministic fake providers

Implement:

- OpenAI-style request/response types;
- canonical message/content/tool-call types;
- provider protocol;
- fake scripted controller, worker, and reducer;
- request-local trace model;
- config loading/validation.

Acceptance:

- message arrays round-trip without mutation;
- unknown safe fields survive;
- image/multipart content is not flattened;
- fake models can produce deterministic tool trajectories.

### Task 2 — Transparent pass-through endpoint

Implement:

- `GET /v1/models`;
- `POST /v1/chat/completions` non-streaming;
- virtual-model alias lookup;
- auth and upstream header policy;
- direct controller call without reason behavior.

Acceptance:

- a toy tool client behaves identically through the pass-through proxy and direct endpoint except IDs/model alias/telemetry;
- external tool definitions and tool calls remain intact.

### Task 3 — Private reason loop

Implement:

- private tool injection;
- exact reason-call detection;
- mixed real/reason tool-call policy;
- complete call-site snapshot;
- concurrent worker fan-out;
- reducer call and schema validation;
- synthetic tool result;
- controller resume;
- hard loop/upstream-call caps.

Acceptance:

- fake controller calls reason, consumes checkpoint, and changes its next action;
- private reason tool never appears in outward response;
- worker tool syntax is never executed;
- cancellation terminates all workers;
- maximum call caps cannot be bypassed.

### Task 4 — Persistence and compatibility

Implement:

- `<deliberation>` rendering/parsing;
- role provenance;
- optional HMAC signature;
- `assistant_tags`, `ephemeral`, `auto`;
- strict JSON detection/fallback.

Acceptance:

- a two-turn client replays the checkpoint and the next controller sees it;
- user/tool lookalike tags are not trusted;
- strict JSON output remains valid;
- text-plus-real-tool call remains parseable in a compatible client.

### Task 5 — Live GLM/Kimi adapters

Implement direct OpenAI-compatible adapters before relying on LiteLLM. Verify provider-specific requirements for:

- tool-call IDs and history;
- content plus tool calls;
- model aliases/snapshots;
- reasoning effort;
- recommended sampling;
- usage and cached-input fields;
- complete assistant-message preservation.

Acceptance:

- tiny live tool-use contract suite passes repeatedly;
- all structural provider differences are captured in adapters, not scattered through the engine.

### Task 6 — Benchmark-grade telemetry

Implement:

- JSONL/OpenTelemetry-style spans;
- separate usage/cost records per component;
- exact prompt/config hashes;
- manifest writer;
- secret redaction;
- trace replay CLI.

Acceptance:

- one request's actual upstream calls, tokens, latency, retries, and outcome reconcile;
- branch/reducer text can be retained in benchmark mode and omitted in production mode.

### Task 7 — Harness conformance

Run synthetic/tiny tasks through:

1. mini-SWE-agent/Pier;
2. Harbor/Terminus-2.

Verify every item in `04_BENCHMARK_PLAN.md` §4 before collecting scores.

## Canonical prompts

Copy prompt text verbatim from `06_PROMPTS_CONFIG_AND_TRACES.md`. Do not paraphrase it during implementation. Expose prompt IDs and hashes.

Default experiment:

```text
controller prompt: controller-neutral-v1
branch prompt:     branch-same-agent-v1
reducer prompt:    reducer-checkpoint-v1
tool schema:       zero-argument strict object
```

`controller-outsourcing-v1` is an ablation, not the default.

## Core algorithm

```python
async def reasonproxy_completion(request, config):
    history = verify_existing_assistant_checkpoints(request.messages)
    checkpoints = []

    for i in range(config.max_reason_calls_per_request + 1):
        tools = request.tools
        if i < config.max_reason_calls_per_request:
            tools = inject_private_reason_tool(tools)

        out = await controller(history, tools, request.params)

        if not contains_private_reason_call(out):
            return compose_normal_outward_response(
                out,
                latest_checkpoint=checkpoints[-1] if checkpoints else None,
                persistence=select_persistence_mode(request),
            )

        call_site = history + [out.message]
        branches = await parallel_same_agent_continuations(call_site)
        checkpoint = await reduce_branches(call_site, branches)
        checkpoints.append(checkpoint)
        history = call_site + [synthetic_reason_tool_result(out, checkpoint)]

    raise InternalInvariantError()
```

Use robust typed equivalents, deadlines, cancellation, tracing, and validation.

## Required tests before any benchmark spend

### Unit/property

- request immutability;
- private tool injection/removal;
- collision rejection;
- reason response classification;
- mixed-call behavior;
- tag/signature provenance;
- strict-schema fallback;
- budget termination;
- usage aggregation;
- canonical/provider round trips.

### Golden

- final text only;
- real tool call only;
- one reason then real tool;
- two reasons then final;
- branch timeout/failure;
- reducer invalid output/fallback;
- user-injected fake deliberation tag;
- image content;
- stream requested;
- client cancellation.

### Live contract

- GLM controller invokes reason and resumes;
- Kimi/GLM workers accept full tool trajectory rendering;
- assistant content + tool call is legal for selected controller;
- prior assistant checkpoint is preserved on replay;
- provider usage is parsed.

### Harness

- shell action executes;
- tool result is replayed;
- final submit action works;
- checkpoint does not alter parser;
- compaction behavior is recorded.

## First benchmark matrix

Do not begin with FrontierSWE. Start with a predeclared DeepSWE pilot.

```text
A direct controller
B same prompt/tool injection, reason disabled/no-op
C one external same-agent branch
D four homogeneous branches + reducer
E two GLM + two Kimi branches + reducer
F matched additional serial native reasoning
```

Use 10–20 tasks selected before treatment outcomes and at least three trials per task. Freeze the implementation after repairing protocol bugs, then run the full suite.

Mandatory later baselines:

- explicit `reason(question)`;
- fixed/automatic call schedule;
- random matched calls;
- Second Thought-like automatic boundary branching;
- raw concatenation;
- ephemeral versus assistant-tag persistence;
- best-of-N complete trajectories;
- frontier model under the same harness.

## Evidence standard

Never report:

- public leaderboard scores as though they were internally controlled;
- proxy system score as raw “GLM” or “Kimi” performance;
- cached/promotional prices without a date;
- malformed/infrastructure failures as model failures without adjudication;
- selected best prompts/configurations from the test set without disclosure.

Use system names such as:

```text
GLM-5.3 direct
ReasonProxy(GLM-5.3; 4x homogeneous)
ReasonProxy(GLM-5.3; 2x GLM + 2x Kimi)
```

Report actual total inference.

## Nearest-prior-work warning

Read `02_LITERATURE_REVIEW.md` before writing novelty claims. **Second Thought (arXiv:2608.13667)** is very close. The implementation and paper must not claim first use of full-trajectory parallel thoughts inside an agent.

The empirical differentiators to isolate are:

- explicit self-selected reason call;
- immediate pre-action effect;
- zero/minimal payload and full call-site inheritance;
- natural same-agent/heterogeneous branches;
- compact reducer checkpoint;
- assistant-message persistence;
- stateless drop-in endpoint.

## Completion deliverables

1. Running proxy and Dockerfile.
2. Typed config matching `reasonproxy.example.yaml` or a documented evolved schema.
3. Fake-provider and live-provider test suite.
4. mini-SWE and Terminus conformance reports.
5. Frozen pilot manifest and task split.
6. Per-condition results with cost/latency/usage.
7. Failure analysis.
8. Reproduction commands.
9. Updated paper draft that distinguishes measurements from hypotheses.

Do not ask for redesign unless a concrete provider/harness constraint invalidates a normative contract. When that occurs, document the incompatibility, preserve the core semantics, implement the narrowest adapter, and add a regression test.
