# 02 — Literature Review, Prior Art, and Novelty Boundaries

## 1. Review scope and confidence

This review covers the closest work found through 2026-09-04 in six families:

1. explicit thinking tools and metareasoning;
2. recursive/sub-model inference scaffolds;
3. candidate sampling and reasoning search;
4. multi-model fusion and collaboration;
5. long-horizon agent test-time scaling;
6. asynchronous and mid-trajectory parallel cognition.

The broad design space is crowded. The literature strongly supports the plausibility of additional inference-time compute and parallel diversity, but it also sharply limits what this project can claim as new.

This is a serious prior-art review, not an exhaustive patent search or a guarantee that no unpublished/obscure implementation exists.

## 2. Taxonomy

ReasonProxy combines ideas that prior work usually studies separately:

| Axis | Representative prior work | ReasonProxy choice |
|---|---|---|
| Who decides to spend extra compute? | Fixed algorithm, learned gate, or main agent | Frozen controller invokes `reason()` as a tool |
| What state is delegated? | Explicit subquestion, prompt slice, completed solution, or full trajectory | Complete call-site trajectory; no meaningful question required |
| What do branches do? | Solve, critique, debate, inspect, or continue | Continue as counterfactual copies of the same live agent |
| When are branches launched? | Request ingress, after full rollout, each reasoning step, or environment wait | At arbitrary controller-selected points before the next outward commitment |
| How are branches merged? | Vote, rank, concatenate, debate, refine, or aggregate | Fast reducer emits a compact cognitive checkpoint |
| Where does merged state live? | Final answer, tool observation, hidden runtime memory | Ordinary assistant `<deliberation>` content replayed by the caller |
| What is modified? | Model weights, decoding algorithm, or agent harness | Stateless OpenAI-compatible endpoint; frozen models/harness |
| Do branches act? | Sometimes full environment rollouts | No; cognition only, one action authority |

The research contribution must be evaluated as this combination, not as any individual ingredient.

## 3. Explicit thinking tools

### 3.1 Anthropic’s `think` tool

Anthropic described a `think` tool for tool-using Claude agents in March 2025. The tool gives the model an explicit place to pause and reason during a multi-step tool-use trajectory. It is most useful when the model must process tool outputs carefully or follow complex policies.

**Similarity**

- A model chooses to invoke a cognitive action inside an agent loop.
- The action is side-effect-free and supports better next-step decisions.
- The interface can be extremely simple.

**Difference**

- The tool does not outsource cognition to a collection of independent commodity endpoints.
- It is effectively a scaffold for the same model’s own reasoning.
- It does not define full-state trajectory branching and reduction.
- It does not provide the proposed stateless proxy/persistent checkpoint mechanism.

**Novelty consequence**

ReasonProxy cannot claim that an explicit reasoning tool is new. Its contribution must lie in what the tool invokes and how the result is integrated.

Source: Anthropic, “The ‘think’ tool: Enabling Claude to stop and think in complex tool use situations,” 2025. See `SOURCES.md`.

## 4. Recursive and delegated language-model inference

### 4.1 Recursive Language Models (RLMs)

Zhang, Kraska, and Khattab introduce RLMs as a task-agnostic scaffold for long-context inference. Rather than feeding an enormous prompt directly into one network, the scaffold places the prompt in an external REPL-like environment. The model programmatically inspects, decomposes, and recursively invokes LMs over selected prompt slices/subtasks.

**Similarity**

- Capability is supplied by the inference scaffold rather than weight changes.
- Ordinary public/local models can be composed through sub-LM calls.
- The overall system can present an LLM-like input/output interface.
- The controller decides when and how to allocate recursive inference.

**Difference**

- RLM’s central problem is long-context access and programmatic decomposition.
- The controller constructs explicit operations and subqueries over externalized context.
- ReasonProxy makes the complete current trajectory the implicit query.
- ReasonProxy’s controller need only emit `reason({})`; the proxy, not the controller, orchestrates branch fanout and reduction.
- ReasonProxy focuses on mid-trajectory agent decisions, not arbitrary-length prompt processing.

**Research lineage**

ReasonProxy should explicitly credit RLMs for the philosophy that model capability may be implemented as a general inference scaffold around frozen models. A clean framing is:

