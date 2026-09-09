"""Configuration: providers, model backends, virtual models.

A virtual model is one immutable experimental condition. Everything the paper
needs to pin lives here and is hashed into every trace record, so validation is
deliberately strict: a condition that cannot mean what it says (live
deliberation with no branches, a quorum larger than the fan-out, an inert
branch list on a control arm) is a definition error, not a runtime surprise.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from .prompts import BRANCH_ROLE_SUFFIXES

# Request-body keys the proxy owns end to end: the engine decides the model,
# the message list, the tool surface, and never streams or fans out upstream.
# A config or caller that sets them would silently break the protocol, so they
# are rejected here and stripped in the transport.
TRANSPORT_OWNED = frozenset(
    {"model", "messages", "tools", "tool_choice", "stream", "stream_options", "n"}
)

# Total attempts, including the initial request.
DEFAULT_MAX_ATTEMPTS = 4


class Provider(BaseModel):
    """An OpenAI-compatible HTTP endpoint."""

    base_url: str
    api_key_env: str = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = Field(default=300.0, gt=0)
    max_attempts: int = Field(default=DEFAULT_MAX_ATTEMPTS, ge=1, le=16)

    @model_validator(mode="after")
    def _check(self) -> Provider:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError(f"base_url must be an http(s) URL, got {self.base_url!r}")
        return self

    def api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise RuntimeError(f"{self.api_key_env} is not set")
        return key


class Backend(BaseModel):
    """A concrete (provider, model, sampling) triple.

    `params` is passed through verbatim so provider-specific knobs
    (`reasoning_effort`, `thinking_level`, `thinking`, ...) need no abstraction.
    No key here is interpreted, and nothing is defaulted on the model's behalf.

    `max_concurrent` and `rpm` exist because provider rate limits are per model,
    not per backend name: Workers AI caps frontier models at 20
    requests/minute/account (50 with prepaid AI Gateway credits), which a K-way
    fan-out crosses immediately when several benchmark tasks run at once. Two
    backends naming the same (provider, model) share one gate upstream.
    """

    provider: str
    model: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    max_concurrent: int = Field(default=4, ge=1)
    rpm: int | None = Field(default=None, ge=1)


class VirtualModel(BaseModel):
    """One experimental condition, exposed to callers as a single model id.

    `reason_mode` is the condition axis:

    * `live`   - the reason tool is offered and really fans out to branches;
    * `noop`   - the identical prompt and tool are offered, but a call returns
                 a fixed neutral "no deliberation available" result, so the
                 intervention is isolated from the mere presence of the tool;
    * `off`    - no instruction injection and no interception at all: the plain
                 model, for the baseline arm.
    """

    controller: str
    reason_mode: Literal["live", "noop", "off"] = "live"
    branches: list[str] = Field(default_factory=list)
    reducer: str | None = None
    branch_prompt: Literal["same_agent", "roles"] = "same_agent"
    branch_roles: list[str] = Field(default_factory=list)
    controller_prompt: Literal[
        "neutral", "outsourcing", "concise", "concise-outsourcing", "deliberate", "deliberate-focus"
    ] = "concise"
    persistence: Literal["auto", "assistant_tags", "ephemeral"] = "auto"
    # Loop bound, not a budget: the last iteration has no reason tool, so the
    # controller is forced to emit an externally visible response.
    max_reason_calls: int = Field(default=2, ge=0)
    min_branches: int = Field(default=1, ge=1)  # quorum, checked against branches
    branch_timeout_s: float = Field(default=120.0, gt=0)
    reduce: bool = True  # False => deterministic concatenation (ablation)

    @model_validator(mode="after")
    def _check(self) -> VirtualModel:
        if self.reason_mode == "live":
            if not self.branches:
                raise ValueError("reason_mode=live requires branches")
            if self.reduce and not self.reducer:
                raise ValueError("reason_mode=live with reduce=true requires a reducer")
            if self.min_branches > len(self.branches):
                raise ValueError(
                    f"min_branches={self.min_branches} exceeds "
                    f"{len(self.branches)} branch(es): quorum can never be met"
                )
            if self.max_reason_calls < 1:
                raise ValueError("reason_mode=live requires max_reason_calls >= 1")
            if self.branch_prompt == "roles":
                if len(self.branch_roles) != len(self.branches):
                    raise ValueError(
                        f"branch_prompt=roles requires one role per branch: "
                        f"{len(self.branch_roles)} role(s), {len(self.branches)} branch(es)"
                    )
                unknown = [r for r in self.branch_roles if r not in BRANCH_ROLE_SUFFIXES]
                if unknown:
                    raise ValueError(
                        f"unknown branch_roles {unknown}; "
                        f"known roles: {sorted(BRANCH_ROLE_SUFFIXES)}"
                    )
            return self

        # Control arms run no workers. Declaring them anyway means the condition
        # does not do what the file says, which is exactly the mistake that
        # invalidates a comparison, so it is rejected rather than ignored.
        dead = [k for k in ("branches", "reducer", "branch_roles") if getattr(self, k)]
        if self.branch_prompt == "roles":
            dead.append("branch_prompt=roles")
        if dead:
            raise ValueError(
                f"reason_mode={self.reason_mode} runs no branches; remove {', '.join(dead)}"
            )
        if self.reason_mode == "noop" and self.max_reason_calls < 1:
            raise ValueError(
                "reason_mode=noop still advertises the reason tool and "
                "requires max_reason_calls >= 1"
            )
        return self


class Config(BaseModel):
    providers: dict[str, Provider]
    backends: dict[str, Backend]
    virtual_models: dict[str, VirtualModel]
    trace_path: str | None = "runs/reasonproxy.jsonl"
    # Branch monologue and checkpoints are large and quotable; keep them only
    # when a run is explicitly doing trajectory analysis.
    trace_texts: bool = False

    @model_validator(mode="after")
    def _resolve(self) -> Config:
        if not self.virtual_models:
            raise ValueError("no virtual_models configured")
        for name, be in self.backends.items():
            if be.provider not in self.providers:
                raise ValueError(f"backend {name!r}: unknown provider {be.provider!r}")
            owned = sorted(TRANSPORT_OWNED & set(be.params))
            if owned:
                raise ValueError(
                    f"backend {name!r}: params may not set proxy-owned request keys {owned}"
                )
        for name, vm in self.virtual_models.items():
            refs = [("controller", vm.controller)]
            refs += [("branch", b) for b in vm.branches]
            if vm.reducer:
                refs.append(("reducer", vm.reducer))
            for role, ref in refs:
                if ref not in self.backends:
                    raise ValueError(
                        f"virtual model {name!r}: unknown {role} backend {ref!r}"
                    )
        return self

    @classmethod
    def load(cls, path: str | Path) -> Config:
        raw = Path(path).read_text()
        # Hash the file as written, so the manifest identifies the condition
        # rather than the machine it ran on.
        sha = hashlib.sha256(raw.encode()).hexdigest()[:16]
        cfg = cls.model_validate(yaml.safe_load(_expand(raw)))
        cfg._sha = sha  # type: ignore[attr-defined]
        return cfg

    @property
    def sha(self) -> str:
        return getattr(self, "_sha", "unhashed")


_ENV_REF = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand(raw: str) -> str:
    """Substitute ${VAR} in config text. Account ids and base URLs differ per
    operator; the condition definition should not."""

    def sub(m: re.Match[str]) -> str:
        val = os.environ.get(m.group(1))
        if val is None:
            raise RuntimeError(f"config references ${{{m.group(1)}}} but it is not set")
        return val

    return _ENV_REF.sub(sub, raw)
