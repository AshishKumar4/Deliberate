# Deliberate (ReasonProxy)

A stateless OpenAI-compatible proxy that gives any coding agent a private
deliberation tool. The agent calls `deliberate()` (v3/v4) instead of acting;
the proxy fans the trajectory out to several independent continuations,
reduces them to one compact checkpoint, and resumes the agent — which then
acts with its own tools. Routing through the proxy is not the effect; only
measured invocations of the private tool count.

Status: the mechanism is fully plumbed and unit-tested, but live harness
replays currently show **zero spontaneous uptake** (0/60 matched requests
across 5 controllers on v2). v3 (intuitive name + value proposition) and v4
(optional focus question) are versioned experimental conditions built to test
exactly why. See [findings](#findings) and
`runs/onboarding/zero-uptake-root-cause.json`.

## How it works

```
agent ──► /v1/chat/completions ──► ReasonProxy ──► providers (Cloudflare Workers AI, OpenCode Zen)
                                          │
                     controller calls deliberate()
                                          │ intercept (never reaches the agent's harness)
                     ┌────────────────────┼────────────────────┐
                     ▼                    ▼                    ▼
                  branch 1             branch 2  ...        branch N
                  (same agent, tool_choice=none, nothing executes)
                     └────────────────────┼────────────────────┘
                                          ▼
                                   reducer → <deliberation> checkpoint
                                          │ (conclusion, evidence, alternatives,
                                          │  next_action, verification)
                     controller resumes with checkpoint, then acts
```

- **Stateless per request.** No server-side sessions. Each request carries the
  full trajectory; `max_reason_calls` resets every turn by construction.
- **Invisible interception.** A turn containing only the private call is
  answered inside the proxy and never reaches the caller's harness, so a
  harness that mandates an action every turn still gets one on the outward
  response.
- **Fixed ensembles, versioned prompts.** Conditions differ only in
  `reason_mode` (`off` / `noop` / `live`) and prompt version. Every trace row
  records content hashes of the exact prompt bytes used, so a result can never
  claim a revision it did not run.
- **Caller wins.** Caller sampling parameters, `tool_choice`, response format,
  and tool lists are forwarded verbatim; a caller-forced turn suppresses the
  private tool rather than fighting the caller.

## Use

**Run the proxy** (host-only; never inside a task container):

```bash
export REASONPROXY_LOCAL_BINDING_KEY=local-binding-no-cloudflare-credential
export OPENCODE_API_KEY=...            # -> provider zen
# Start the local AI binding first (uses Wrangler OAuth, no manual tokens):
wrangler dev -c workers/ai/wrangler.jsonc
.venv/bin/python -m reasonproxy.app     # serves /v1/* per configs/research.yaml
```

Point any OpenAI-compatible agent at the proxy URL with the virtual model id
as the model name (`rp/cf-super-available`, `rp/zen-lightning-available`,
`ctl/...`, `noop/...`). The `ctl` arm is the raw model over the same path
(no injection, no interception); `noop` advertises the identical tool but
returns a fixed neutral result, isolating prompt+tool presence from real
deliberation.

**Run the benchmark** (Terminal-Bench 2.0, Harbor + mini-swe-agent, guarded):

```bash
.venv/bin/python scripts/benchmark.py run \
  --frozen runs/frozen/terminal-bench-2-20260907T170001Z.json \
  --job-root runs/tb2-guarded --conditions rp/zen-lightning-available --tasks sqlite-db-truncate
.venv/bin/python scripts/benchmark.py analyze --frozen <manifest> --execution <record> --out <analysis>
```

**Probe a backend** (reachability, wire conformance, reducer contract — never quality):

```bash
export CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=... OPENCODE_API_KEY=...
.venv/bin/python scripts/probe.py configs/research.yaml
```

**Tests:** `.venv/bin/python -m pytest tests/ -q`

## Conditions and versions

| Version | Tool | Args | Prompt | Status |
|---|---|---|---|---|
| v2 | `__reasonproxy_reason` | none (violations rejected) | `concise` | frozen benchmark condition |
| v3 | `deliberate` | none | `deliberate` (value prop + worked example) | experimental |
| v4 | `deliberate` | optional `question` (≤2000 chars, threaded into branches) | `deliberate-focus` | experimental |

`reason` was rejected as a name: a bare generic noun is the likeliest
caller-tool collision and reads as a label, not an action. v2 conditions are
frozen — v3/v4 run as newly named conditions and are never pooled with v2.

## Findings

- **Uptake, not routing.** 6 treatment trials + 60 matched replay requests:
  0 private invocations, 0 branches. Requests traverse the proxy; the
  deliberation mechanism is never used. Native model reasoning is a separate,
  unmeasured quantity.
- **Recognition without selection.** 4/5 controllers name and describe the
  tool on direct query but never invoke it during task work; the 5th (Ling)
  fails even recognition. The failure is at the decision step, not transport.
- **External reference.** NVIDIA publishes Nemotron 3.5 Lightning +
  mini-SWE-agent v2.4.5 at **29.7% on Terminal-Bench 2.1 (89 tasks)** (model-card
  chart). No local baseline rerun is needed; the treatment must be aligned to
  TB 2.1 / mini-SWE 2.4.5 before any benchmark-level comparison.
- Raw records, sleep-overlap audits, and per-arm analyses live under
  `runs/onboarding/`; frozen inputs under `runs/frozen/`.

## Layout

- `src/reasonproxy/` — proxy: `engine.py` (loop/interception/fan-out/reduce),
  `prompts.py` (versioned model-visible strings), `config.py`, `upstream.py`
  (retries, rate gates, usage accounting), `app.py`
- `configs/research.yaml` — active roster (frozen benchmark aliases + docs)
- `scripts/` — `benchmark.py`, `probe.py`, `native_harness.py`
- `tests/` — behavioral tests over scripted upstream stubs (no live calls)
- `workers/ai/` — local Cloudflare binding (`wrangler.jsonc`)
- `runs/` — frozen manifests, execution records, trace audits, experiment plans
- `reasonproxy-research-bundle/` — original research spec and protocol docs