```text
RLM: externalize and recursively process context
ReasonProxy: externalize and parallelize deliberation at a live agent state
```

Source: Zhang et al., “Recursive Language Models,” arXiv:2512.24601, 2025.

### 4.2 Reproduction evidence and overthinking

“Think, But Don’t Overthink” reproduces RLM-style recursion using open models and reports that depth-1 recursion can help difficult tasks while deeper recursion may sharply increase time/cost and hurt performance.

**Design implication**

V1 should be one-level and non-recursive. The outer controller may call `reason()` again only after integrating a result or receiving new environment evidence.

Source: Wang, “Think, But Don’t Overthink: Reproducing Recursive Language Models,” arXiv:2603.02615, 2026.

### 4.3 LM-Guided Chain-of-Thought

Lee et al. use a lightweight LM to generate a rationale that is supplied to a larger black-box LM. The result demonstrates that reasoning material generated by a weaker model can sometimes improve a stronger frozen model.

**Similarity**

- Reasoning is generated externally and consumed by another model.
- Models need not share tokenizer, logits, or hidden states.
- The approach is compatible with black-box APIs.

**Difference**

- The rationale generator is not a selectively called multi-branch tool in a live agent trajectory.
- There is no general stateless proxy or action loop.
- The direction is often small model rationale → large answer model, whereas ReasonProxy may use a commodity controller and a commodity ensemble.

Source: Lee et al., “Can Small Language Models Help Large Language Models Reason Better?: LM-Guided Chain-of-Thought,” LREC-COLING 2024.

### 4.4 Co-LLM

Co-LLM models which LM should generate the next token as a latent variable and trains a base LM to invoke assistant models at token granularity.

**Similarity**

- Reasoning/generation capability need not be contained in one model.
- A controller can learn when to rely on other language models.
- Collaboration may outperform individual constituents.

**Difference**

- Co-LLM requires training and token-level integration.
- ReasonProxy works with black-box, independently tokenized APIs.
- The delegation unit is a semantic tool action and a whole continuation, not the next token.

Source: Shen et al., “Learning to Decode Collaboratively with Multiple Language Models,” ACL 2024.

### 4.5 SpecCoT

SpecCoT combines a large reasoning model with a smaller draft model at the level of reasoning steps. The large model establishes the current reasoning direction; the small model proposes multiple candidate next steps in parallel; and the large model accepts one candidate or rejects them and produces its own step. The authors report 1.7–4.1× lower inference latency while maintaining accuracy comparable to ordinary large-model inference across their evaluated tasks.

**Similarity**

- Cheap parallel generations can supply useful candidate cognition to a more capable continuing model.
- The useful unit of collaboration can be a reasoning step rather than a token or complete final answer.
- Parallel breadth may reduce serial inference latency when drafts are accepted.

**Difference**

- SpecCoT is a tightly coupled draft–verify decoding procedure, not a self-invoked tool inside an arbitrary agent trajectory.
- The large model supplies the direction and verifies every speculative step; ReasonProxy snapshots the complete live state, collapses bounded continuations once, and returns a checkpoint to an otherwise ordinary controller.
- ReasonProxy targets independently served black-box chat endpoints and third-party agent harnesses.

**Evaluation consequence**

SpecCoT strengthens the need for a matched-compute and matched-latency comparison against parallel cheap drafts. A gain cannot be attributed merely to using a small model to generate multiple candidate thoughts.

Source: Shi et al., “SpecCoT: Accelerating Chain-of-Thought Reasoning through Speculative Exploration,” Findings of EMNLP 2025.

## 5. Sampling and search over reasoning

### 5.1 Self-consistency

Self-consistency samples multiple reasoning paths and selects the most consistent answer. It established that independent stochastic reasoning paths can outperform a single greedy chain.

**Similarity**

- Parallel samples provide breadth and error diversity.
- Same-model multisampling is a meaningful baseline and potentially a cost-efficient implementation because of prefix caching.

**Difference**

- Self-consistency is generally applied to a complete answer at request level.
- It usually aggregates by answer consistency/voting rather than returning a persistent checkpoint to a continuing agent.
- The branch point is fixed by the inference algorithm rather than selected as an action in a trajectory.

