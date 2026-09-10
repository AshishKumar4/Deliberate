# Powered evaluation plan

Written 2026-09-10. Replaces the n=1 pilot program. Every number below is either
measured in this repo or computed from a cited source; nothing is estimated by feel.

## 1. What has actually been tested so far

| | Value |
|---|---|
| Graded trials, all time | 72 |
| Distinct benchmark tasks touched | 4 (`sqlite-db-truncate`, `query-optimize`, `log-summary-date-ranges`, `cancel-async-tasks`) |
| Task pool those came from | Terminal-Bench 2.0, 89 tasks |
| Repeats per task-condition cell | 1-3 |
| DeepSWE trials | 0 (suite wired, never run) |
| Terminal-Bench 3.0 / 4.0 trials | 0 |
| ARC-AGI-3 / HLE trials | 0 |

Minimum detectable effect at 6 paired trials, 80% power, 20% discordance: **20 pp**.
So every result in this repo to date is a mechanism pilot, not evidence about capability.
The design questions it answered (uptake, directive effect, reducer reliability, role
diversity) were the right ones to answer cheaply first, and they are answered. The
capability question has not been asked at a sample size that could answer it.

## 2. Models in the roster (all live-verified, all free on the current accounts)

| Role | Alias | Endpoint |
|---|---|---|
| Controller | `@cf/zai-org/glm-5.3` | Cloudflare Workers AI, direct REST |
| Controller | `@cf/zai-org/glm-5.3-flash` | Cloudflare Workers AI, direct REST |
| Branch | `@cf/zai-org/glm-5.3` | Cloudflare Workers AI |
| Branch | `@cf/deepseek-ai/deepseek-v4-pro-0813` | Cloudflare Workers AI |
| Branch | `nemotron-3-ultra-free` | OpenCode Zen |
| Branch | `@cf/zai-org/glm-5.3-flash` (hot) | Cloudflare Workers AI |
| Reducer | `@cf/nvidia/nemotron-3-120b-a12b` (Super) | Cloudflare Workers AI |
| Reducer | `inception/mercury-2.5` (Mercury) | OpenRouter |

Excluded, with reasons: `@cf/qwen/qwen3.8-27b` (architecturally rejects the injected
second system message), `muse-spark-1.3-contributor` (Responses-API only; paid tier
returns 401 insufficient balance), GLM-5 / GLM-5.1 / GLM-5v (never in any roster -
older, weaker models, and irrelevant to this program).

## 3. The statistical arithmetic

Paired binary outcome, two-sided alpha 0.05, 80% power, McNemar:

| Effect to detect | Discordance 10% | 20% | 30% |
|---|---|---|---|
| +2 pp | 1,960 | 3,922 | 5,884 |
| +3 pp | 870 | 1,742 | 2,614 |
| +5 pp | 312 | 626 | 940 |
| +7 pp | 158 | 318 | 478 |
| +10 pp | - | 155 | 233 |

Minimum detectable effect at the sample sizes each benchmark can supply (20% discordance):

| Paired trials | MDE |
|---|---|
| 66 (TB4, 1 repeat) | 15.2 pp |
| 89 (TB2, 1 repeat) | 13.1 pp |
| 330 (TB4, 5 repeats) | 6.9 pp |
| 2,500 (HLE, 1 pass) | **2.5 pp** |
| 5,000 (HLE, 2 passes) | 1.8 pp |

**Only HLE can currently resolve a one-to-two-point effect.** That is not a preference,
it is the sample size available divided by the cost per observation.

## 4. Why Terminal-Bench 4.0 cannot carry the powered claim locally

Downloaded and profiled: `terminal-bench/terminal-bench@4.0.0`, 66 tasks,
`~/.cache/reasonproxy/terminal-bench`.

| Property | Value |
|---|---|
| Declared agent timeout | 28,800 s (8 h) on **all 66** tasks |
| Declared memory | 4 GiB x35, 8 GiB x22, 16 GiB x8, 32 GiB x1 |
| Declared CPUs | 2 x89, 4 x26, 8 x11, 16 x4 (per container; tasks are multi-container) |
| Expert time estimate | median 4 h, max 60 h, **431.8 h summed** |
| Host RAM | 16 GiB total |

9 of 66 tasks declare more memory or CPU than this host has, so they cannot run here at
any speed. The remaining 57 run one at a time. At an optimistic 1 h/trial: one arm at
n=1 is ~57 h; a two-arm paired run is ~5 days; the 5 repeats needed for a 7 pp MDE is
~24 days of uninterrupted execution. TB4 statistical power requires cloud sandboxes
(`harbor run --env modal|daytona`, which the CLI already supports) and therefore a
compute budget decision. Model inference stays free; sandbox compute does not.

## 5. The official-baseline control, and its confound

Published: **GLM-5.3 at max reasoning effort scores 41.8% +/- 3.2% on Terminal-Bench 4.0**
(rank 5; 8.7 B tokens; ~$2,700), inside a **Claude Code** harness.
Same model on TB 3.0: 28.3%. On TB 2.1: 88.2%.

Two different controls are needed and they answer different questions:

1. **External calibration** - raw GLM-5.3 in the *official* agent (`harbor -a claude-code`)
   on the full 66-task pool. Tells us whether our plumbing reproduces 41.8% +/- 3.2%.
   Any gap is our infrastructure's error bar and must be published with the result.
2. **Causal control** - raw GLM-5.3 in *our* agent (`mini-swe-agent`), same tasks, same
   caps, paired against the deliberation arm. This is the only control that licenses a
   causal claim about the proxy.

