# ReasonProxy / Externalized Deliberation

**Research and implementation bundle**  
**Status:** design specification; no benchmark result is claimed yet  
**Research snapshot:** 2026-09-04  
**Primary target:** frozen, already-shipped commodity LLMs used through public APIs

## One-sentence thesis

A language-model agent need not perform all difficult reasoning inside one serial model continuation: it can invoke a nearly zero-argument `reason()` tool that expands the complete live trajectory into several parallel same-agent continuations, collapses them into a compact cognitive checkpoint, and returns that checkpoint through an ordinary stateless OpenAI-compatible response.

## What is in this bundle

| File | Purpose |
|---|---|
| `01_RESEARCH_SPEC.md` | Research question, formal model, hypotheses, novelty position, and final design |
| `02_LITERATURE_REVIEW.md` | Annotated prior art, comparison matrix, and closest-work analysis |
| `03_STATELESS_PROXY_SPEC.md` | Normative protocol and implementation design for the proxy |
| `04_BENCHMARK_PLAN.md` | How DeepSWE, Terminal-Bench, FrontierSWE, ARC-AGI, and related harnesses work; exact evaluation plan |
| `05_IMPLEMENTATION_ROADMAP.md` | Repository layout, engineering milestones, tests, telemetry, deployment, and risk register |
| `06_PROMPTS_CONFIG_AND_TRACES.md` | Proposed tool descriptions, prompts, configuration, wire examples, and pseudocode |
| `07_PAPER_PLAN.md` | Contribution claims, paper outline, figures/tables, and publication thresholds |
| `SOURCES.md` | Verified source catalog and dated benchmark/model snapshots |
| `REFERENCES.bib` | BibTeX for the core academic literature |
| `reasonproxy.example.yaml` | Machine-readable starting configuration |
| `AGENT_HANDOFF.md` | Concise implementation instructions for coding agents |
| `MASTER_GUIDE.md` | Concatenation of the substantive documents for single-context ingestion |

## Canonical design, in one diagram

```mermaid
sequenceDiagram
    participant H as Existing agent harness
    participant P as Stateless ReasonProxy
    participant C as Frozen controller LLM
    participant W as Parallel cheap LLM branches
    participant R as Fast reducer

    H->>P: Chat Completions request: full messages + external tools
    P->>C: Same request + private reason() tool + stable instructions
    C-->>P: reason({})
    Note over P: Intercept; do not expose to harness
    P->>W: Full call-site trajectory, rendered per provider
    W-->>P: Same-agent cognitive continuations
    P->>R: Context + branch continuations
    R-->>P: Compact deliberation checkpoint
    P->>C: Synthetic reason tool result; resume controller
    C-->>P: Real tool call or final answer
    P-->>H: Normal assistant response with <deliberation> checkpoint
    Note over H,P: Harness replays assistant response next turn; no server session required
```

## Final semantic contract

The internal action is approximately:

```text
reason({})
```

It means:

> Expand the complete current agent state into additional parallel inference. Do not require the controller to restate or summarize the problem. Return a compact, reusable deliberative checkpoint before the controller chooses its next externally visible action.

The proxy is a stateless transducer:

\[
F_{\mathcal R}: (H_t, T, \Theta) \longrightarrow A_t
\]

where:

- `H_t` is the complete caller-supplied message history;
- `T` is the caller's ordinary external tool set;
- `Θ` is proxy policy and provider configuration;
- `A_t` is an ordinary assistant response accepted by the existing harness.

A `reason()` call creates parallel continuations from the same call-site state:

\[
z_i \sim q_{\phi_i}(\cdot \mid H_t \oplus [\operatorname{REASON}]),\quad i=1\dots K,
\]

then collapses them:

\[
D_t = g_\psi(H_t, z_1,\ldots,z_K).
\]

The controller sees `D_t` as the result of its reason tool and resumes. The outward response persists a concise version of `D_t` in normal assistant content:

```xml
<deliberation rp_version="1">
Likely diagnosis: ...
Evidence: ...
Alternatives and risks: ...
Best next move: ...
How to verify: ...
</deliberation>
```

This block is **not a raw chain-of-thought dump**. It is a compact state checkpoint containing conclusions, alternatives, uncertainties, and actionable verification steps.

## Normative V1 decisions