Source: Wang et al., “Self-Consistency Improves Chain of Thought Reasoning in Language Models,” ICLR 2023 / arXiv:2203.11171.

### 5.2 Tree of Thoughts (ToT)

ToT treats intermediate thoughts as search states, explores multiple paths, evaluates states, and supports lookahead/backtracking.

**Similarity**

- A linear generation is expanded into a branching search process.
- Alternative reasoning trajectories can repair premature commitment.

**Difference**

- ToT is an explicit search algorithm over thoughts.
- ReasonProxy exposes branching as an optional tool action inside an otherwise ordinary agent model call.
- V1 has one expansion-and-collapse step rather than a persistent search tree.

Source: Yao et al., “Tree of Thoughts: Deliberate Problem Solving with Large Language Models,” NeurIPS 2023 / arXiv:2305.10601.

### 5.3 Graph of Thoughts (GoT)

GoT generalizes thought search to arbitrary graphs and operations such as aggregation and refinement.

**Similarity**

- Multiple thought units can be aggregated into a new cognitive state.
- Reasoning need not remain a single sequence.

**Difference**

- GoT is a general graph-based prompting/inference framework.
- ReasonProxy’s public trajectory remains linear; only the internal inference for a selected action becomes a temporary DAG.
- The endpoint abstraction and stateless persistence mechanism are different contributions.

Source: Besta et al., “Graph of Thoughts: Solving Elaborate Problems with Large Language Models,” AAAI 2024 / arXiv:2308.09687.

### 5.4 Multi-agent debate

Multi-agent debate has several models propose and critique answers over rounds, often improving factuality/reasoning.

**Similarity**

- Multiple independent models can expose errors and alternatives.
- Heterogeneous model families may reduce correlated failure.

**Difference**

- Debate involves explicit communication rounds and often high sequential latency.
- ReasonProxy workers do not communicate and V1 has a single parallel fanout plus one reducer.
- Workers are trajectory continuations, not named debating agents.

Source: Du et al., “Improving Factuality and Reasoning in Language Models through Multiagent Debate,” ICML 2024.

## 6. Multi-model fusion

### 6.1 LLM-Blender

LLM-Blender introduces a PairRanker to compare candidates and a GenFuser to synthesize them.

**Similarity**

- Black-box outputs from different models are generatively fused.
- A reducer can outperform majority voting by preserving complementary content.

**Difference**

- The target is generally final response generation.
- There is no controller-selected mid-trajectory cognitive tool or same-agent call-site semantics.

Source: Jiang et al., “LLM-Blender: Ensembling Large Language Models with Pairwise Ranking and Generative Fusion,” ACL 2023.

### 6.2 Mixture-of-Agents (MoA)

MoA structures multiple layers of LLM agents; each layer receives outputs from the preceding layer and generates improved responses.

**Similarity**

- Heterogeneous model collaboration and an aggregator can improve quality.
- Public endpoints can be composed without shared weights.

**Difference**

- MoA is normally a fixed inference architecture applied to the request/final answer.
- ReasonProxy’s expansion is optional, selected by the running agent, and returns cognition rather than the final external answer.
- The existing harness remains unaware of the internal ensemble.

Source: Wang et al., “Mixture-of-Agents Enhances Large Language Model Capabilities,” arXiv:2406.04692, 2024.

### 6.3 LLM councils and production systems

Open-source systems such as Karpathy’s `llm-council` and several MCP “council” servers fan prompts to multiple LLMs and synthesize their outputs.

**Similarity**

- An outer application can invoke a council as a callable capability.
- The implementation can be provider-agnostic.

**Difference**

- Councils usually receive an explicit prompt/question.
- They generally return an answer/advisory report, not a full-state continuation checkpoint.
- They are not necessarily transparent model proxies or self-selected trajectory operators.

**Novelty consequence**

The paper must not claim “we expose an ensemble as a tool.” That has ample system prior art.

### 6.4 Council Mode

Council Mode implements request-level heterogeneous ensemble inference as a three-stage pipeline: complexity triage, parallel expert generation, and a dedicated consensus model that identifies agreements, disagreements, and unique findings before producing a final response. The paper reports a 35.9% relative reduction in hallucinations on HaluEval and a 7.8-point TruthfulQA improvement over its best individual model.

