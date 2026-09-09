"""Config is the experimental contract.

Every condition in a paper table is one of these blocks, so a definition that
cannot mean what it says — live deliberation with nothing to deliberate, a
quorum larger than the fan-out, a control arm carrying an inert branch list —
must fail at load rather than silently produce a run that measures something
else.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from reasonproxy.config import Config, Provider

BASE: dict = {
    "providers": {
        "fake": {"base_url": "http://provider.invalid/v1", "api_key_env": "FAKE_KEY"}
    },
    "backends": {
        "ctl": {"provider": "fake", "model": "controller-1"},
        "w1": {"provider": "fake", "model": "worker-1"},
        "w2": {"provider": "fake", "model": "worker-2"},
        "red": {"provider": "fake", "model": "reducer-1"},
    },
    "trace_path": None,
}

LIVE: dict = {
    "controller": "ctl",
    "branches": ["w1", "w2"],
    "reducer": "red",
    "min_branches": 2,
}


def cfg(**virtual_models) -> Config:
    return Config.model_validate(BASE | {"virtual_models": virtual_models})


def rejects(fragment: str, **virtual_models) -> None:
    with pytest.raises(ValidationError) as exc:
        cfg(**virtual_models)
    assert fragment in str(exc.value)


# --------------------------------------------------------------------------- #
# reason_mode
# --------------------------------------------------------------------------- #

def test_live_condition_keeps_the_research_defaults():
    c = cfg(rp=LIVE)
    vm = c.virtual_models["rp"]
    assert vm.reason_mode == "live"
    assert vm.controller_prompt == "concise"
    assert vm.branch_prompt == "same_agent"
    assert vm.persistence == "auto"
    # Texts are opt-in: branch monologue is large and quotable.
    assert c.trace_texts is False


def test_live_needs_workers_and_a_reducer_it_can_actually_use():
    rejects("reason_mode=live requires branches", rp={"controller": "ctl"})
    rejects("requires a reducer", rp={"controller": "ctl", "branches": ["w1"]})
    # The concatenation ablation is the one live shape with no reducer.
    ablation = cfg(rp={"controller": "ctl", "branches": ["w1"], "reduce": False})
    assert ablation.virtual_models["rp"].reducer is None


def test_a_quorum_larger_than_the_fan_out_is_rejected():
    rejects("quorum can never be met", rp=LIVE | {"min_branches": 3})
    assert cfg(rp=LIVE | {"min_branches": 2}).virtual_models["rp"].min_branches == 2


def test_live_without_a_reason_call_could_never_deliberate():
    rejects("requires max_reason_calls >= 1", rp=LIVE | {"max_reason_calls": 0})
    # The explicit safety bound itself is preserved, not reinterpreted.
    assert cfg(rp=LIVE | {"max_reason_calls": 7}).virtual_models["rp"].max_reason_calls == 7


def test_control_arms_reject_worker_config_they_would_never_run():
    rejects(
        "reason_mode=off runs no branches",
        ctl={"controller": "ctl", "reason_mode": "off", "branches": ["w1", "w2"]},
    )
    rejects(
        "reason_mode=noop runs no branches",
        noop={"controller": "ctl", "reason_mode": "noop", "reducer": "red"},
    )
    rejects(
        "branch_prompt=roles",
        noop={"controller": "ctl", "reason_mode": "noop", "branch_prompt": "roles"},
    )
    clean = cfg(
        ctl={"controller": "ctl", "reason_mode": "off"},
        noop={"controller": "ctl", "reason_mode": "noop"},
    )
    assert clean.virtual_models["ctl"].branches == []
    assert clean.virtual_models["noop"].reducer is None


def test_noop_still_advertises_the_tool_but_off_intercepts_nothing():
    rejects(
        "reason_mode=noop still advertises",
        noop={"controller": "ctl", "reason_mode": "noop", "max_reason_calls": 0},
    )
    plain = cfg(ctl={"controller": "ctl", "reason_mode": "off", "max_reason_calls": 0})
    assert plain.virtual_models["ctl"].max_reason_calls == 0


def test_every_arm_can_pin_the_same_controller_prompt():
    # ctl/noop/rp must be able to share one prompt choice, and the retired
    # 'disabled' value must not resurface as a hidden second way to say off.
    arms = cfg(
        ctl={"controller": "ctl", "reason_mode": "off", "controller_prompt": "concise"},
        noop={"controller": "ctl", "reason_mode": "noop", "controller_prompt": "concise"},
        rp=LIVE | {"controller_prompt": "concise"},
    )
    assert {vm.controller_prompt for vm in arms.virtual_models.values()} == {"concise"}
    rejects(
        "controller_prompt",
        ctl={"controller": "ctl", "reason_mode": "off", "controller_prompt": "disabled"},
    )


def test_branch_roles_must_name_real_role_suffixes():
    rejects(
        "unknown branch_roles ['sceptic']",
        rp=LIVE | {"branch_prompt": "roles", "branch_roles": ["sceptic", "challenger"]},
    )
    rejects(
        "one role per branch",
        rp=LIVE | {"branch_prompt": "roles", "branch_roles": ["challenger"]},
    )
    roles = cfg(rp=LIVE | {"branch_prompt": "roles",
                           "branch_roles": ["challenger", "evidence"]})
    assert roles.virtual_models["rp"].branch_roles == ["challenger", "evidence"]


# --------------------------------------------------------------------------- #
# references and ranges
# --------------------------------------------------------------------------- #

def test_dangling_backend_references_name_the_role_that_dangles():
    rejects("unknown controller backend 'nope'", rp=LIVE | {"controller": "nope"})
    rejects("unknown branch backend 'w9'", rp=LIVE | {"branches": ["w1", "w9"]})
    rejects("unknown reducer backend 'nope'", rp=LIVE | {"reducer": "nope"})

    with pytest.raises(ValidationError) as exc:
        Config.model_validate(
            BASE
            | {
                "backends": {"x": {"provider": "zen", "model": "m"}},
                "virtual_models": {"ctl": {"controller": "x", "reason_mode": "off"}},
            }
        )
    assert "backend 'x': unknown provider 'zen'" in str(exc.value)


def test_backend_params_stay_generic_but_cannot_hijack_the_call():
    passthrough = Config.model_validate(
        BASE
        | {
            "backends": {
                "ctl": {
                    "provider": "fake",
                    "model": "controller-1",
                    "params": {"temperature": 0.2, "thinking": {"type": "enabled"},
                               "reasoning_effort": "high"},
                }
            },
            "virtual_models": {"ctl": {"controller": "ctl", "reason_mode": "off"}},
        }
    )
    # Verbatim: no key is interpreted and no output cap is invented.
    assert passthrough.backends["ctl"].params == {
        "temperature": 0.2,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }

    with pytest.raises(ValidationError) as exc:
        Config.model_validate(
            BASE
            | {
                "backends": {
                    "ctl": {"provider": "fake", "model": "controller-1",
                            "params": {"model": "somewhere-else", "stream": True}}
                },
                "virtual_models": {"ctl": {"controller": "ctl", "reason_mode": "off"}},
            }
        )
    assert "proxy-owned request keys ['model', 'stream']" in str(exc.value)


@pytest.mark.parametrize(
    "patch, fragment",
    [
        ({"backends": {"ctl": {"provider": "fake", "model": "m", "max_concurrent": 0}}},
         "greater than or equal to 1"),
        ({"backends": {"ctl": {"provider": "fake", "model": "m", "rpm": 0}}},
         "greater than or equal to 1"),
        ({"backends": {"ctl": {"provider": "fake", "model": ""}}},
         "at least 1 character"),
        ({"providers": {"fake": {"base_url": "provider.invalid/v1",
                                 "api_key_env": "FAKE_KEY"}}},
         "base_url must be an http(s) URL"),
        ({"providers": {"fake": {"base_url": "http://x/v1", "api_key_env": "FAKE_KEY",
                                 "timeout_s": 0}}},
         "greater than 0"),
        ({"virtual_models": {}}, "no virtual_models configured"),
    ],
)
def test_limits_that_could_not_run_are_rejected(patch, fragment):
    base = BASE | {"virtual_models": {"ctl": {"controller": "ctl", "reason_mode": "off"}}}
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(base | patch)
    assert fragment in str(exc.value)


def test_branch_deadline_must_be_a_real_deadline():
    rejects("greater than 0", rp=LIVE | {"branch_timeout_s": 0})


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #

def test_load_expands_env_refs_and_hashes_the_file_as_written(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNT", "acct-123")
    text = yaml.safe_dump(
        BASE
        | {
            "providers": {
                "fake": {"base_url": "http://api.invalid/${ACCOUNT}/v1",
                         "api_key_env": "FAKE_KEY"}
            },
            "virtual_models": {"rp": LIVE},
        }
    )
    path = tmp_path / "research.yaml"
    path.write_text(text)

    cfg = Config.load(path)
    assert cfg.providers["fake"].base_url == "http://api.invalid/acct-123/v1"
    # The sha identifies the condition file, not the expanded machine state.
    assert len(cfg.sha) == 16
    monkeypatch.setenv("ACCOUNT", "acct-999")
    assert Config.load(path).sha == cfg.sha

    path.write_text(text + "\n# a comment is a new condition file\n")
    assert Config.load(path).sha != cfg.sha


def test_load_refuses_a_config_whose_env_ref_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCOUNT", raising=False)
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(BASE | {"virtual_models": {"rp": LIVE}}).replace(
        "http://provider.invalid/v1", "http://api.invalid/${ACCOUNT}/v1"
    ))
    with pytest.raises(RuntimeError, match=r"\$\{ACCOUNT\}"):
        Config.load(path)


def test_api_key_names_the_variable_the_operator_must_set(monkeypatch):
    p = Provider(base_url="http://x/v1", api_key_env="MISSING_TOKEN")
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="MISSING_TOKEN is not set"):
        p.api_key()
    monkeypatch.setenv("MISSING_TOKEN", "value")
    assert p.api_key() == "value"
