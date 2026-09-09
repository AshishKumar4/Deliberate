# Deliberate

Coding agents think and act in a single serial continuation: one hypothesis at
a time, reasoning and tool calls interleaved, no way to step back and weigh
alternatives before committing to an expensive or hard-to-reverse action.
Deliberate is a proxy that gives any OpenAI-compatible agent a way out of
that: a `deliberate` tool that buys parallel thinking on demand.

The agent calls `deliberate()` instead of acting. The proxy fans the agent's
full trajectory out to several independent continuations of that same agent,
reduces them to one compact checkpoint — conclusion, evidence, alternatives,
best next move, how to verify — and hands it back. The agent reads it and
then acts with its own tools. Nothing executes during deliberation and
nothing is observed; it is cognition, not action.

## Why

A lone continuation gets stuck inside its first plausible diagnosis. Long
serial reasoning drifts, and by the time the agent acts it has never
seriously considered being wrong. Independent continuations don't share that
fate: they disagree, surface alternatives, and catch each other's blind
spots — the way asking several engineers to think separately beats thinking
alone. The checkpoint distills that into something the acting agent can use
in one turn.

Because Deliberate is a proxy, not a harness patch, it works with any agent
that speaks the OpenAI chat-completions protocol. Point the agent at the
proxy, and the capability is just another tool. No harness fork, no custom
scaffold, no lock-in to one agent design.

## Use

Run the proxy (host-only):

```bash
export REASONPROXY_LOCAL_BINDING_KEY=local-binding-no-cloudflare-credential
export OPENCODE_API_KEY=...
wrangler dev -c workers/ai/wrangler.jsonc   # local model binding (OAuth, no manual tokens)
.venv/bin/python -m reasonproxy.app
```

Point your agent at the proxy URL and use the `deliberate` tool where a
decision is difficult, uncertain, or hard to reverse. A turn containing only
`deliberate` is internal: the proxy intercepts it and resumes you, so your
harness still gets its action on the outward turn. You may pass a focus
question — `deliberate(question="Which diagnosis fits the log output?")` —
or nothing at all.

## Status: WIP

The mechanism is built and tested: interception, fan-out, reduction,
checkpoint persistence, usage accounting — all covered by the test suite
(`.venv/bin/python -m pytest tests/ -q`).

Whether agents actually invoke the tool spontaneously in live harness runs,
and whether that improves task outcomes, is **unproven and under active
investigation**. Early measurements show models routing through the proxy
without calling the tool; current work is diagnosing why and testing
elicitation variants. Treat every efficacy claim about this project as WIP
until this section says otherwise.