**Similarity**

- Heterogeneous public models are invoked in parallel.
- A generative reducer is instructed to preserve disagreement and unique evidence rather than merely vote.
- Adaptive triage avoids paying ensemble cost for every query.

**Difference**

- Council Mode operates at request/final-answer level rather than as a callable branch point within a continuing tool-use trajectory.
- Its experts answer the query as a council; ReasonProxy branches from the complete call-site state as counterfactual continuations of the same acting agent.
- The consensus is the user-facing answer, whereas ReasonProxy’s reduced output is intermediate cognitive state that the authoritative controller may accept, revise, or reject before acting.

Source: Wu et al., “Council Mode: Mitigating Hallucination and Bias in LLMs via Multi-Agent Consensus,” arXiv:2604.02923, 2026.

## 7. Long-horizon agent test-time scaling

### 7.1 Scaling Test-Time Compute for Agentic Coding

Kim et al. study parallel long-horizon coding rollouts, structured summaries of progress/failures, recursive tournament voting, and parallel-distill-refine methods.

**Similarity**

- Parallel inference improves long-horizon coding systems.
- Summaries can compress useful state from multiple trajectories.
- Compute allocation, not just model weights, drives capability.

**Difference**

- Parallelism is over substantial/full agent rollouts from the task.
- ReasonProxy forks only cognition at selected states inside one authoritative trajectory.
- Workers do not mutate independent repositories or environments.
- ReasonProxy is designed to be far cheaper and easier to insert behind an endpoint.

Source: Kim et al., “Scaling Test-Time Compute for Agentic Coding,” arXiv:2604.16529, 2026.

### 7.2 AggAgent

AggAgent exposes parallel long-horizon trajectories as an environment that an aggregation agent can inspect with tools and synthesize.

**Similarity**

- Aggregation is itself an agentic information-processing problem.
- Long trajectories require selective inspection/condensation.

**Difference**

- AggAgent aggregates completed or ongoing action trajectories.
- ReasonProxy’s branch workers have no environment forks and generate only cognitive continuations.
- The reducer receives small bounded continuations rather than navigating large rollout stores.

Source: “Agentic Aggregation for Parallel Scaling of Long-Horizon Agentic Tasks,” arXiv:2604.11753 / COLM 2026.

### 7.3 Parallel Environments for Agents

Parallel-environment work lets agents fork isolated world states, take alternative actions, and compare outcomes.

**Similarity**

- A single problem trajectory can branch mid-course.
- Alternative futures may prevent local mistakes.

**Difference**

- Environment state and actions are forked.
- ReasonProxy forks cognition only and leaves one environment/action authority.
- Its branches are cheap API calls rather than full sandbox copies and tool trajectories.

**Research value of the difference**

A cognition-only fork may deliver some search benefit without multiplying environment costs or creating state reconciliation problems.

## 8. Asynchronous and parallel agent cognition

### 8.1 Second Thought — nearest prior work

Sun et al.’s **Second Thought: Reasoning in Parallel as LLM Agents Act and Observe** is the closest work found.

The paper identifies a ReAct “reasoning idle window” after a thought concludes while the main thread serializes an action and waits for its observation. It automatically forks four auxiliary branches—Check, Recall, Rehearse, and Alternative—from the complete trajectory. The branches share the prompt prefix, emit interruption-safe XML-tagged atomic thoughts, and are harvested/concatenated into the tool-observation message for the next turn.

The paper evaluates SWE-Bench Pro, Terminal-Bench 2.1, and a tool-calling dialogue benchmark with three reasoning models. It reports lower turn counts in all tested model–benchmark pairs, reductions in main-thread decoding in most settings, and statistically significant Pass@1 improvements in two of nine pairs. A compute-matched serial-reasoning control is also included.

### 8.1.1 Overlap with ReasonProxy

Second Thought already contains several ideas that are central to our design:

- training-free inference-time augmentation;
- full trajectory snapshot rather than explicit problem handoff;
- branch generation as continuation of the same model state;
- parallel auxiliary cognition during an agent trajectory;
- prompt-prefix/KV-cache reuse;
- XML-delimited cognitive units;
- reinserting auxiliary cognition into future context;
- agentic coding and terminal evaluations;
- compute-matched serial reasoning controls.

