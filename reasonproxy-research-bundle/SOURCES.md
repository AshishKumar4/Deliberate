# Sources and Verification Notes

**Research snapshot:** 2026-09-04 (America/Chicago)  
**Policy:** Prefer papers, official benchmark repositories, and official provider/API documentation. Current prices and leaderboard values are dated snapshots, not timeless constants.

This catalog is intentionally broader than `REFERENCES.bib`: it includes software repositories, benchmark instructions, API contracts, and provider pricing pages that are not conventional academic references.

## 1. Closest and most important prior work

### Second Thought: Reasoning in Parallel as LLM Agents Act and Observe

- Zhensu Sun, Chengran Yang, Yunbo Lyu, Jieke Shi, David Lo. 2026.
- arXiv:2608.13667.
- https://arxiv.org/abs/2608.13667
- Importance: the closest paper found. It forks four auxiliary reasoning branches after each ReAct thought, generates them during action/observation idle time, and reincorporates them into later context. It evaluates SWE-Bench Pro, Terminal-Bench 2.1, and τ3-bench.
- Consequence for novelty: the proposed paper cannot claim that parallel auxiliary reasoning inside a live agent trajectory is new. It must isolate explicit controller-selected invocation, pre-action branch-and-collapse, zero/minimal payload, heterogeneity, generative reduction, assistant-message persistence, and proxy deployment.

### Recursive Language Models

- Alex L. Zhang, Tim Kraska, Omar Khattab. 2025.
- arXiv:2512.24601.
- https://arxiv.org/abs/2512.24601
- Code: https://github.com/alexzhang13/rlm
- Importance: treats long prompts as an external environment and allows the controller to programmatically inspect/decompose context and recursively invoke language models. It is the main conceptual precedent for moving cognition into a model-agnostic inference harness.

### Scaling Test-Time Compute for Agentic Coding

- Joongwon Kim, Wannan Yang, Kelvin Niu, Hongming Zhang, Yun Zhu, Eryk Helenowski, Ruan Silva, Zhengxing Chen, Srinivasan Iyer, Manzil Zaheer, Daniel Fried, Hannaneh Hajishirzi, Sanjeev Arora, Gabriel Synnaeve, Ruslan Salakhutdinov, Anirudh Goyal. 2026.
- arXiv:2604.16529.
- https://arxiv.org/abs/2604.16529
- Importance: scales long-horizon coding agents by representing, selecting, and reusing complete rollout trajectories. It is a mandatory full-rollout/trajectory-summary baseline and supports the view that representation and reuse are central to agentic test-time scaling.

### Agentic Aggregation for Parallel Scaling of Long-Horizon Agentic Tasks (AggAgent)

- Yoonsang Lee, Howard Yen, Xi Ye, Danqi Chen. 2026.
- arXiv:2604.11753.
- https://arxiv.org/abs/2604.11753
- Importance: an aggregation agent treats parallel trajectories as an environment and inspects/synthesizes them through tools. This is close in its emphasis on long trajectories and information-preserving aggregation, but operates over complete agent rollouts rather than local cognition-only branches.

### Think, But Don't Overthink: Reproducing Recursive Language Models

- Daren Wang. 2026.
- arXiv:2603.02615.
- https://arxiv.org/abs/2603.02615
- Code: https://github.com/drbillwang/rlm-reproduction
- Importance: reports that depth-1 recursion can help while deeper recursion can increase cost/time and hurt performance. Supports the conservative V1 choice of one-level, bounded external deliberation.

### Sleep-time Compute: Beyond Inference Scaling at Test-time

- Kevin Lin, Charlie Snell, Yu Wang, Charles Packer, Sarah Wooders, Ion Stoica, Joseph E. Gonzalez. 2025.
- arXiv:2504.13171.
- https://arxiv.org/abs/2504.13171
- Importance: studies precomputing useful state before queries and amortizing cognition across related questions. Relevant to later speculative/prefetch and persistent-context directions, not the canonical V1.

## 2. Model collaboration, ensembling, and reasoning search

### Learning to Decode Collaboratively with Multiple Language Models (Co-LLM)

- Zejiang Shen, Hunter Lang, Bailin Wang, Yoon Kim, David Sontag. ACL 2024.
- https://aclanthology.org/2024.acl-long.701/
- DOI: 10.18653/v1/2024.acl-long.701
- Importance: learns token-level decisions about whether a base model or an assistant model should generate. Closely related to the thesis that the currently acting model need not contain all generation/reasoning capability, but requires learned collaboration and operates at token granularity.

### Can Small Language Models Help Large Language Models Reason Better?: LM-Guided Chain-of-Thought

