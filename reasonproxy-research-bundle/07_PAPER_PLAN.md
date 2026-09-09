# 07 — Paper Plan, Claims, and Publication Strategy

## 1. Working titles

Primary:

> **Externalized Deliberation: A Stateless Branch-and-Collapse Reasoning Tool for Frozen Language-Model Agents**

Alternatives:

- **ReasonProxy: Selective Parallel Deliberation for Commodity Language-Model Agents**
- **The State Is the Query: Full-Trajectory External Reasoning for Frozen LLM Agents**
- **Branch, Collapse, Act: Outsourcing Agent Reasoning Through a Stateless Model Proxy**
- **Reasoning as a Tool: Frontier Agentic Performance from Frozen Commodity Models** — use only if results justify “frontier.”

Do not put “frontier performance,” “better than Fable,” or “superior reasoning” in the title before the data supports it.

## 2. One-paragraph pitch

Language-model agents normally perform deliberation and action selection in one serial model continuation. We introduce a model-agnostic inference operator, exposed as a nearly zero-argument `reason()` tool, that lets a frozen controller selectively externalize difficult cognition. At a self-selected point in its live trajectory, a stateless proxy captures the complete conversation and tool history, forks several ordinary commodity LLM continuations as the same agent, reduces them into a compact cognitive checkpoint, feeds the checkpoint back to the controller, and emits it in a normal assistant message for future context. No model weights, benchmark environment, or external agent tools are changed. We evaluate whether this operation improves the success–cost–latency frontier of coding and terminal agents relative to direct inference, additional serial reasoning, explicit helper delegation, fixed mixture-of-agents inference, full-rollout test-time scaling, and the closest mid-trajectory parallel-reasoning baseline.

## 3. Core research questions

### RQ1 — Capability

Does self-invoked externalized deliberation improve long-horizon task success for frozen commodity controllers under a fixed agent harness?

### RQ2 — Resource efficiency

At equal or comparable dollar, token, and latency budgets, does branch-and-collapse deliberation outperform additional serial native reasoning?

### RQ3 — Interface

Does a zero/minimal-argument full-state tool outperform explicit question-based delegation?

### RQ4 — Scheduling

Does the controller's choice of when to invoke deliberation outperform automatic, periodic, random, or simple error-triggered invocation at matched call counts?

### RQ5 — Branch composition

How much gain comes from independent same-model samples versus heterogeneous model families or fixed cognitive roles?

### RQ6 — Reduction and persistence

Are generative cognitive checkpoints and transcript persistence necessary, or is raw concatenation/ephemeral advice sufficient?

### RQ7 — Substitution versus complementarity

Can low native reasoning plus external deliberation replace high native reasoning, or is external deliberation useful mainly as a complement?

### RQ8 — Failure regimes

When does the method hurt due to correlated branches, reducer loss, overthinking, poor call placement, context growth, or latency?

## 4. Exact contribution claims

The paper may claim the following **only if implemented and supported**.

### C1 — A precise inference primitive

A self-invoked, nearly payload-free trajectory-expansion operator:

\[
C_t \xrightarrow{\operatorname{REASON}}
\{z_1,\dots,z_K\}
\xrightarrow{g}
D_t
\xrightarrow{\pi}
a_t.
\]

The full call-site trajectory, rather than an explicit delegated question, specifies the reasoning problem.

### C2 — Stateless deployment boundary

An implementation as an OpenAI-compatible request-to-response transducer requiring no server-side conversation state. Compact deliberative state persists through ordinary assistant messages when the caller replays them.

### C3 — Commodity-model composition

The controller, branches, and reducer can be already-shipped black-box endpoints. No logits, shared tokenizer, hidden states, or fine-tuning are required.

### C4 — Controlled empirical evaluation

A fixed-harness study separating gains from:

- prompt/tool injection;
- extra compute;
- serial reasoning;
- one helper;
- homogeneous and heterogeneous branches;
- always-on MoA;
- complete-rollout sampling;
- automatic mid-trajectory branching;
- checkpoint reduction and persistence.

### C5 — Empirical frontier or negative characterization

If results are strong, claim a better task-success/cost/latency frontier. If not, claim a careful characterization of where externally composed cognition succeeds and fails. Do not force a positive headline unsupported by the study.

## 5. Claims that are unavailable as novelty

The related literature already establishes or explores:

- sampling multiple reasoning paths;
- tree/graph branching of language-model thoughts;
- multi-agent debate and model councils;
- candidate ranking and fusion;
- mixture-of-agents layers;
- token-level collaboration between LMs;
- recursive/sub-LM calls from an inference harness;
- scaling complete agent rollouts at test time;
- summarizing and reusing long agent trajectories;
- automatic auxiliary reasoning branches during action/observation idle windows;
- simple think tools during tool use.

Therefore the paper must not say:

> “We are the first to use multiple LLMs for agent reasoning.”

or:

> “We are the first to branch agent trajectories.”

or:

> “We are the first to expose an LLM council as a tool.”

The defensible novelty is the combination and semantics of **explicit controller-selected invocation, zero/minimal payload, full call-site same-agent continuations, branch reduction into persistent assistant state, and stateless endpoint deployment**, plus whatever empirical phenomenon the experiments establish.

## 6. Closest-work positioning

## 6.1 Second Thought

The closest paper found is:

> Zhensu Sun, Chengran Yang, Yunbo Lyu, Jieke Shi, and David Lo. *Second Thought: Reasoning in Parallel as LLM Agents Act and Observe.* arXiv:2608.13667, 2026.

It forks four auxiliary thoughts after each main Thought, generates them in parallel with action/environment waiting, and merges them back with the next observation. It is training-free, uses full trajectory context, and evaluates coding/terminal/interactive agent tasks. It removes broad novelty claims around “parallel reasoning inside an agent trajectory.”

Mandatory distinctions to test rather than merely assert:

| Dimension | Second Thought | ReasonProxy proposal |
|---|---|---|
| Invocation | Automatic after each thought boundary | Explicit action selected by controller |
| Timing | Exploits action/observation idle window | On critical path before next visible action |
| Effect | Primarily informs future turn after observation | Can change the current next action |
| Branch roles | Four fixed auxiliary roles | Natural same-agent continuations by default |
| Models | Same-model branches in reported design | Homogeneous or heterogeneous public endpoints |
| Merge | Auxiliary thoughts appended/merged with observation | Reducer creates compact checkpoint |
| Persistence | Added around tool observation | Ordinary assistant `<deliberation>` state |
| Integration | Agent-loop framework change | Stateless model endpoint/proxy |

At least one faithful or approximate Second Thought-style automatic-boundary baseline should be included on a compatible harness.

## 6.2 Recursive Language Models

RLMs treat long prompts as an external environment and let a model programmatically inspect/decompose context and recursively call LMs on pieces. Shared philosophy:

- capability moves into the inference runtime;
- frozen black-box models can use sub-LM inference;
- tool-like recursion can extend effective computation.

Difference:

- RLM controllers explicitly orchestrate context/subproblems;
- ReasonProxy treats the complete live trajectory as the implicit query and hides branch orchestration behind one operation;
- the main target is long-horizon action selection, not primarily arbitrary long-context processing.

## 6.3 Test-time scaling for coding agents

Full-rollout methods generate multiple long trajectories and select, aggregate, or distill them. They are essential baselines because they may spend compute more effectively by exploring environment actions rather than cognition-only branches.

ReasonProxy's proposed advantage is local, selective branching without duplicating sandboxes or committing branch actions. This is a hypothesis, not an established fact.

## 6.4 Co-LLM, MoA, LLM-Blender, self-consistency, ToT/GoT, debate

These establish that model outputs can be sampled, routed, ranked, debated, fused, and composed. ReasonProxy should cite them as its inner computational lineage rather than trying to distinguish every implementation detail as a new algorithm.

## 6.5 Anthropic think tool

A think tool establishes a useful interface precedent inside tool-using trajectories. The key distinction is that ReasonProxy's tool invokes external independent inference rather than merely allocating a same-model thinking step.

## 7. Hypotheses

Predeclare directional hypotheses carefully.

### H1

At matched harness and controller, `reason()` increases task success over direct inference.

### H2

At matched cost or generated-token budget, parallel branch-and-collapse outperforms extra serial controller reasoning on at least one long-horizon benchmark.

### H3

Zero-argument full-state continuation performs at least as well as explicit question-based helper delegation, while reducing prompt burden on the controller.

### H4

Controller-selected invocation outperforms random/fixed invocation at matched reason-call count.

### H5

A compact reducer checkpoint outperforms raw branch concatenation at similar controller input budget.

### H6

Assistant-message persistence improves long-horizon performance over purely ephemeral deliberation.

### H7

Heterogeneous branches improve on homogeneous branches when model errors are sufficiently decorrelated, but may lose on cost/cache/latency.

### H8

The method has diminishing or negative returns beyond a modest branch/call budget due to overthinking, redundancy, and context growth.

## 8. Paper outline

### 1. Introduction

- Commodity models are strong but lag frontier agents on hard long-horizon tasks.
- Existing inference scaling either lengthens one serial trace, branches a fixed inference graph, or duplicates complete agent rollouts.
- Ask whether reasoning itself can be supplied as a generic capability to an unmodified agent.
- Introduce `reason()` and the state-is-the-query principle.
- Preview results only after confirmed.

### 2. Related Work

Organize by mechanisms:

1. serial reasoning and test-time compute;
2. sampled/tree/graph reasoning;
3. multi-model fusion and debate;
4. model collaboration/delegation;
5. recursive inference harnesses;
6. long-horizon agent rollout scaling;
7. mid-trajectory parallel reasoning and think tools;
8. speculative reasoning-step collaboration and selective metareasoning (SpecCoT, iMAD, SR²AM);
9. request-level heterogeneous councils and consensus systems.

Put Second Thought prominently, not in an appendix. Treat SpecCoT, Council Mode, iMAD, and SR²AM as mechanism-level comparators rather than burying them in a generic multi-agent paragraph.

### 3. Method

- formal agent state and action space;
- private REASON action;
- full-state branch rendering;
- worker constraints;
- reducer/checkpoint;
- resumed controller;
- assistant-message persistence;
- stateless endpoint interface;
- complexity, token, and latency accounting.

### 4. System

- OpenAI-compatible proxy;
- provider adapters;
- compatibility modes;
- caching and deadlines;
- provenance/security;
- telemetry.

Keep engineering details that affect correctness; move routine implementation to appendix.

### 5. Experimental Setup

- benchmarks and harnesses;
- controllers/workers/reducers;
- prompts and call budgets;
- condition matrix;
- cost/latency methodology;
- statistical plan;
- development/test separation.

### 6. Main Results

- task success by benchmark/controller;
- cost per success;
- latency and turn counts;
- Pareto curves;
- frontier-model comparisons under same harness.

### 7. Mechanism and Ablations

- explicit versus zero-arg;
- self-selected versus automatic/random;
- one/homogeneous/heterogeneous branches;
- reducer versus concatenation;
- ephemeral versus persistent;
- native reasoning effort interaction;
- Second Thought approximation;
- branch count/call budget scaling.

### 8. Trajectory Analysis

- cases where a checkpoint changes the next action;
- branch diversity and reducer retention;
- ignored correct insights;
- overthinking and distraction;
- call-placement errors;
- compaction/persistence effects.

Use concise excerpts rather than publishing raw private reasoning dumps.

### 9. Limitations and Ethics

- increased data exposure across providers;
- no guarantee of correctness from consensus;
- context/prompt injection risks;
- proprietary endpoint drift;
- compatibility limits;
- no proof that tags are hidden from end users;
- benchmarks may not predict production usefulness;
- larger compute footprint even when cheaper monetarily.

### 10. Conclusion

- reasoning can be treated as a runtime capability if evidence supports it;
- state precise boundaries and future work without claiming solved general intelligence.

## 9. Figures

### Figure 1 — One-trajectory branch-and-collapse

Show public harness trajectory, private reason call, parallel same-agent branches, reducer, checkpoint, and resumed action.

### Figure 2 — Stateless wire lifecycle

Show request transcript in, internal mini-loop, normal assistant response out, and replayed `<deliberation>` in next request.

### Figure 3 — Relationship to prior paradigms

Compare:

- serial CoT;
- fixed MoA;
- full-rollout best-of-N;
- Second Thought idle-window branches;
- self-selected ReasonProxy call.

### Figure 4 — Success–cost Pareto frontier

Each system configuration as a point/curve with confidence intervals.

### Figure 5 — Where reason calls occur

Distribution over agent turn, error events, progress stage, and outcome.

### Figure 6 — Information flow

Measure branch insights retained by reducer and later reflected in controller actions.

## 10. Tables

### Table 1 — Related-work comparison

Include invocation, branch state, action authority, reduction, persistence, training, deployment boundary.

### Table 2 — Main benchmark results

Rows: systems. Columns: success, cost, cost/success, total latency, turns, reason calls.

### Table 3 — Compute-matched controls

Serial versus external breadth at several budgets.

### Table 4 — Interface/scheduling ablations

Zero-arg versus question; selected versus automatic/random/fixed.

### Table 5 — Branch/reducer/persistence ablations

### Table 6 — Reliability and failure rates

Parser errors, provider failures, timeouts, malformed calls, context-limit failures.

## 11. Abstract template

Do not fill result placeholders until the frozen run is complete.

```text
Language-model agents usually perform deliberation and action selection in a single serial model continuation. We study whether deliberative capability can instead be supplied as a generic inference-time tool to an unmodified agent. We introduce ReasonProxy, a stateless model-side transformation that adds a nearly zero-argument `reason()` action. At a controller-selected call site, the proxy captures the complete live conversation and tool trajectory, forks K ordinary language-model continuations as the same agent, reduces them into a compact cognitive checkpoint, and resumes the original controller before exposing its next action. The checkpoint can be replayed through ordinary assistant messages, requiring neither model training nor server-side conversation state. On [BENCHMARKS], [COMMODITY MODELS] with ReasonProxy improve from [X] to [Y] task success at [COST/LATENCY], compared with [KEY BASELINES]. Ablations show [SUPPORTED MECHANISM CLAIMS]. These results [support/do not support] the hypothesis that external parallel inference can substitute for part of a controller's monolithic reasoning compute in long-horizon agents.
```