1. **Stateless server semantics.** No Redis, hidden conversation mapping, or `previous_response_id` dependency is required for Chat Completions. Every request is independently computable from the supplied transcript.
2. **One-level external deliberation.** Workers cannot recursively call `reason()` and cannot execute external tools.
3. **Full call-site inheritance.** The controller does not need to write a useful question. The full transcript and the fact that it called `reason()` specify the cognitive problem.
4. **Same-agent continuation by default.** Workers continue the live trajectory as plausible copies of the acting agent. Critic roles and specialized branch prompts are ablations, not the canonical mechanism.
5. **One authority for action.** Only the original controller may emit real tool calls to the harness.
6. **Compact reduction.** A reducer converts branches into a cognitive checkpoint instead of concatenating long essays.
7. **Persistent assistant content where compatible.** The final checkpoint is emitted in `<deliberation>` tags alongside the real action/final answer so the normal client transcript carries it into future requests.
8. **Neutral default prompt.** Do not claim external deliberation is already proven superior to native reasoning. An outsourcing-biased prompt is tested as an explicit experimental condition.
9. **Buffer-first streaming.** V1 buffers internal generations until it knows the final externally visible response; it may then replay it as OpenAI-style SSE.
10. **Pin everything in evaluations.** Benchmark commit, harness, model snapshot, provider, sampling policy, context policy, proxy version, and prompts must be recorded.

## Compatibility boundary

The transparent persistence mechanism requires a caller that:

- sends the full/relevant conversation transcript on each request;
- round-trips assistant content into the next request;
- permits textual assistant content together with a tool call or action.

This is common but not universal. Strict JSON-only response parsers may reject prefixed XML-like content. ReasonProxy therefore defines three persistence modes:

| Mode | Behavior | Use |
|---|---|---|
| `assistant_tags` | Emits a signed or unsigned `<deliberation>` block in assistant content | Default research mode; free-form/tool-native agents |
| `ephemeral` | Uses the checkpoint only within the current HTTP request | Strict schemas; widest compatibility, weaker memory |
| `schema_adapter` | Embeds the checkpoint in a known allowed field | Opt-in integration for a specific harness/schema |

A system should never silently alter strict structured output. `auto` mode must detect the request contract conservatively and fall back to `ephemeral` when persistence is unsafe.

## Primary empirical claim to test

Not:

> Ensembles sometimes improve LLM answers.

That is established prior art.

The testable claim is:

> A generic, self-invoked, full-state trajectory-expansion tool can move frozen commodity-model agents onto a better success–cost–latency frontier than extra serial reasoning, ordinary helper delegation, fixed mixture-of-agents inference, or best-of-N complete rollouts.

The ambitious target is to substantially close the gap between inexpensive models such as GLM-5.3/Kimi K3 and frontier agents such as Claude Fable on difficult, fixed-harness coding and terminal benchmarks. Current leaderboard values are motivation only; every paper comparison must be rerun or explicitly identified as an external snapshot.

## Closest prior work warning

The nearest paper found in this review is **Second Thought: Reasoning in Parallel as LLM Agents Act and Observe** (Sun et al., arXiv:2608.13667, August 2026). It also forks full-trajectory continuations and reincorporates auxiliary thoughts. It makes broad claims such as “parallel reasoning during a trajectory” unavailable to us as novelty.

The proposed work must distinguish and empirically isolate:

- explicit, model-selected `reason()` invocation versus automatic forking after every thought/action;
- critical-path branch-and-collapse versus exploiting environment wait time;
- generic same-agent continuation and heterogeneous commodity endpoints versus four fixed auxiliary roles;
- learned/generative reduction into a checkpoint versus direct concatenation;
- persistent ordinary assistant messages versus appending thoughts to a tool observation;
- a stateless drop-in OpenAI-compatible inference transducer versus a modified agent harness.

See `02_LITERATURE_REVIEW.md` for the detailed comparison. Other mandatory adjacent work now covered there includes SpecCoT (parallel cheap reasoning-step drafts), Council Mode (triaged heterogeneous consensus), iMAD (learned selective debate), and SR²AM (self-regulated planning). Together they make adaptive compute allocation and matched-latency controls necessary, not optional.

## Recommended first run

Start with a deterministic 10-task DeepSWE pilot selected before viewing treatment outcomes:

```text
A  mini-SWE-agent + GLM-5.3 direct
B  same, routed through proxy, reason tool injected but disabled
C  same + one external branch
D  same + four homogeneous branches + reducer
E  same + heterogeneous cheap branches + reducer
F  same total-dollar/token budget spent as extra serial native reasoning
```

Use at least three independent trials in the pilot, inspect trajectories manually, repair only implementation defects, freeze V1, then run the full benchmark with a preregistered matrix.

## Research honesty

This bundle intentionally separates:

- **verified facts** about existing papers/APIs/benchmarks;
- **design decisions** proposed here;
- **hypotheses** that require experiments;
- **aspirational outcomes** such as matching Fable-class performance.

No source search can guarantee worldwide novelty, and this is not a patentability opinion. The architecture is publishable only if the empirical and systems contributions survive the controls in `04_BENCHMARK_PLAN.md`.