- Jooyoung Lee, Fan Yang, Thanh Tran, Qian Hu, Emre Barut, Kai-Wei Chang. LREC-COLING 2024.
- https://aclanthology.org/2024.lrec-main.252/
- Importance: a lightweight LM produces a rationale consumed by a frozen larger model. Supports empirical plausibility that a weaker model's generated cognition can improve another model.

### LLM-Blender: Ensembling Large Language Models with Pairwise Ranking and Generative Fusion

- Dongfu Jiang, Xiang Ren, Bill Yuchen Lin. ACL 2023.
- https://aclanthology.org/2023.acl-long.792/
- DOI: 10.18653/v1/2023.acl-long.792
- Importance: PairRanker plus GenFuser; strong precedent for candidate ranking and generative fusion. The reducer in ReasonProxy belongs to this lineage.

### Mixture-of-Agents Enhances Large Language Model Capabilities

- Junlin Wang, Jue Wang, Ben Athiwaratkun, Ce Zhang, James Zou. 2024.
- arXiv:2406.04692.
- https://arxiv.org/abs/2406.04692
- OpenReview: https://openreview.net/forum?id=h0ZfDIrj7T
- Importance: layered parallel LLM outputs condition downstream aggregators. It establishes off-the-shelf model-level composition and the importance of proposer/aggregator quality and diversity.

### Self-Consistency Improves Chain of Thought Reasoning in Language Models

- Xuezhi Wang, Jason Wei, Dale Schuurmans, Quoc V. Le, Ed H. Chi, Sharan Narang, Aakanksha Chowdhery, Denny Zhou. ICLR 2023.
- arXiv:2203.11171.
- https://arxiv.org/abs/2203.11171
- OpenReview: https://openreview.net/forum?id=1PL1NIMMrw
- Importance: samples multiple reasoning paths and marginalizes/selects by answer consistency. Canonical evidence that inference breadth can outperform greedy decoding.

### Tree of Thoughts: Deliberate Problem Solving with Large Language Models

- Shunyu Yao, Dian Yu, Jeffrey Zhao, Izhak Shafran, Thomas L. Griffiths, Yuan Cao, Karthik Narasimhan. NeurIPS 2023.
- arXiv:2305.10601.
- https://arxiv.org/abs/2305.10601
- Importance: explicit search over intermediate thoughts. ReasonProxy differs by making trajectory branching a selectively invoked tool inside an existing agent, but ToT removes any broad claim that branching inference is new.

### Graph of Thoughts: Solving Elaborate Problems with Large Language Models

- Maciej Besta et al. AAAI 2024.
- arXiv:2308.09687.
- https://arxiv.org/abs/2308.09687
- Importance: generalizes reasoning structures from chains/trees to graphs. Relevant to the branch-and-collapse/DAG interpretation.

### Improving Factuality and Reasoning in Language Models through Multiagent Debate

- Yilun Du, Shuang Li, Antonio Torralba, Joshua B. Tenenbaum, Igor Mordatch. ICML 2024.
- https://proceedings.mlr.press/v235/du24e.html
- arXiv:2305.14325.
- Importance: multiple model instances exchange arguments to improve reasoning/factuality. Establishes multi-agent deliberation; ReasonProxy does not require inter-branch communication.

### ReAct: Synergizing Reasoning and Acting in Language Models

- Shunyu Yao, Jeffrey Zhao, Dian Yu, Nan Du, Izhak Shafran, Karthik Narasimhan, Yuan Cao. ICLR 2023.
- arXiv:2210.03629.
- https://arxiv.org/abs/2210.03629
- OpenReview: https://openreview.net/forum?id=WE_vluYUL-X
- Importance: the standard reasoning/action/observation trajectory structure underlying many agent harnesses and Second Thought's timing analysis.

### SpecCoT: Accelerating Chain-of-Thought Reasoning through Speculative Exploration

- Junhan Shi, Yijia Zhu, Zhenning Shi, Dan Zhao, Qing Li, Yong Jiang. Findings of EMNLP 2025.
- https://aclanthology.org/2025.findings-emnlp.1326/
- Importance: a small model proposes multiple reasoning-step drafts in parallel and a large model verifies/selects them. The authors report 1.7–4.1× lower latency with comparable accuracy. It is an important matched-latency baseline for the claim that cheap parallel cognition is useful.

### Council Mode: Mitigating Hallucination and Bias in LLMs via Multi-Agent Consensus

- Shuai Wu, Xue Li, Yanna Feng, Yufang Li, Zhijun Wang. 2026.
- arXiv:2604.02923.
- https://arxiv.org/abs/2604.02923
- Importance: complexity triage, heterogeneous parallel model calls, and structured consensus synthesis. It is close to the inner ensemble/reducer but operates at request/final-answer level rather than as a live trajectory tool.