The harness is a first-order confound, not a detail: on ARC-AGI-3 the same model scores
62.7% under the standard harness and 98.6-99.9% under the provider-adapter harness. A
deliberation gain measured in one harness does not transfer to a leaderboard number
produced in another, and will not be reported as if it did.

## 6. Reasoning effort

Requested: every ensemble member at maximum reasoning, with only final outputs captured.

* **Only final outputs are captured - already true and enforced in code.** Branch text
  comes from `_branch_text` (`src/reasonproxy/engine.py:1160-1162`), which reads
  `.content` only. Raw scratchpad is never promoted, persisted, reduced or shown
  (`engine.py:16-17`); traces record `reasoning_chars` as a length only
  (`engine.py:764`). A branch that returns reasoning and no content is rejected, not
  salvaged (`engine.py:1150-1153`).
* **Max effort is expressible without a code change.** A backend's `params:` block is
  forwarded verbatim; `TRANSPORT_OWNED` (`src/reasonproxy/config.py:23-29`) claims only
  `model`, `messages`, `input`, `tools`, `tool_choice`, `text`, `stream`,
  `stream_options`, `n`, so `reasoning_effort` / `reasoning` reach the provider body
  untouched. Two tests in `tests/test_upstream.py` assert exactly that, including that a
  null override drops the key instead of sending a JSON null. This reverses the
  2026-09-10 zero-parameter policy *deliberately and only for reasoning budget*, which
  is the variable under test; sampling parameters stay unset.
* **There is no single "max" token - the ladder is per model.** Read 2026-09-10 from the
  public OpenRouter catalogue (`GET /api/v1/models`, `reasoning` field):

| Model | Supported efforts | Provider default | Declared in our rosters |
|---|---|---|---|
| `z-ai/glm-5.3` | max, high, low | **max** | `max` |
| `z-ai/glm-5.3-flash` | max, high, low | **max** | `max` |
| `deepseek/deepseek-v4-pro-0813` | max, high, low | high | `max` |
| `nvidia/nemotron-3-ultra-550b-a55b` | high, medium | high | `high` (no `max` exists) |
| `inception/mercury-2.5` | high, medium, low, none | medium | `high` reducer, `none` judge |
| `qwen/qwen3.8-27b` (excluded) | xhigh, medium, low | xhigh | - |

  Two consequences. First, `high` is *below default* for both GLM models, so the initial
  draft of these rosters would have quietly **reduced** effort under what every earlier
  trial in this repo already received from provider defaults; that is corrected to `max`.
  Second, because `max` is GLM 5.3's default, the published 41.8% run and our own prior
  zero-parameter trials were already at maximum effort - raising effort is therefore not
  an available source of gain for the controller, only an equalizer for the branches.
* **Route authority differs.** Mercury is reached *through* OpenRouter, so its ladder is
  authoritative. GLM, DeepSeek and Nemotron are served by Cloudflare Workers AI and
  OpenCode Zen, whose OpenAI-compatible surfaces do not document this key; for those the
  catalogue is model-family evidence and the accepted key/value MUST still be probed
  live before a scored run, with the result recorded in the manifest.

## 7. Execution order

1. **HLE, full 2,500-question suite** (`scripts/hle.py`). Text-only population declared
   up front because the Workers AI controllers are text-to-text; image questions are out
   of population, not scored wrong. Official prompts verbatim, official denominator,
   8,192-token floor, arm-blind judge, resumable JSONL. Paired McNemar + Wilson +
   paired bootstrap. Arms: raw controller vs directive-only vs full ensemble, all at max
   reasoning effort. **This is the run that can detect 2.5 pp.**
2. **TB4 causal arms, host-feasible pool** - frozen and ready:
   `runs/onboarding/terminal-bench-4-selection.json` pins the **57** tasks whose declared
   containers fit this host (<= 8192 MB, <= 8 CPUs), chosen by resources alone before any
   model touched a TB4 task; the 9 excluded are named in that file. `freeze --suite
   terminal-bench-4 --roster configs/tb4.yaml` plans **171 trials** (57 x 3 arms x 1) at
   the official 8-hour timeout, with the registry dataset pinned by the content digest of
   all 66 `task.toml` files. Detects roughly 15 pp and no smaller: descriptive, and the
   only agentic evidence this host can produce unaided.
3. **TB4 external calibration** - raw GLM-5.3 in the official Claude Code agent on the
   full 66, to measure our infrastructure's error bar against 41.8% +/- 3.2%.
4. **TB4 at power** - full 66 x 3 arms x 5 repeats needs cloud sandboxes; locally that is
   a ~24-day serial run for a 7 pp MDE.
5. **ARC-AGI-3** - `arcprize/arc-agi-3-benchmarking`, 25 games, needs a free
   `ARC_API_KEY`; our proxy registers as an OpenAI-compatible model config. Report
   standard-harness and provider-adapter numbers separately, as the foundation requires.

## 8. Blockers that need a human

| Blocker | Why | Fix |
|---|---|---|
| All provider credentials lost | The eval kernel was reset; keys were held in process memory only and were never written to disk (verified by the pre-commit secret scans) | Re-supply `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `OPENCODE_API_KEY`, `OPENROUTER_API_KEY`, `REASONPROXY_API_KEY` |
| HLE dataset gated | `cais/hle` is `gated: auto`; anonymous parquet fetch returns 401 | Accept the terms with any free HF account, export `HF_TOKEN` |
| ARC-AGI-3 needs a key | Games are served by the ARC API | Free key from arcprize.org, export `ARC_API_KEY` |
| TB4 at power needs cloud sandboxes | 9/66 tasks exceed this host; the rest are serial | Decide on a Modal or Daytona budget, or accept TB4 at n=1 for description only |