## 12. Reviewer objections to anticipate

### “This is just MoA behind a tool.”

Response requires data, not rhetoric:

- compare fixed MoA every turn;
- compare explicit helper delegation;
- show the effect of call-site selection and full-state zero-arg semantics;
- show long-horizon checkpoint persistence.

### “Second Thought already does mid-trajectory parallel reasoning.”

Agree with the overlap. Include it as closest prior art and compare automatic idle-window branches to explicit pre-action branch-and-collapse. Do not claim the shared idea as new.

### “Of course more compute helps.”

Provide equal-dollar, equal-token, and serial-compute controls; show Pareto curves.

### “The prompt tells the model to behave better.”

Include injection/no-op control and publish exact prompts.

### “You changed the agent harness.”

Primary result must use the same harness commit/config and only change the endpoint. Document any compatibility adapter symmetrically.

### “The reducer is simply the stronger model.”

Use a cheap/fast reducer, compare reducer sizes, raw concatenation, and controller-sees-all. Report all model identities and costs.

### “The branches may leak hidden benchmark data.”

Demonstrate that branches receive only controller-visible messages and tools; keep hidden verifier/reference artifacts outside the proxy request.

### “Assistant tags are not really hidden reasoning.”

Agree. Call them persistent cognitive checkpoints in ordinary assistant content. They may be visible to clients/users unless the UI filters them.

### “Stateless is marketing; you still have caches/logs.”

Define semantic statelessness precisely: no cross-request session state participates in correctness. Operational caches and telemetry do not reconstruct conversations.

### “Public APIs changed during the experiment.”

Pin aliases/snapshots when possible, interleave conditions, record dates and calibration tasks, and disclose unpinned provider drift.

### “It only works because the controller already knows when it is wrong.”

That is an important limitation. Compare heuristic/oracle/random placement and analyze missed calls. A trained metareasoning policy is future work.

## 13. Publication bar

### Architecture/demo paper

A correct proxy plus qualitative examples and small gains could support a systems demonstration or workshop submission. It is unlikely to establish a strong main-track result by itself because the components have extensive prior art.

### Solid empirical paper

Consistent gains on two fixed-harness benchmarks, matched-compute controls, a direct Second Thought comparison, two controller families, and careful resource accounting could be publishable even without matching Fable.

### Strong main-track result

The strongest outcome would combine:

- large task-success gains;
- a better dollar/latency frontier than obvious alternatives;
- substantial closure of the commodity-to-frontier gap;
- replication across coding and terminal/interactive tasks;
- mechanism evidence for self-selected full-state branching;
- an open, reproducible proxy implementation.

### Falsifying/negative result

A thorough negative result may still be valuable if it shows that apparent local reasoning improvements fail to translate into end-to-end success and identifies why. Its publishability depends on breadth, controls, and insight rather than a product claim.

## 14. Preregistration skeleton

Before the full run, freeze a public or timestamped document containing:

- primary benchmark, task set, and commits;
- primary controller and provider version;
- treatment configuration;
- primary baseline and metric;
- trial count;
- exclusion rules for infrastructure failures;
- statistical interval/test;
- resource accounting method;
- development tasks used;
- secondary analyses;
- conditions that trigger stopping or rerun.

A practical primary hypothesis:

> On the full DeepSWE task set under a pinned mini-SWE/Pier configuration, ReasonProxy with controller-selected zero-argument calls and four branch continuations improves paired task success over the direct controller, while reporting all internal inference and remaining below a predeclared cost-per-task ceiling.

## 15. Release artifacts

A credible paper release should include:

- ReasonProxy source;
- Docker/container deployment;
- exact prompts and configs;
- provider adapter contract tests;
- benchmark command manifests;
- task split/preregistration;
- aggregate and per-task outcomes;
- raw public trajectories where licenses permit;
- redacted internal branch/reducer traces;
- cost/latency event logs;
- analysis scripts;
- known failures and reproduction instructions.

Do not release API keys, secrets, hidden tests, licensed task artifacts, or private provider reasoning fields.

## 16. Longer-term research directions

Only after V1 evidence:

- train a lightweight controller specifically for `reason()` metareasoning;
- learn task-conditioned worker selection and stopping;
- use objective verifiers inside the cognitive service;
- allow branches read-only tools;
- speculate/prefetch likely reason calls;
- update/compress old checkpoints through harness-aware protocols;
- compare reasoning capsules with latent/recurrent memory;
- distill successful external deliberation into smaller controllers;
- study whether repeated use teaches an in-context call policy;
- generalize beyond LLM workers to code, search, theorem provers, and simulators.

These are not needed to establish the basic frozen-model inference result.