### iMAD: Intelligent Multi-Agent Debate for Efficient and Accurate LLM Inference

- Wei Fan, JinYi Yoon, Bo Ji. AAAI 2026, 40(35):29403–29411.
- DOI: 10.1609/aaai.v40i35.40181.
- https://ojs.aaai.org/index.php/AAAI/article/view/40181
- Importance: selectively triggers multi-agent debate with a learned gate, reporting up to 92% token reduction and up to 13.5% accuracy improvement in its evaluated QA settings. It is central prior art for adaptive external-compute allocation.

### Efficient Agentic Reasoning Through Self-Regulated Simulative Planning (SR²AM)

- Mingkai Deng, Jinyu Hou, Lara Sá Neves, Varad Pimpalkhute, Taylor W. Killian, Zhengzhong Liu, Eric P. Xing. 2026.
- arXiv:2605.22138.
- https://arxiv.org/abs/2605.22138
- Importance: decomposes agentic behavior into reactive execution, simulative planning, and learned self-regulation over whether/how deeply to plan. It motivates treating the `reason()` call policy as a metareasoning problem, while differing because it trains/realizes the systems inside an LLM rather than through a black-box proxy.

## 3. Think tools and practical council systems

### Anthropic's “The think tool: Enabling Claude to stop and think in complex tool use situations”

- Anthropic Engineering, 2025.
- https://www.anthropic.com/engineering/claude-think-tool
- Importance: interface precedent for a callable cognitive step inside tool use. Its operation is not an external heterogeneous branch ensemble.

### Karpathy LLM Council

- GitHub repository: https://github.com/karpathy/llm-council
- Importance: practical multi-model candidate/judge synthesis system. Useful as systems prior art, but not a live zero-argument same-agent trajectory operator.

### Research-panel / council MCP systems

- Example issue/design: https://github.com/zoharbabin/web-researcher-mcp/issues/302
- Importance: shows that an outer agent calling a multi-model panel as a tool exists in practice. Therefore “an ensemble exposed as a tool” is not a sufficient novelty claim.

## 4. Agent/environment branching and related systems

### Parallel Environments for Agents

- Use the exact published citation from the authors/repository when adding to a paper draft; the research review identified work that forks isolated environments so agents can explore actions in parallel.
- Importance: distinguishes cognition-only branches from world-state/action branches. The proposed V1 forks no sandbox and commits no branch action.

### Fork, Explore, Commit: OS Primitives for Agentic Exploration

- Cong Wang, Yusheng Zheng. 2026.
- arXiv:2602.08199.
- https://arxiv.org/abs/2602.08199
- Importance: environment/system primitives for branching exploration. Relevant to later extensions that allow branch tools; not the V1 cognitive-only mechanism.

## 5. Benchmarks and harnesses

### DeepSWE

- Official repository: https://github.com/datacurve-ai/deep-swe
- Benchmark site: https://deepswe.datacurve.ai/
- Verified snapshot: repository describes 113 original long-horizon tasks across TypeScript, Go, Python, JavaScript, and Rust, using isolated environments and program-based verifiers. Tasks use Harbor format; official examples use Pier and mini-SWE-agent.

### Pier

- Official repository: https://github.com/datacurve-ai/pier
- Importance: Harbor-compatible runner for sandboxed coding-agent evaluations and the standard DeepSWE execution path.

### mini-SWE-agent

- Official repository: https://github.com/SWE-agent/mini-swe-agent
- Model configuration docs: https://github.com/SWE-agent/mini-swe-agent/blob/main/docs/models/quickstart.md
- Importance: model-agnostic coding harness. LiteLLM/custom `api_base` configuration enables an OpenAI-compatible proxy without replacing the agent.

### Terminal-Bench 2.1

- Official repository: https://github.com/harbor-framework/terminal-bench-2-1
- Dataset/Hub: https://hub.harborframework.com/datasets/terminal-bench/terminal-bench-2-1/latest
- Importance: complex containerized command-line tasks evaluated through Harbor. The repository's September 2026 state says community leaderboard submissions are closed and describes at least five trials per task for the submission protocol; local evaluation remains possible.

### Harbor

- Documentation: https://www.harborframework.com/docs
- Repository: https://github.com/harbor-framework/harbor
- Importance: separates dataset, agent, model, sandbox, and verifier. This separation enables a clean fixed-harness endpoint comparison.

### Terminus-2

- Official docs: https://www.harborframework.com/docs/agents/terminus-2
- Importance: Harbor's neutral reference terminal agent. It supports custom `api_base`, JSON/XML parsers, reasoning-effort settings, and conversation summarization. Parser and compaction behavior must be included in the ReasonProxy conformance gate.

### FrontierSWE v2

