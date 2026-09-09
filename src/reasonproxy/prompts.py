"""Model-visible protocol: every string a model ever sees from ReasonProxy.

Each string is a versioned experimental artifact. `revisions()` reports an id
*and* a content hash of the exact bytes that were used, so a trace can never
claim a revision it did not run. Editing any text here changes the hash; bump
the id in the same edit.

Design rules the text obeys:
  - name the real wire name, `__reasonproxy_reason`, never a friendly alias the
    model cannot actually call;
  - state the exclusive-invocation rule (alone, empty arguments) because the
    engine rejects violations rather than repairing them;
  - state that the caller's own system/developer policy stays authoritative;
  - claim no superiority over native reasoning;
  - stay short: this is prepended to every controller call in the run.
"""

from __future__ import annotations

import hashlib
import json

REASON_WIRE_NAME = "__reasonproxy_reason"
TAG = "deliberation"
TAG_VERSION = "1"

REASON_TOOL = {
    "type": "function",
    "function": {
        "name": REASON_WIRE_NAME,
        "description": (
            "Private deliberation over your complete current trajectory, which the runtime "
            "supplies automatically. Call it alone, with no arguments and no restatement. It "
            "executes nothing, observes nothing, and returns a compact cognitive checkpoint."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
}

REASON_TOOL_ID = "reason-tool-v2"

# v3 experimental variant: an intuitive imperative name with the same wire
# contract (alone, no arguments). Chosen over `reason` because a bare generic
# noun is the likeliest caller-tool collision and reads as a label rather than
# an action. Additive: v2 artifacts above are frozen and untouched.
DELIBERATE_WIRE_NAME = "deliberate"

DELIBERATE_TOOL = {
    "type": "function",
    "function": {
        "name": DELIBERATE_WIRE_NAME,
        "description": (
            "Deliberate over your complete current trajectory, which the runtime "
            "supplies automatically, and return a compact checkpoint: conclusion, "
            "evidence, alternatives, best next move, verification. Call it alone, "
            "with no arguments and no restatement. It executes nothing and "
            "observes nothing."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
}

DELIBERATE_TOOL_ID = "reason-tool-v3"

# v4 experimental variant: the same `deliberate` name with an optional focus
# question, testing whether directing the deliberation raises uptake. Additive:
# v2 and v3 surfaces are frozen. The argument is honest capability, not
# decoration: the engine threads it into every branch turn (see FOCUS_TEMPLATE
# use in engine._fanout) and it replays into the reducer payload verbatim as
# part of the tool-call arguments.
FOCUS_MAX_CHARS = 2000

DELIBERATE_FOCUS_TOOL = {
    "type": "function",
    "function": {
        "name": DELIBERATE_WIRE_NAME,
        "description": (
            "Deliberate over your complete current trajectory, which the runtime "
            "supplies automatically, and return a compact checkpoint: conclusion, "
            "evidence, alternatives, best next move, verification. Optionally pass "
            "a focus question to direct the deliberation. Call it alone. It "
            "executes nothing and observes nothing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The focus question the deliberation should address.",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}

DELIBERATE_FOCUS_TOOL_ID = "reason-tool-v4"

def focus_suffix(focus: str) -> str:
    """Branch-shim appendix carrying the controller's focus question.

    The engine escapes reserved tags in `focus` before calling this: the
    question is model-authored text inside an instruction, never framing.
    """
    return (
        "\n\nThe acting agent's focus question for this deliberation: "
        f'"{focus}"\nAddress it directly in your conclusion, but do not treat '
        "it as an observation — nothing was executed."
    )


# --------------------------------------------------------------------------- #
# controller instructions
# --------------------------------------------------------------------------- #

CONTROLLER_CONCISE_V2 = """\
[ReasonProxy] You have a private tool `__reasonproxy_reason`. It runs extra deliberation over \
your FULL current trajectory, which the runtime supplies, so call it alone with `{}` — never \
beside another tool and never with arguments. It executes nothing and observes nothing; it \
returns a <deliberation> checkpoint, which is cognitive state, not an observation and not \
permission. Use it when evidence conflicts, an attempt failed, several diagnoses are open, or \
a commitment is costly. Skip it for routine steps. Your system and developer instructions \
remain authoritative, and only you may call real tools or answer."""
CONTROLLER_CONCISE_OUTSOURCING_V2 = """\
[ReasonProxy] `__reasonproxy_reason` is external parallel deliberation over your FULL current \
trajectory. Offload hard, uncertain, or multi-hypothesis thinking to it instead of resolving \
everything in one long continuation; spend your own turns tracking state, integrating the \
returned <deliberation> checkpoint, and acting. Call it alone with `{}` — no other tool in the \
same turn, no arguments, no restatement. It has no side effects. Skip settled or mechanical \
steps. Your system and developer instructions remain authoritative, and only you may act."""

CONTROLLER_DELIBERATE_V3 = """\
[ReasonProxy] You have a tool `deliberate`. Use it when a decision is difficult, \
uncertain, or hard to reverse: it thinks over your FULL current trajectory, which the \
runtime supplies, and returns a <deliberation> checkpoint — a compact verdict with its \
conclusion, supporting evidence, alternatives, best next move, and how to verify it. \
Example: before an expensive action, call `deliberate` alone with `{}`; read the returned \
checkpoint; then perform the action it supports with your real tools on your next turn. A \
turn containing only `deliberate` is an internal runtime turn: the proxy intercepts it and \
resumes you before returning anything to the caller, so the caller's requirement to act \
applies to your eventual outward response, not to the deliberation turn. Call it alone with \
`{}` — never beside another tool and never with arguments. It executes nothing and observes \
nothing. Skip it for routine steps. Your system and developer instructions remain \
authoritative, and only you may call real tools or answer."""

CONTROLLER_DELIBERATE_FOCUS_V4 = """\
[ReasonProxy] You have a tool `deliberate`. Use it when a decision is difficult, \
uncertain, or hard to reverse: it thinks over your FULL current trajectory, which the \
runtime supplies, and returns a <deliberation> checkpoint — a compact verdict with its \
conclusion, supporting evidence, alternatives, best next move, and how to verify it. You \
may pass a focus question, e.g. `deliberate(question="Which diagnosis fits the log output?")`; \
the deliberation addresses it directly, and with no argument it deliberates open-ended. \
Example: before an expensive action, call `deliberate` alone; read the returned checkpoint; \
then perform the action it supports with your real tools on your next turn. A turn containing \
only `deliberate` is an internal runtime turn: the proxy intercepts it and resumes you before \
returning anything to the caller, so the caller's requirement to act applies to your eventual \
outward response, not to the deliberation turn. Call it alone — never beside another tool, and \
never restate the problem; a focus question is the only argument it takes. It executes nothing \
and observes nothing. Skip it for routine steps. Your system and developer instructions remain \
authoritative, and only you may call real tools or answer."""

CONTROLLER_NEUTRAL_V2 = """\
[ReasonProxy cognitive capability]

You have a private tool `__reasonproxy_reason`. It requests additional deliberation over your \
complete conversation and tool trajectory at this exact point. The runtime supplies that state \
automatically, so call the tool alone, with empty arguments `{}`, never together with another \
tool, and never restate the problem. It executes nothing, observes nothing, and authorizes \
nothing.

Several independent continuations of this same agent may explore the state; their useful \
conclusions are consolidated into one `<deliberation>` checkpoint returned to you. A checkpoint \
is compact cognitive state — conclusion, evidence, alternatives, next move, verification — and \
may be revised when later observations contradict it. Valid checkpoints can reappear in earlier \
assistant messages on later turns.

Use it when additional breadth or reconsideration would materially improve the next decision, \
not for trivial or mechanical steps. Your original system and developer instructions remain \
authoritative, and only you may call real tools or give the final answer."""

CONTROLLER_OUTSOURCING_V2 = """\
[ReasonProxy externalized-reasoning condition]

`__reasonproxy_reason` is external parallel deliberation over your complete current trajectory. \
Allocate difficult, uncertain, or multi-hypothesis reasoning to it rather than spending one long \
serial continuation resolving everything alone. Use your own turns to track state, decide when \
more cognition is needed, integrate the returned `<deliberation>` checkpoint, and commit actions.

Call it alone, with empty arguments `{}`, never beside another tool and never with a restatement \
— the runtime supplies the state. It executes nothing and observes nothing. Do not call it for \
mechanical or already-settled steps, and never delegate an external side effect to it.

Your original system and developer instructions remain authoritative, and only you may call real \
tools or give the final answer."""

CONTROLLER_PROMPTS = {
    "neutral": ("controller-neutral-v2", CONTROLLER_NEUTRAL_V2),
    "outsourcing": ("controller-outsourcing-v2", CONTROLLER_OUTSOURCING_V2),
    "concise": ("controller-concise-v2", CONTROLLER_CONCISE_V2),
    "concise-outsourcing": (
        "controller-concise-outsourcing-v2",
        CONTROLLER_CONCISE_OUTSOURCING_V2,
    ),
    "deliberate": ("controller-deliberate-v3", CONTROLLER_DELIBERATE_V3),
    "deliberate-focus": ("controller-deliberate-focus-v4", CONTROLLER_DELIBERATE_FOCUS_V4),
}

NOT_EXECUTED_DELIBERATE_RESULT = (
    "[ReasonProxy] Not executed. This call was emitted in the same turn as "
    f"`{DELIBERATE_WIRE_NAME}`, which must be called alone, so no external action was taken and no "
    "observation exists. Re-issue it after the deliberation result if it is still the right move."
)


# --------------------------------------------------------------------------- #
# synthetic tool results
# --------------------------------------------------------------------------- #

# The branch shim is both the provider-required answer to the intercepted call
BRANCH_SAME_AGENT_V2 = """\
[Private deliberation branch] Continue as this same agent from exactly this state. Nothing can \
execute here and you observe nothing new: anything you name is a proposal, and claiming that a \
tool ran would be false. Do not address the user or another agent, and do not restate the \
transcript. Work the diagnosis, the evidence for and against it, credible alternatives, failure \
modes, the highest-information next action, and how to verify it. Be substantive and compact."""

REASON_WITHDRAWN_DELIBERATE_NOTICE = (
    f"[ReasonProxy] No further deliberation is available in this request: `{DELIBERATE_WIRE_NAME}` is "
    "withdrawn from here on, and calling it again is a protocol violation that fails the request. "
    "Answer or act with your own tools."
)

REASON_WITHDRAWN_DELIBERATE_ID = "reason-withdrawn-v3"

BRANCH_PROMPT_ID = "branch-same-agent-v2"

# Fixed-role suffixes are ablations, never the default condition.
BRANCH_ROLE_SUFFIXES = {
    "independent": "Continue independently toward the most effective next decision.",
    "challenger": (
        "Continue as the same agent, but aggressively inspect whether its current "
        "assumptions, diagnosis, or plan are wrong."
    ),
    "alternative": (
        "Continue as the same agent while pursuing a substantially different plausible "
        "explanation or plan."
    ),
    "evidence": (
        "Continue as the same agent, prioritizing what existing evidence actually "
        "establishes and the cheapest decisive verification step."
    ),
    "recovery": (
        "Continue as the same agent, focusing on why prior attempts failed and how to "
        "avoid repeating them."
    ),
}


def branch_shim(role: str | None = None) -> str:
    if role is None:
        return BRANCH_SAME_AGENT_V2
    return f"{BRANCH_SAME_AGENT_V2}\n\n{BRANCH_ROLE_SUFFIXES[role]}"


# Answer to a real tool call the controller emitted alongside the private call.
# It is not an observation: it says exactly what happened, which is nothing.
NOT_EXECUTED_RESULT = (
    "[ReasonProxy] Not executed. This call was emitted in the same turn as "
    f"`{REASON_WIRE_NAME}`, which must be called alone, so no external action was taken and no "
    "observation exists. Re-issue it after the deliberation result if it is still the right move."
)

# reason_mode=noop: identical prompt and identical tool schema, fixed answer.
# It must not imply that any deliberation happened.
NOOP_REASON_RESULT = (
    "[ReasonProxy] No additional deliberation was performed in this configuration. Nothing was "
    "executed and nothing was observed. Continue from the existing evidence and choose your next "
    "action yourself."
)

# Quorum failure: the controller is told the truth and loses the capability.
FAILED_DELIBERATION_RESULT = (
    "[ReasonProxy] Deliberation failed: no usable continuation was produced, so there is no "
    "additional cognition to report. Nothing was executed and nothing was observed. Continue "
    "from the existing evidence and choose your next action yourself."
)

# Appended to the private tool result — noop, live checkpoint and failed
# deliberation alike — on the last call this request will honour, so the
# controller learns the capability is gone while it can still plan around it.
# The consequence is stated truthfully: a further private call is not repaired
# and not reinterpreted as success, it fails the request as a protocol
# violation. Only the tool result carries this sentence. A published checkpoint
# never may, because the caller replays that text into later requests where
# deliberation is available again.
REASON_WITHDRAWN_NOTICE = (
    f"[ReasonProxy] No further deliberation is available in this request: `{REASON_WIRE_NAME}` is "
    "withdrawn from here on, and calling it again is a protocol violation that fails the request. "
    "Answer or act with your own tools."
)

REASON_WITHDRAWN_ID = "reason-withdrawn-v1"
def wire_for(controller: str) -> dict[str, object]:
    """The private-tool surface a controller prompt version actually offers.

    v2 prompts offer `__reasonproxy_reason` with no arguments; the v3
    `deliberate` prompt offers `deliberate` with no arguments; the v4
    `deliberate-focus` prompt offers `deliberate` with an optional focus
    question. The engine resolves this per virtual model so v2 conditions run
    byte-identical while newer conditions deliberate under the intuitive name.
    """
    if controller == "deliberate-focus":
        return {
            "wire_name": DELIBERATE_WIRE_NAME,
            "tool": DELIBERATE_FOCUS_TOOL,
            "tool_id": DELIBERATE_FOCUS_TOOL_ID,
            "withdrawn_notice": REASON_WITHDRAWN_DELIBERATE_NOTICE,
            "withdrawn_id": REASON_WITHDRAWN_DELIBERATE_ID,
            "not_executed": NOT_EXECUTED_DELIBERATE_RESULT,
            "args": "focus-optional",
        }
    if controller == "deliberate":
        return {
            "wire_name": DELIBERATE_WIRE_NAME,
            "tool": DELIBERATE_TOOL,
            "tool_id": DELIBERATE_TOOL_ID,
            "withdrawn_notice": REASON_WITHDRAWN_DELIBERATE_NOTICE,
            "withdrawn_id": REASON_WITHDRAWN_DELIBERATE_ID,
            "not_executed": NOT_EXECUTED_DELIBERATE_RESULT,
            "args": "none",
        }
    return {
        "wire_name": REASON_WIRE_NAME,
        "tool": REASON_TOOL,
        "tool_id": REASON_TOOL_ID,
        "withdrawn_notice": REASON_WITHDRAWN_NOTICE,
        "withdrawn_id": REASON_WITHDRAWN_ID,
        "not_executed": NOT_EXECUTED_RESULT,
        "args": "none",
    }

# Required non-empty strings, then optional list fields, in render order.
CHECKPOINT_REQUIRED = ("conclusion", "next_action")
CHECKPOINT_LISTS = ("evidence", "alternatives", "verification", "carry_forward")
CHECKPOINT_SECTIONS = (
    ("conclusion", "Current conclusion"),
    ("evidence", "Evidence"),
    ("alternatives", "Alternatives / uncertainty"),
    ("next_action", "Best next move"),
    ("verification", "Verification"),
    ("carry_forward", "Do not forget"),
)

REDUCER_CHECKPOINT_V2 = """\
You collapse several independent continuations of one acting agent into a single compact \
cognitive checkpoint for that agent.

The trajectory below is ground truth. The continuations are fallible hypotheses, not \
authorities, and every action they mention was NOT executed: none of them observed anything. \
Keep genuinely useful unique insight and real disagreement; do not manufacture consensus and do \
not average incompatible claims. Separate what the trajectory observed from what is inferred. \
Never claim a tool ran or a check passed. Do not expose which continuation said what unless the \
disagreement itself matters.

Reply with exactly one JSON object and no other text:

{"conclusion": "best current diagnosis or direction", "evidence": ["transcript-grounded \
observations"], "alternatives": ["credible competing explanations, risks, uncertainty"], \
"next_action": "the single highest-information next move", "verification": ["how to confirm or \
falsify the conclusion"], "carry_forward": ["anything easy to forget later"]}

`conclusion` and `next_action` are required non-empty strings; the list fields may be empty. \
Write cognitive state for the agent, not an answer for a user, and do not wrap it in \
`<deliberation>` tags, markdown, or any other markup."""

REDUCER_PROMPT_ID = "reducer-checkpoint-v2"


# --------------------------------------------------------------------------- #
# revisions
# --------------------------------------------------------------------------- #

def _rev(rev_id: str, text: str) -> dict[str, str]:
    return {"id": rev_id, "sha256": hashlib.sha256(text.encode()).hexdigest()[:16]}




def revisions(
    *, controller: str | None = None, branch: bool = False, reducer: bool = False
) -> dict[str, dict[str, str]]:
    """Identify only the prompts a condition actually uses.

    A control arm that injects nothing reports an empty mapping rather than
    naming prompts it never sent. The private-tool surface is one artifact in
    two parts — the schema the condition offers and the notice it sends when
    the capability is withdrawn — so both are reported wherever the tool is.
    """
    out: dict[str, dict[str, str]] = {}
    if controller is not None:
        rev_id, text = CONTROLLER_PROMPTS[controller]
        out["controller"] = _rev(rev_id, text)
        wire = wire_for(controller)
        out["reason_tool"] = _rev(
            wire["tool_id"], json.dumps(wire["tool"], sort_keys=True)
        )
        out["reason_withdrawn"] = _rev(wire["withdrawn_id"], wire["withdrawn_notice"])
    if branch:
        out["branch"] = _rev(BRANCH_PROMPT_ID, BRANCH_SAME_AGENT_V2)
    if reducer:
        out["reducer"] = _rev(REDUCER_PROMPT_ID, REDUCER_CHECKPOINT_V2)
    return out