These cannot be claimed as independently novel.

### 8.1.2 Material differences

| Dimension | Second Thought | ReasonProxy |
|---|---|---|
| Trigger | Automatically after each completed Thought | Explicit `reason()` action chosen by controller at arbitrary points |
| Compute window | Action/observation idle time | Current request’s deliberation critical path before outward commitment |
| Effect on current action | Cannot alter already-issued current action | Designed specifically to alter the next outward action |
| Branch semantics | Four fixed complementary roles | Minimal same-agent continuations by default; roles are ablations |
| Branch models | Same reasoning model/state in reference design | Homogeneous samples or heterogeneous commodity endpoints |
| Branch deadline | Observation arrival; interruption-safe atomic units | Configured timeout/quorum; full compact continuations |
| Merge | Truncate and concatenate thoughts | Fast reducer reconciles branches into one checkpoint |
| Persistence location | Appended to tool-observation message | Ordinary assistant `<deliberation>` content |
| Integration | Agent-loop/harness modification | Stateless OpenAI-compatible proxy; harness need not know |
| External actions | Main ReAct loop already acted before branches | Controller is re-invoked after branch result, before real action is exposed |
| Central question | Use idle wall-clock capacity | Selectively outsource difficult reasoning through a generic capability |

### 8.1.3 Required comparison

A credible study should implement a Second-Thought-like baseline in the same evaluation stack:

- automatic branching after every externally visible action;
- four Check/Recall/Rehearse/Alternative prompts;
- direct concatenation or its documented harvesting method;
- equivalent worker-token budget where possible.

If ReasonProxy does not beat or complement this baseline, the novelty and practical case weaken substantially.

### 8.1.4 Potential complementarity

The methods are not mutually exclusive:

- Second Thought exploits otherwise idle environment latency.
- ReasonProxy spends explicit critical-path compute only when the controller judges it useful.

A future hybrid could use automatic idle-window branches opportunistically and `reason()` for difficult decision points. This should be considered after the clean V1 comparison.

Source: Sun et al., “Second Thought: Reasoning in Parallel as LLM Agents Act and Observe,” arXiv:2608.13667, 2026.

### 8.2 Sleep-time compute

Sleep-time compute precomputes useful intermediate information over a static context before queries arrive.

**Similarity**

- Deliberation can be moved outside the immediate answer-generation path.
- Computed state can improve later queries.

**Difference**

- The context is static and precomputation occurs before the query.
- ReasonProxy is invoked online at a particular live trajectory state.

Source: Lin et al., “Sleep-time Compute: Beyond Inference Scaling at Test-time,” arXiv:2504.13171, 2025.

### 8.3 Asynchronous function calling and speculative execution

Recent systems overlap tool execution, model generation, or speculative future actions to reduce wall-clock latency.

**Similarity**

- Agent latency is treated as a systems scheduling problem.
- Parallel work can be hidden behind external waits.

**Difference**

- These systems often reschedule computation that the main trajectory would perform anyway or speculate on future actions.
- ReasonProxy intentionally purchases additional independent cognitive content and merges it before the action is committed.

These systems remain relevant to later latency optimization, especially speculative prefetch of likely reason calls.

## 9. Related metareasoning and adaptive allocation

A separate literature studies deciding whether more debate, search, planning, or reasoning is worth its cost. Even where methods differ, it motivates the decision rule:

\[
\text{invoke reason at } s_t \quad \text{iff}\quad
\mathbb E[\Delta Q\mid s_t] > \lambda C + \mu T.
\]

### 9.1 iMAD

Intelligent Multi-Agent Debate (iMAD) observes that debate can waste tokens and can overturn an already-correct single-agent answer. It first elicits a structured self-critique, extracts interpretable hesitation-related features, and uses a lightweight learned classifier to trigger debate only when it predicts that debate will help. Across six visual/question-answering datasets, the authors report up to 92% lower token use and up to 13.5% higher final accuracy relative to evaluated alternatives.

**Similarity**

- Additional multi-model inference is optional rather than mandatory.
- The central systems problem is estimating the value of more reasoning before paying for it.
- More collective reasoning can sometimes hurt, so a no-op/control path is essential.