- Official launch/methodology: https://www.frontierswe.com/blog/v2
- Repository: https://github.com/Proximal-Labs/frontier-swe
- Verified September 2026 launch snapshot: 34 tasks, a purpose-built Proximus harness, and up to 20 hours per task. Reported launch scores include Claude Fable 5.1 at 56.29%, GPT-5.6 at 32.2%, GLM-5.3 at 30.2%, and Kimi K3 at 25.9%.
- Caveat: these scores motivate the target but are not a controlled comparison unless rerun under the same study configuration. Some tasks require or benefit from vision.

### Terminal-Bench-Science

- Site: https://terminal-bench-science.ai/
- Repository: https://github.com/laude-institute/terminal-bench-science
- Importance: hard scientific terminal tasks and a useful high-gap validation suite. Use official task revisions and trial protocol.

### ARC-AGI-3

- Developer harness: https://github.com/ARCAGI-Labs/arc-agi-3-benchmarking
- Competition/overview: https://arcprize.org/competitions/2026/arc-agi-3
- Importance: interactive reasoning games with provider/model configuration and detailed trajectories. Suitable for cross-domain transfer after multimodal/action compatibility is validated.

### ARC-AGI-2

- Guide: https://arcprize.org/guide
- Importance: static grid-transformation reasoning; useful for later reasoning-only tests, but less aligned with self-selected mid-trajectory tool use.

## 6. OpenAI-compatible API and state behavior

### Chat Completions API reference

- https://platform.openai.com/docs/api-reference/chat/create
- Importance: canonical external endpoint. Assistant messages can carry content and tool calls under the API's message schema, enabling checkpoint text alongside an external action when a client supports and replays it.

### Function/tool calling guide

- https://platform.openai.com/docs/guides/function-calling
- Importance: reference semantics for assistant tool calls, tool results, IDs, and multi-turn replay.

### Prompt caching guide

- https://platform.openai.com/docs/guides/prompt-caching
- Importance: exact/shared prefixes can reduce provider-side prefill cost and latency. Worker providers maintain independent caches.

### Conversation state / Responses API

- https://platform.openai.com/docs/guides/conversation-state
- https://platform.openai.com/docs/api-reference/responses/create
- Importance: a strictly stateless independent proxy can support Responses only when the request replays the relevant prior items. A `previous_response_id` that points to state stored only by another provider cannot be reconstructed from nothing.

## 7. Commodity model/provider sources

Prices and product behavior can change. The values below are a **2026-09-04 research snapshot** and must be refreshed before experiments.

### Z.AI / GLM

- Pricing: https://docs.z.ai/guides/overview/pricing
- API/model docs: https://docs.z.ai/guides/llm/glm-5
- Prompt caching: https://docs.z.ai/guides/capabilities/caching
- Snapshot used in planning: GLM-5.3 listed at approximately $1.40/M uncached input, $0.26/M cached input, and $4.40/M output. A temporary GLM-5.3-Flash promotion was also visible; do not use promotional pricing as a timeless paper claim.
- Importance: target commodity controller/worker with reasoning-effort controls and OpenAI-compatible access.

### Moonshot / Kimi K3

- Product/model page: https://www.kimi.com/en/blog/kimi-k3
- API docs: https://platform.moonshot.ai/docs
- Pricing: https://platform.moonshot.ai/docs/pricing
- Snapshot used in planning: Kimi K3 listed at approximately $0.30/M cache-hit input, $3/M uncached input, and $15/M output, with a large context window and reasoning-effort settings.
- Importance: second commodity family for heterogeneous branches and controller replication.

## 8. Source-quality notes

- Academic novelty claims should rely on the papers themselves, not blogs summarizing them.
- Official benchmark scores are not interchangeable across harnesses, commits, trial counts, or resource limits.
- GitHub repositories can change; record commit hashes used in experiments.
- Provider model aliases can move; record snapshot/version/date and calibration outputs.
- Prices should be read from official billing docs immediately before and after a full run.
- A paper should describe public leaderboard values as external snapshots unless the authors rerun the exact system.

## 9. Claims still requiring additional verification before submission

1. Whether an even closer unpublished or very recent work implements the exact zero-argument, self-selected, same-agent, full-state, reducer-collapsed, stateless proxy interface.
2. Exact citation and public implementation status of “Parallel Environments for Agents.”
3. Exact feature compatibility of each pinned GLM/Kimi model snapshot with assistant content plus tool calls.
4. Whether each target harness faithfully round-trips assistant content in the selected parser/configuration.
5. Official rules for publishing/submitting proxy-system results to each benchmark leaderboard at the time of submission.
6. Current model prices, context limits, and rate limits when experiments begin.

These unknowns should be resolved through implementation-time contract tests and a final related-work search immediately before paper submission.