**Difference**

- iMAD uses a separately trained gate over an initial answer/self-critique and then launches structured debate on question answering.
- ReasonProxy V1 asks the frozen live controller itself to choose a `reason()` action at arbitrary points in a long-horizon trajectory.
- ReasonProxy’s workers do not debate and do not need an explicit question payload; they branch from the current agent state.

Source: Fan, Yoon, and Ji, “iMAD: Intelligent Multi-Agent Debate for Efficient and Accurate LLM Inference,” AAAI 2026.

### 9.2 SR²AM

SR²AM decomposes agentic reasoning into reactive execution (System I), simulative planning (System II), and self-regulation (System III) that determines when and how deeply to plan. Its components are realized as distinct stages inside an LLM’s chain of thought and trained with supervised learning and reinforcement learning. The authors report competitive results with much larger systems and 25.8–95.3% fewer reasoning tokens than comparable agentic LLMs in their evaluated settings.

**Similarity**

- It makes the fast-controller/expensive-deliberator boundary explicit.
- It treats the decision to deliberate—and the depth of deliberation—as a first-class learned policy.
- It supports the view that metareasoning can matter as much as the planner itself.

**Difference**

- SR²AM modifies/trains the model and realizes the systems internally in its reasoning trace.
- ReasonProxy aims to instantiate the boundary physically at runtime with unmodified black-box endpoints: controller, external branch fabric, and reducer.
- V1 deliberately tests whether prompted self-invocation works before introducing a trained gate.

Source: Deng et al., “Efficient Agentic Reasoning Through Self-Regulated Simulative Planning,” arXiv:2605.22138, 2026.

### 9.3 Consequences for ReasonProxy

V1 relies on prompted model judgment. Later work may train a call policy or use a lightweight gate. Any learned selector must be compared with:

- self-selection by the frozen controller;
- random calls;
- fixed intervals;
- difficulty-classifier calls;
- iMAD-style learned gating;
- oracle calls derived from counterfactual rollouts.

The metareasoning policy may ultimately be a larger contribution than the raw ensemble. The strongest long-term framing is not merely “parallel thoughts help,” but “a controller can learn to purchase the appropriate amount and kind of external cognition.”

## 10. Novelty matrix

Legend: ✓ central; ~ partial/related; — absent or not central.

| Work | Frozen black-box models | Agent-selected call | Full live state implicit | Same-agent continuation | Parallel branches | Generative collapse | Persists in trajectory | Stateless model proxy |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Anthropic think tool | ✓ | ✓ | ✓ | ✓ | — | — | ~ | — |
| RLM | ✓ | ✓ | ~ | — | ~ | task-dependent | ~ | LLM-like scaffold |
| LM-Guided CoT | ✓ | — | — | — | — | — | rationale input | — |
| Co-LLM | — (trained) | learned | token context | token collaboration | ~ | learned decoding | generated text | — |
| SpecCoT | ~ | — | reasoning-step state | speculative step continuation | ✓ | verify/select | accepted reasoning step | — |
| Self-consistency | ✓ | — | request state | independent solutions | ✓ | vote | final answer | possible wrapper |
| ToT/GoT | ✓ | algorithmic | search state | thought branches | ✓ | evaluate/aggregate | search graph | — |
| LLM-Blender | ✓ | — | explicit prompt | candidate answers | ✓ | ✓ | final answer | possible wrapper |
| MoA | ✓ | — | explicit prompt | layered answers | ✓ | ✓ | final answer | possible wrapper |
| Council Mode | ✓ | triage gate | explicit request | expert answers | ✓ | ✓ | final answer | service, not transparent proxy |
| iMAD | ~ (trained gate) | learned gate | initial answer/critique | debating agents | ✓ | debate/finalize | final answer | — |
| SR²AM | — (trained) | learned self-regulation | internal trajectory | internal planning | — | internal integration | chain of thought | — |
| Agentic coding TTC | ✓ | algorithmic | task/rollouts | full agents | ✓ | ✓ | rollout summaries | — |
| Parallel environments | ✓ | often agent/algorithm | world state | full agents | ✓ | select/merge | environment trajectory | — |
| Second Thought | ✓ | automatic | ✓ | ✓ | ✓ | concatenation | tool observation | — |
| **ReasonProxy** | ✓ | **✓** | **✓** | **✓** | **✓** | **✓** | **assistant checkpoint** | **✓** |

No single cell is enough for novelty. The contribution is the operational combination plus empirical evidence.

## 11. Claims the paper may make only after evidence

### Potentially supportable

- A zero-argument full-state reason tool improves frozen commodity agents.
- Self-selected trajectory expansion allocates compute more efficiently than fixed branching.
- Parallel breadth beats matched serial reasoning on selected agentic tasks.
- A reducer checkpoint is more useful per context token than branch concatenation.
- Persistent assistant-state checkpoints improve long-horizon performance.
- A stateless proxy reproduces the benefit across otherwise unmodified harnesses.
- Cheap homogeneous/heterogeneous endpoints close a measured portion of a frontier-agent gap.

### Avoid or qualify

- “First external reasoning tool.”
- “First model ensemble exposed as a tool.”
- “First parallel reasoning for agents.”
- “First full-trajectory branch.”
- “First use of XML thought blocks.”
- “External deliberation is superior to internal reasoning.”
- “Matches Fable” based only on different public leaderboard harnesses.
- “Model X score” when the evaluated system includes multiple hidden model calls.

## 12. Recommended paper positioning

### Weak positioning

> We ask several LLMs to think and summarize them behind a tool.

This will be dismissed as a wrapper around MoA/councils.

### Better positioning

> We study whether reasoning itself can be supplied as a stateless runtime capability to frozen agents. We introduce a trajectory-expansion operator whose input is the full call-site state rather than a delegated question, and evaluate whether self-selected branch-and-collapse inference improves the capability–cost–latency frontier on long-horizon tasks.

### Strongest positioning if results support it

> The effective reasoning capacity of an agent need not be contained in its controller model. A frozen commodity controller can allocate external parallel cognition at selected states and integrate it through its ordinary transcript, approaching frontier-agent performance without model training or harness modification.

## 13. Research gaps that remain open

Even after the adjacent work, several questions are not settled:

1. Can already-shipped models reliably decide when to call an external cognition tool from a description alone?
2. Does a payload-free full-state interface outperform explicit delegation?
3. Is same-agent continuation better than specialist roles at the same budget?
4. Does generative collapse outperform raw thought concatenation on long trajectories?
5. Does public assistant persistence matter enough to justify format complexity?
6. Can heterogeneous cheap endpoints beat homogeneous multisampling after accounting for cache locality?
7. How much native reasoning should a controller retain when external deliberation is available?
8. Does the mechanism transfer across coding, terminal, interactive reasoning, and non-agentic tasks?
9. Can a stateless endpoint preserve the benefit across multiple third-party harnesses?
10. Can it outperform parallel full rollouts per dollar and per unit of wall-clock time?

These questions define the actual research program.

## 14. Minimum related-work set for every draft

The manuscript should directly discuss at least:

- ReAct;
- self-consistency;
- Tree of Thoughts and Graph of Thoughts;
- multi-agent debate;
- LLM-Blender;
- Mixture-of-Agents;
- Anthropic think tool;
- LM-Guided CoT;
- Co-LLM;
- SpecCoT;
- Council Mode;
- iMAD;
- SR²AM;
- Recursive Language Models;
- agentic coding test-time scaling;
- AggAgent;
- parallel environments;
- sleep-time compute;
- Second Thought.

Omitting Second Thought would make the novelty discussion misleading.

## 15. Bottom-line assessment

### Architectural novelty

- Broad ensemble reasoning: low.
- Ensemble as callable tool: low.
- Full-trajectory continuation: no longer independently novel because of Second Thought and neighboring work.
- The exact stateless, self-invoked, pre-action branch-and-collapse endpoint with assistant-checkpoint persistence: plausibly distinct.

### Publishability

- Implementation plus small isolated gains: likely workshop/demo/system note.
- Consistent gains across controllers and fixed-harness benchmarks with matched-compute ablations: strong paper.
- Substantial commodity-to-frontier gap closure at lower cost: potentially high impact.

The empirical phenomenon must carry the paper. Wording alone cannot turn the mechanism into a contribution.
