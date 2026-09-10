#!/usr/bin/env python3
"""Freeze, run and analyze official benchmark trials through ReasonProxy.

Three commands, in order:

    freeze    resolve a predeclaration into an immutable manifest: harness
              versions, clone HEAD, per-task identity hashes, roster/prompt/
              source hashes, host facts, and the fixed trial plan.
    run       execute a subset of that plan by driving the *official* CLI
              (`pier run` / `harbor run`) with a generated job config, launched
              through scripts/native_harness.py so the harness's leaked
              cancellation of a main-container `docker compose exec` cannot
              leave the agent running into the verifier phase.
    analyze   read native rewards out of the harness trial dirs, adjudicate
              outcomes, and compute paired task-level statistics.

This is orchestration only. It contains no agent, no task logic, no retry
policy, no adaptive budget, and it never synthesizes a missing trial score.
Both harnesses are used exactly as installed, with exactly one recorded
runtime patch: the Docker exec cancellation guard, whose policy digest, patch
target digest and bootstrap bytes are frozen into the manifest and re-checked
before every run (see scripts/native_harness.py).

    python scripts/benchmark.py freeze --suite deepswe
    python scripts/benchmark.py run --frozen runs/frozen/deepswe-<stamp>.json --conformance
    python scripts/benchmark.py run --frozen runs/frozen/deepswe-<stamp>.json --tasks skrub-duration-encoding
    python scripts/benchmark.py analyze --frozen runs/frozen/deepswe-<stamp>.json

Use the explicit paths `freeze` prints. `--frozen latest` is a convenience that
requires --suite: runs/frozen holds one manifest per suite and "latest" is a
lexical glob, not a sort by time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]

MODES = ("ctl", "rp", "noop")
EXPECTED_REASON_MODE = {"ctl": "off", "rp": "live", "noop": "noop"}

# Suite wiring. `tasks_subdir` is where task directories live inside the clone:
# DeepSWE keeps them under tasks/, terminal-bench-2 keeps them at the root.
SUITES: dict[str, dict[str, Any]] = {
    "deepswe": {
        "runner": "pier",
        "predeclaration": "experiments/deepswe/pilot.json",
        "clone": "~/.cache/reasonproxy/deep-swe",
        "tasks_subdir": "tasks",
        "template": "experiments/deepswe/mini-swe.yaml",
        "job_root": "runs/deepswe-pilot",
        # Reserved dev/conformance tasks come from the predeclaration itself.
        "reserved": None,
    },
    "terminal-bench-2": {
        "runner": "harbor",
        "predeclaration": "runs/onboarding/terminal-bench-selection.json",
        "clone": "~/.cache/reasonproxy/terminal-bench-2",
        "tasks_subdir": "",
        "template": None,
        "job_root": "runs/tb2-replication",
        # Main reserved this task for Harbor grader conformance after the
        # frozen replication subset was fixed; it is not in that subset.
        "reserved": ["cancel-async-tasks"],
    },
    # Terminal-Bench 4.0 (66 tasks, harbor dataset terminal-bench@4.0.0). Same
    # layout as 2.0, but the tasks declare 8-hour agent timeouts and up to 32 GiB
    # / 16 CPUs, so most of the pool cannot run on a 16 GiB laptop at all: this
    # suite is wired for cloud sandboxes (harbor --env modal/daytona) and for
    # the memory-feasible subset locally.
    "terminal-bench-4": {
        "runner": "harbor",
        "predeclaration": "runs/onboarding/terminal-bench-4-selection.json",
        "clone": "~/.cache/reasonproxy/terminal-bench",
        "tasks_subdir": "",
        "template": None,
        "job_root": "runs/tb4",
        "reserved": None,
    },
}

RUNNER_EXECUTABLE = {"pier": "pier", "harbor": "harbor"}
RUNNER_DIST = {"pier": "datacurve_pier", "harbor": "harbor"}

# The native CLI is not invoked directly. Both harnesses leak a cancelled
# main-container `docker compose exec`: they kill only the host docker client,
# so the in-container agent keeps running - keeps calling the model and keeps
# mutating the workspace - while the verifier phase scores it. This bootstrap
# installs a narrow Docker exec cancellation guard into the harness's own
# interpreter and then runs the original CLI entrypoint with unchanged
# arguments. It is an explicit, hashed runtime patch, not a custom agent.
NATIVE_HARNESS_REL = "scripts/native_harness.py"

# Files whose bytes define a run. `freeze` hashes all of them; `run` re-checks
# the proxy sources, the roster, this orchestrator, the native bootstrap and
# the suite's job-config template against the manifest, plus the
# predeclaration against its frozen hash, so no mid-experiment edit to
# anything that shapes a trial can pass silently. Include the local Worker
# transport as well as the Python proxy.
PROXY_SOURCE_GLOBS = ("src/reasonproxy/*.py", "workers/ai/*.js", "workers/ai/*.jsonc")
EXPERIMENT_SOURCE_GLOBS = (
    "experiments/deepswe/*.json",
    "experiments/deepswe/*.yaml",
    "scripts/benchmark.py",
    "scripts/hle.py",
    "scripts/live_smoke.py",
    NATIVE_HARNESS_REL,
)

SENSITIVE_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH)", re.IGNORECASE)

# `${VAR}` text in a roster or a job config names an environment variable; the
# harness resolves it at construction time, so the template - never the value -
# is what an artifact is allowed to carry.
ENV_TEMPLATE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


def redact(text: str) -> str:
    """Replace any host secret value with a named placeholder."""
    out = text
    for key, value in os.environ.items():
        if value and len(value) >= 8 and SENSITIVE_ENV.search(key):
            out = out.replace(value, f"<redacted:{key}>")
    return out


def _looks_like_secret(text: str) -> bool:
    """True for text shaped like a credential value, not like a name or scheme.

    Applied to roster header values with `${VAR}` templates removed first, so
    ``Authorization: Bearer ${OPENCODE_API_KEY}`` leaves only "Bearer " behind
    and passes, while an inlined token does not.
    """
    if re.search(r"sk-[A-Za-z0-9_-]{8,}", text) or re.search(r"\bey[A-Za-z0-9]{8,}\.", text):
        return True
    return any(
        len(token) >= 16 and any(c.isdigit() for c in token) and any(c.isalpha() for c in token)
        for token in re.split(r"[^A-Za-z0-9_-]+", text)
    )


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def run_text(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


# --------------------------------------------------------------------------
# environment / pin resolution
# --------------------------------------------------------------------------


def load_pins() -> dict[str, Any]:
    raw = (REPO / "pyproject.toml").read_bytes()
    doc = tomllib.loads(raw.decode())
    pins = doc.get("tool", {}).get("reasonproxy", {}).get("harnesses", {})
    return {"pins": pins, "pyproject_sha256": sha256_bytes(raw)}


def dist_version(site_packages: Path, dist: str) -> str | None:
    for entry in sorted(site_packages.glob(f"{dist}-*.dist-info")):
        name = entry.name[: -len(".dist-info")]
        if "-" in name:
            return name.rsplit("-", 1)[1]
    return None


def tool_info(executable: str, dist: str) -> dict[str, Any]:
    which = shutil.which(executable)
    if which is None:
        return {"executable": executable, "found": False, "error": "not on PATH"}
    real = Path(os.path.realpath(which))
    info: dict[str, Any] = {
        "executable": executable,
        "found": True,
        "path": str(which),
        "resolved_path": str(real),
        "version": None,
        "version_source": None,
    }
    for site in sorted(real.parents[1].glob("lib/python3*/site-packages")):
        version = dist_version(site, dist)
        if version:
            info["version"] = version
            info["version_source"] = str(site / f"{dist}-{version}.dist-info")
            break
    if info["version"] is None:
        code, text = run_text([which, "--version"])
        if code == 0:
            match = re.search(r"(\d+\.\d+[^\s]*)", text)
            info["version"] = match.group(1) if match else text
            info["version_source"] = f"{executable} --version"
    return info


# --------------------------------------------------------------------------
# native exec cancellation guard
# --------------------------------------------------------------------------


def native_python(tool: dict[str, Any]) -> str | None:
    """The harness's own venv interpreter: the `python` next to its CLI script.

    The guard patches objects inside the harness's process, so it has to be
    installed by the same interpreter that imports the harness. uv installs
    each tool into its own venv, so that interpreter is a sibling of the
    console script the CLI resolves to.
    """
    resolved = tool.get("resolved_path") or tool.get("path")
    if not resolved:
        return None
    bin_dir = Path(resolved).parent
    for name in ("python3", "python"):
        candidate = bin_dir / name
        if candidate.exists():
            return str(candidate)
    return None


def native_guard_snapshot(runner: str, tool: dict[str, Any]) -> dict[str, Any]:
    """Identify the bootstrap, its effective policy, and the runtime it patches.

    Produced by the bootstrap itself, under the harness interpreter, so the
    record is what will actually run rather than this orchestrator's guess.
    `policy_digest` covers the cancellation policy (signals, kill ladder,
    fail-closed rules, in-container script bytes); `target_digest` covers the
    installed harness modules and the exec-path method bodies the policy
    reasons about. Both are frozen and re-checked before a run.
    """
    bootstrap = REPO / NATIVE_HARNESS_REL
    snapshot: dict[str, Any] = {
        "bootstrap": NATIVE_HARNESS_REL,
        "bootstrap_sha256": sha256_file(bootstrap),
        "venv_python": native_python(tool),
        "launch_template": [
            "<venv_python>",
            NATIVE_HARNESS_REL,
            runner,
            "<native_cli_path>",
            "run",
            "-c",
            "<job config>",
            "-y",
        ],
    }
    if not bootstrap.exists():
        snapshot["error"] = f"{NATIVE_HARNESS_REL} is missing"
        return snapshot
    python = snapshot["venv_python"]
    if not python:
        snapshot["error"] = f"no venv interpreter next to {tool.get('resolved_path')}"
        return snapshot
    code, text = run_text([python, str(bootstrap), "--describe", runner])
    if code != 0:
        snapshot["error"] = f"--describe {runner} exited {code}: {redact(text)[:400]}"
        return snapshot
    try:
        described = json.loads(text)
    except json.JSONDecodeError:
        snapshot["error"] = f"--describe {runner} returned non-JSON: {redact(text)[:200]}"
        return snapshot
    policy = described.get("policy") or {}
    runtime = described.get("runtime") or {}
    snapshot.update(
        {
            "install_ok": bool(described.get("install_ok")),
            "policy_version": policy.get("version"),
            "policy_digest": policy.get("digest"),
            "cancellation_policy": policy,
            "target_digest": runtime.get("target_digest"),
            "patched_runtime": runtime,
        }
    )
    if not snapshot["install_ok"]:
        snapshot["error"] = described.get("error") or "guard install failed"
    elif not (snapshot["policy_digest"] and snapshot["target_digest"]):
        snapshot["error"] = "guard description is missing a digest"
    return snapshot


def clone_state(path: Path) -> dict[str, Any]:
    """Identify a task clone, whether it is a git checkout or a registry download.

    Terminal-Bench 2.0 arrives as a git clone, so its HEAD is the honest pin.
    Terminal-Bench 4.0 arrives from the Harbor registry as plain files with no
    history, so there is no commit to compare against and a missing-HEAD check
    would reject a perfectly identified dataset. What a manifest actually needs
    to pin is *which tasks these are*, so a non-git clone is pinned by the
    digest of every `task.toml` in it, keyed by task name. Either way the
    identity is content-addressed and a mid-experiment edit cannot pass.
    """
    if (path / ".git").exists():
        code, text = run_text(["git", "-C", str(path), "rev-parse", "HEAD"])
        if code != 0:
            return {"path": str(path), "kind": "git", "head": None, "error": text}
        dirty_code, dirty = run_text(["git", "-C", str(path), "status", "--porcelain"])
        return {
            "path": str(path),
            "kind": "git",
            "head": text.strip(),
            "dirty": bool(dirty.strip()) if dirty_code == 0 else None,
        }
    if not path.is_dir():
        return {"path": str(path), "kind": "missing", "head": None, "error": "no such directory"}
    manifests = sorted(path.glob("*/task.toml"))
    if not manifests:
        return {
            "path": str(path),
            "kind": "unknown",
            "head": None,
            "error": "neither a git clone nor a task directory",
        }
    rolling = hashlib.sha256()
    for manifest in manifests:
        rolling.update(manifest.parent.name.encode())
        rolling.update(b"\t")
        rolling.update(sha256_bytes(manifest.read_bytes()).encode())
        rolling.update(b"\n")
    return {
        "path": str(path),
        "kind": "registry",
        "head": None,
        "tasks": len(manifests),
        "content_sha256": rolling.hexdigest(),
        "note": "no git history; identity is the digest of every task.toml in the clone",
    }


def docker_snapshot() -> dict[str, Any]:
    code, text = run_text(["docker", "info", "--format", "{{json .}}"])
    if code != 0:
        return {"available": False, "error": redact(text)[:400]}
    try:
        info = json.loads(text)
    except json.JSONDecodeError:
        return {"available": False, "error": "docker info returned non-JSON"}
    return {
        "available": True,
        "server_version": info.get("ServerVersion"),
        "architecture": info.get("Architecture"),
        "os_type": info.get("OSType"),
        "ncpu": info.get("NCPU"),
        "mem_total_bytes": info.get("MemTotal"),
    }


def host_snapshot() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": sys.version.split()[0],
        "docker": docker_snapshot(),
        "emulation_note": (
            "DeepSWE images are linux/amd64; on an arm64 host they execute under "
            "emulation, which consumes part of the official agent budget."
        ),
    }


def source_digest() -> dict[str, Any]:
    files: dict[str, str] = {}
    for pattern in (*PROXY_SOURCE_GLOBS, *EXPERIMENT_SOURCE_GLOBS):
        for path in sorted(REPO.glob(pattern)):
            digest = sha256_file(path)
            if digest:
                files[str(path.relative_to(REPO))] = digest
    proxy_only = {k: v for k, v in files.items() if k.startswith(("src/reasonproxy/", "workers/ai/"))}
    return {
        "files": files,
        "combined_sha256": sha256_bytes(
            "".join(f"{k}\t{v}\n" for k, v in sorted(files.items())).encode()
        ),
        "proxy_sha256": sha256_bytes(
            "".join(f"{k}\t{v}\n" for k, v in sorted(proxy_only.items())).encode()
        ),
        "note": "No git commit exists for this working tree; these are content hashes.",
    }


# --------------------------------------------------------------------------
# predeclaration
# --------------------------------------------------------------------------


class Predeclaration:
    """Normalized view over a suite's frozen, pre-outcome declaration."""

    def __init__(self, suite: str, wiring: dict[str, Any], raw: dict[str, Any], path: Path):
        self.suite = suite
        self.raw = raw
        self.path = path
        self.sha256 = sha256_bytes(path.read_bytes())
        self.runner: str = wiring["runner"]
        self.clone = Path(wiring["clone"]).expanduser()
        self.tasks_subdir: str = wiring["tasks_subdir"]
        self.template = Path(REPO / wiring["template"]) if wiring["template"] else None
        self.job_root = wiring["job_root"]

        if raw.get("schema") == "reasonproxy.predeclaration/1":
            dataset = raw["dataset"]
            self.repository = dataset["repository"]
            self.commit = dataset["commit"]
            self.tasks = [t["id"] for t in raw["tasks"]]
            self.task_meta = {t["id"]: t for t in raw["tasks"]}
            self.reserved = list(dataset.get("reserved_tasks", {}))
            self.conditions = [c["alias"] for c in raw["conditions"]]
            self.repeats = int(raw["trials_per_task_condition"])
            self.family_order = list(raw["execution_order"]["family_order"])
            self.mode_order = list(raw["execution_order"]["mode_order"])
            self.primary_metric = raw["primary_metric"]["name"]
            self.adjudication = raw["outcome_adjudication"]
            self.contrasts = raw["contrasts"]
            self.adjudication_source = {
                "declared_in": str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path),
                "sha256": self.sha256,
            }
            self.harness = raw["harness"]
        else:
            # Main's terminal-bench selection artifact.
            self.repository = raw["repository"]
            self.commit = raw["commit"]
            self.tasks = list(raw["tasks"])
            self.task_meta = {t: {"id": t} for t in self.tasks}
            self.reserved = list(wiring.get("reserved") or [])
            self.conditions = list(raw["conditions"])
            self.repeats = int(raw.get("trials_per_task_condition", 1))
            self.family_order = _first_seen([alias.split("/", 1)[1] for alias in self.conditions])
            self.mode_order = [m for m in MODES if any(a.startswith(m + "/") for a in self.conditions)]
            self.primary_metric = raw.get("primary_metric", "official binary reward")
            borrowed, borrowed_source = _borrowed_from_pilot()
            self.adjudication = borrowed["outcome_adjudication"]
            self.contrasts = raw.get("contrasts") or borrowed["contrasts"]
            self.adjudication_source = borrowed_source
            self.harness = {
                "agent_step_limit": 250,
                "cost_limit": "disabled",
                "timeout_multiplier": float(raw.get("timeout_multiplier", 1.0)),
                "n_concurrent_trials": 1,
                "external_job_concurrency": raw.get("external_job_concurrency", 1),
                "official_limits_unchanged": {
                    "agent_timeout_sec": raw.get("native_agent_timeout_sec")
                },
                "timeout_deviation_note": (
                    "1.0 keeps official task timeouts; any other value is a "
                    "declared deviation recorded in the manifest"
                ),
            }

    @property
    def tasks_root(self) -> Path:
        return self.clone / self.tasks_subdir if self.tasks_subdir else self.clone


def _first_seen(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def _borrowed_from_pilot() -> tuple[dict[str, Any], dict[str, Any]]:
    """The DeepSWE predeclaration, for a suite whose own artifact omits a section.

    Read once, at freeze time, and copied into the manifest together with the
    hash it was read at. Nothing reads it during analysis: an analysis
    adjudicates from its own manifest only, so editing this file after a freeze
    cannot change how an already-executed trial is scored.
    """
    relative = SUITES["deepswe"]["predeclaration"]
    raw = (REPO / relative).read_bytes()
    return json.loads(raw), {
        "borrowed_from": relative,
        "sha256": sha256_bytes(raw),
        "reason": "this predeclaration declares no outcome_adjudication or contrasts of its own",
    }


def load_predeclaration(suite: str, override: str | None = None) -> Predeclaration:
    if suite not in SUITES:
        raise SystemExit(f"unknown suite {suite!r}; choose from {sorted(SUITES)}")
    wiring = SUITES[suite]
    path = Path(override) if override else REPO / wiring["predeclaration"]
    if not path.is_absolute():
        path = REPO / path
    if not path.exists():
        raise SystemExit(f"predeclaration missing: {path}")
    return Predeclaration(suite, wiring, json.loads(path.read_text()), path)


# --------------------------------------------------------------------------
# task identity
# --------------------------------------------------------------------------


def task_identity(task_dir: Path) -> dict[str, Any]:
    toml_path = task_dir / "task.toml"
    raw = toml_path.read_bytes()
    cfg = tomllib.loads(raw.decode())
    task = cfg.get("task", {})
    meta = cfg.get("metadata", {})
    agent = cfg.get("agent", {})
    verifier = cfg.get("verifier", {})
    environment = cfg.get("environment", {})
    verifier_env = verifier.get("environment", {})
    hashes = {"task.toml": sha256_bytes(raw)}
    instruction = task_dir / "instruction.md"
    if instruction.exists():
        hashes["instruction.md"] = sha256_file(instruction)
        instruction_bytes = instruction.stat().st_size
    else:
        instruction_bytes = None
    return {
        "id": task_dir.name,
        "path": str(task_dir),
        "declared_name": task.get("name"),
        "language": meta.get("language"),
        "category": meta.get("category"),
        "base_commit_hash": meta.get("base_commit_hash"),
        "schema_version": cfg.get("schema_version"),
        "instruction_bytes": instruction_bytes,
        "official_limits": {
            "agent_timeout_sec": agent.get("timeout_sec"),
            "agent_network_mode": agent.get("network_mode"),
            "verifier_timeout_sec": verifier.get("timeout_sec"),
            "verifier_network_mode": verifier.get("network_mode"),
            "verifier_environment_mode": verifier.get("environment_mode"),
            "environment_build_timeout_sec": environment.get("build_timeout_sec")
            or verifier_env.get("build_timeout_sec"),
            "cpus": environment.get("cpus"),
            "memory_mb": environment.get("memory_mb"),
            "storage_mb": environment.get("storage_mb"),
            "gpus": environment.get("gpus"),
            "allow_internet": environment.get("allow_internet"),
            "docker_image": environment.get("docker_image"),
        },
        "hashes": hashes,
    }


# --------------------------------------------------------------------------
# roster snapshot
# --------------------------------------------------------------------------


def roster_snapshot(roster_path: Path, aliases: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Read the roster as raw bytes and resolve the requested aliases.

    Parsed unexpanded on purpose: ``${VAR}`` references stay literal, so the
    manifest records provider *variable names* and never a credential.
    """
    problems: list[str] = []
    raw = roster_path.read_bytes()
    doc = yaml.safe_load(raw.decode()) or {}
    backends = doc.get("backends") or {}
    providers = doc.get("providers") or {}
    virtual_models = doc.get("virtual_models") or {}

    def backend_view(name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        spec = backends.get(name)
        if spec is None:
            problems.append(f"roster: backend {name!r} referenced but not defined")
            return {"backend": name, "missing": True}
        return {
            "backend": name,
            "provider": spec.get("provider"),
            "model": spec.get("model"),
            "params": spec.get("params") or {},
            "max_concurrent": spec.get("max_concurrent"),
            "rpm": spec.get("rpm"),
        }

    resolved: dict[str, Any] = {}
    for alias in aliases:
        vm = virtual_models.get(alias)
        if vm is None:
            problems.append(f"roster: alias {alias!r} is not declared in {roster_path}")
            continue
        mode = alias.split("/", 1)[0]
        reason_mode = vm.get("reason_mode", "live")
        expected = EXPECTED_REASON_MODE.get(mode)
        if isinstance(reason_mode, bool):
            problems.append(
                f"roster: alias {alias!r} reason_mode parsed as boolean {reason_mode!r}; "
                "YAML 1.1 reads bare off/on as booleans, so quote it as \"off\""
            )
        elif expected and reason_mode != expected:
            problems.append(
                f"roster: alias {alias!r} has reason_mode {reason_mode!r}, contract requires {expected!r}"
            )
        if reason_mode == "live":
            if not vm.get("branches"):
                problems.append(f"roster: alias {alias!r} is live but declares no branches")
            if vm.get("reduce", True) and not vm.get("reducer"):
                problems.append(f"roster: alias {alias!r} is live with reduce but no reducer")
        elif vm.get("branches") or vm.get("reducer"):
            problems.append(
                f"roster: alias {alias!r} is {reason_mode!r} but declares branches/reducer"
            )
        resolved[alias] = {
            "reason_mode": reason_mode,
            "controller_prompt": vm.get("controller_prompt"),
            "max_reason_calls": vm.get("max_reason_calls"),
            "min_branches": vm.get("min_branches"),
            "branch_prompt": vm.get("branch_prompt"),
            "branch_roles": vm.get("branch_roles"),
            "branch_timeout_s": vm.get("branch_timeout_s"),
            "reduce": vm.get("reduce"),
            "persistence": vm.get("persistence"),
            "controller": backend_view(vm.get("controller")),
            "branches": [backend_view(b) for b in (vm.get("branches") or [])],
            "reducer": backend_view(vm.get("reducer")),
        }

    def provider_headers(name: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Declared provider headers, with literal credentials refused.

        Upstream merges these into every request, so they are part of a
        condition's definition and belong in the manifest. `${VAR}` text is kept
        verbatim - it names a variable, not a value - while a literal that looks
        like a credential, or any value on a sensitive header with no template in
        it, is replaced by a marker and recorded as a problem. Freezing must
        never be the step that writes a token into an artifact.
        """
        kept: dict[str, Any] = {}
        for header, value in (spec.get("headers") or {}).items():
            text = str(value)
            templated = ENV_TEMPLATE.search(text) is not None
            if _looks_like_secret(ENV_TEMPLATE.sub("", text)) or (
                SENSITIVE_ENV.search(header) and not templated
            ):
                problems.append(
                    f"roster: provider {name!r} header {header!r} carries a literal value; "
                    "declare it as a ${VAR} template so no credential can be frozen in"
                )
                kept[header] = "<rejected: literal value; use ${VAR}>"
            else:
                kept[header] = text
        return kept

    snapshot = {
        "path": str(roster_path.relative_to(REPO)) if roster_path.is_relative_to(REPO) else str(roster_path),
        "sha256": sha256_bytes(raw),
        "trace_path": doc.get("trace_path"),
        "trace_texts": doc.get("trace_texts"),
        "providers": {
            name: {
                "base_url_template": spec.get("base_url"),
                "api_key_env": spec.get("api_key_env"),
                "timeout_s": spec.get("timeout_s"),
                "max_attempts": spec.get("max_attempts"),
                "headers": provider_headers(name, spec),
            }
            for name, spec in providers.items()
        },
        "aliases": resolved,
        "credential_note": "Provider credentials live only in the host proxy process; only variable names are recorded here.",
    }
    return snapshot, problems


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def build_plan(pre: Predeclaration, tasks: list[str], repeats: int, tag: str) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for family in pre.family_order:
        for task in tasks:
            for mode in pre.mode_order:
                alias = f"{mode}/{family}"
                if alias not in pre.conditions:
                    continue
                for repeat in range(1, repeats + 1):
                    plan.append(
                        {
                            "order": len(plan) + 1,
                            "trial_key": f"{pre.suite}/{alias}/{task}/r{repeat}",
                            "condition": alias,
                            "family": family,
                            "mode": mode,
                            "task": task,
                            "repeat": repeat,
                            "tag": tag,
                        }
                    )
    return plan


# --------------------------------------------------------------------------
# freeze
# --------------------------------------------------------------------------


def cmd_freeze(args: argparse.Namespace) -> int:
    pre = load_predeclaration(args.suite, args.predeclaration)
    problems: list[str] = []

    pins_doc = load_pins()
    pins = pins_doc["pins"]
    runner_tool = tool_info(RUNNER_EXECUTABLE[pre.runner], RUNNER_DIST[pre.runner])
    host_mini = tool_info("mini-swe-agent", "mini_swe_agent")
    if not runner_tool.get("found"):
        problems.append(f"{pre.runner} CLI not found on PATH")
    native_guard = native_guard_snapshot(pre.runner, runner_tool)
    if native_guard.get("error"):
        problems.append(
            f"native exec cancellation guard unavailable: {native_guard['error']}"
        )
    pinned_runner = pins.get(pre.runner)
    if pinned_runner and runner_tool.get("version") != pinned_runner:
        problems.append(
            f"{pre.runner} version {runner_tool.get('version')} != pinned {pinned_runner}"
        )
    mini_pin = pins.get("mini_swe_agent")
    if not mini_pin:
        problems.append("pyproject [tool.reasonproxy.harnesses] is missing mini_swe_agent")

    clone = clone_state(pre.clone)
    if clone.get("kind") == "git":
        if clone.get("head") != pre.commit:
            problems.append(
                f"clone {pre.clone} is at {clone.get('head')}, predeclaration pins {pre.commit}"
            )
    elif clone.get("kind") == "registry":
        # A registry download carries a dataset version, not a commit, so the
        # predeclaration pins that version string and the manifest carries the
        # content digest that actually identifies the tasks.
        if not pre.commit:
            problems.append(
                f"clone {pre.clone} is a registry download; predeclaration must pin its "
                "dataset version"
            )
    else:
        problems.append(
            f"clone {pre.clone} is unusable: {clone.get('error') or clone.get('kind')}"
        )
    pinned_commit = pins.get(f"{pre.suite}_commit")
    if pinned_commit and pinned_commit != pre.commit:
        problems.append(
            f"pyproject pins {pre.suite} commit {pinned_commit}, predeclaration says {pre.commit}"
        )

    tasks_root = pre.tasks_root
    task_records: list[dict[str, Any]] = []
    reserved_records: list[dict[str, Any]] = []
    for task in pre.tasks:
        directory = tasks_root / task
        if not (directory / "task.toml").is_file():
            problems.append(f"task {task!r} not found under {tasks_root}")
            continue
        record = task_identity(directory)
        declared = pre.task_meta.get(task, {})
        if declared.get("language") and record["language"] != declared["language"]:
            problems.append(
                f"task {task!r} declares language {record['language']!r}, predeclaration says {declared['language']!r}"
            )
        task_records.append(record)
    for task in pre.reserved:
        if task in pre.tasks:
            problems.append(f"reserved task {task!r} also appears in the scored task list")
            continue
        directory = tasks_root / task
        if (directory / "task.toml").is_file():
            reserved_records.append(task_identity(directory))
        else:
            problems.append(f"reserved task {task!r} not found under {tasks_root}")

    roster_path = Path(args.roster) if os.path.isabs(args.roster) else REPO / args.roster
    if not roster_path.exists():
        raise SystemExit(f"roster not found: {roster_path}")
    roster, roster_problems = roster_snapshot(roster_path, pre.conditions)
    problems.extend(roster_problems)

    plan = build_plan(pre, pre.tasks, pre.repeats, "pilot")
    conformance_plan = build_plan(pre, [t["id"] for t in reserved_records], 1, "conformance")

    proxy_url = args.proxy_url
    manifest = {
        "schema": "reasonproxy.frozen/1",
        "frozen_utc": utc_now(),
        "suite": pre.suite,
        "runner": pre.runner,
        "primary_metric": pre.primary_metric,
        # Frozen copies, not references: an analysis adjudicates from these, so
        # a later edit to a predeclaration cannot rescore an executed trial.
        "adjudication": pre.adjudication,
        "adjudication_source": pre.adjudication_source,
        "contrasts": pre.contrasts,
        "predeclaration": {
            "path": str(pre.path.relative_to(REPO)),
            "sha256": pre.sha256,
            "document": pre.raw,
        },
        "pins": {"pyproject_sha256": pins_doc["pyproject_sha256"], **pins},
        "tools": {
            pre.runner: {**runner_tool, "venv_python": native_guard.get("venv_python")},
            "mini_swe_agent_host": host_mini,
            "mini_swe_agent_in_sandbox": {
                "version": mini_pin,
                "pin_mechanism": "agent kwarg `version` -> uv tool install mini-swe-agent==<version> inside the sandbox",
            },
            "native_harness_bootstrap": native_guard,
        },
        "dataset": {
            "repository": pre.repository,
            "expected_commit": pre.commit,
            "clone": clone,
            "tasks_root": str(tasks_root),
            "pool_size": sum(1 for p in tasks_root.iterdir() if (p / "task.toml").is_file())
            if tasks_root.is_dir()
            else None,
        },
        "tasks": task_records,
        "reserved_tasks": reserved_records,
        "roster": roster,
        "sources": source_digest(),
        "host": host_snapshot(),
        "harness_settings": {
            "model_name_template": "hosted_vllm/<alias>",
            "proxy_url_in_sandbox": proxy_url,
            "agent_phase_allowlist": [urllib.parse.urlsplit(proxy_url).hostname],
            "agent_step_limit": int(args.step_limit),
            "cost_limit": "disabled (agent.cost_limit=0, MSWEA_COST_TRACKING=ignore_errors)",
            "added_wall_clock_cap": None,
            "timeout_multiplier": float(pre.harness["timeout_multiplier"]),
            "n_concurrent_trials": 1,
            "retries": 0,
            "official_task_limits": (
                "unchanged"
                if float(pre.harness["timeout_multiplier"]) == 1.0
                else f"agent timeout x{float(pre.harness['timeout_multiplier'])} (declared deviation)"
            ),
            # Not a limit change: the guard only decides what happens to the
            # in-container process once the harness's own timeout has already
            # fired. Models, prompts, step/cost limits, task timeouts and the
            # verifier are untouched.
            "native_exec_cancellation_guard": {
                "bootstrap": native_guard.get("bootstrap"),
                "bootstrap_sha256": native_guard.get("bootstrap_sha256"),
                "policy_version": native_guard.get("policy_version"),
                "policy_digest": native_guard.get("policy_digest"),
                "target_digest": native_guard.get("target_digest"),
                "install_ok": native_guard.get("install_ok"),
                "error": native_guard.get("error"),
                "summary": (native_guard.get("cancellation_policy") or {}).get("summary"),
            },
            "declared": pre.harness,
        },
        "conditions": pre.conditions,
        "plan": plan,
        "conformance_plan": conformance_plan,
        "counts": {
            "tasks": len(task_records),
            "conditions": len(pre.conditions),
            "repeats": pre.repeats,
            "planned_trials": len(plan),
            "planned_conformance_trials": len(conformance_plan),
        },
        "job_root_default": pre.job_root,
        "problems": problems,
    }

    out = Path(args.out) if args.out else REPO / "runs/frozen" / f"{pre.suite}-{now_stamp()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n")

    print(f"frozen manifest: {out}")
    print(f"  suite            {pre.suite} via {pre.runner} {runner_tool.get('version')}")
    print(f"  clone HEAD       {clone.get('head')} (expected {pre.commit})")
    print(f"  tasks            {len(task_records)} scored, {len(reserved_records)} reserved")
    print(f"  conditions       {len(pre.conditions)}")
    print(f"  planned trials   {len(plan)} (+{len(conformance_plan)} conformance)")
    print(f"  primary metric   {pre.primary_metric}")
    source = pre.adjudication_source
    print(f"  adjudication     frozen from {source.get('declared_in') or source.get('borrowed_from')}")
    print(f"  source digest    {manifest['sources']['combined_sha256'][:16]}")
    print(f"  roster sha       {roster['sha256'][:16]}  trace_path {roster.get('trace_path')}")
    guard = manifest["harness_settings"]["native_exec_cancellation_guard"]
    print(
        f"  exec guard       {guard.get('policy_version')} "
        f"policy {str(guard.get('policy_digest'))[:12]} "
        f"target {str(guard.get('target_digest'))[:12]} "
        f"install_ok={guard.get('install_ok')}"
    )
    print()
    print("next commands:")
    rel = out.relative_to(REPO) if out.is_relative_to(REPO) else out
    print(f"  python scripts/benchmark.py run --frozen {rel} --conformance")
    print(f"  python scripts/benchmark.py run --frozen {rel} --limit 3")
    print(f"  python scripts/benchmark.py run --frozen {rel}")
    print(f"  python scripts/benchmark.py analyze --frozen {rel}")
    if problems:
        print()
        print(f"{len(problems)} problem(s) recorded in the manifest:")
        for problem in problems:
            print(f"  ! {problem}")
        return 1
    return 0


# --------------------------------------------------------------------------
# job config generation
# --------------------------------------------------------------------------


def mini_swe_config(step_limit: int, api_base: str, session_id: str | None) -> dict[str, Any]:
    """mini-swe-agent overrides layered on top of the packaged mini.yaml.

    Both adapters pass the builtin mini config first and this document second,
    so only these keys change: step budget, disabled cost limit, endpoint, and
    a per-trial session header. api_base lives here (rather than in adapter
    model_kwargs) because Pier derives the agent-phase network allowlist from
    URL-shaped keys in this document.

    ``session_id`` is echoed by litellm as the X-Session-ID request header, which
    the proxy records on every trace row, giving an exact trial -> trace join.
    Harbor injects its own `X-Session-ID={trial_name}__agent` after this file and
    would override ours, so it passes ``None`` and the join uses that value.
    """
    model_kwargs: dict[str, Any] = {
        "api_base": api_base,
        "drop_params": True,
        "parallel_tool_calls": True,
    }
    if session_id is not None:
        model_kwargs["extra_headers"] = {"X-Session-ID": session_id}
    return {
        "agent": {"step_limit": int(step_limit), "cost_limit": 0.0},
        "model": {"model_kwargs": model_kwargs},
    }


def agent_env(api_base: str, key_ref: str | None) -> dict[str, str]:
    env = {"MSWEA_COST_TRACKING": "ignore_errors", "HOSTED_VLLM_API_BASE": api_base}
    if key_ref is not None:
        # MSWEA_API_KEY satisfies the adapters' own key check; litellm reads
        # HOSTED_VLLM_API_KEY for the hosted_vllm provider. Both are ${ENV}
        # templates resolved by the harness at trial construction, so the
        # persisted job config keeps the template, not the value.
        env["MSWEA_API_KEY"] = key_ref
        env["HOSTED_VLLM_API_KEY"] = key_ref
    return env


def pier_job_config(
    *,
    job_name: str,
    jobs_dir: Path,
    tasks_root: Path,
    task_names: list[str],
    alias: str,
    api_base: str,
    key_ref: str | None,
    step_limit: int,
    mini_version: str,
    session_id: str,
    template: dict[str, Any] | None,
    timeout_multiplier: float = 1.0,
) -> dict[str, Any]:
    config = json.loads(json.dumps(template)) if template else {}
    # The template owns environment/verifier/artifact knobs and any extra agent
    # field a reviewer adds; this function owns the fields that define an arm.
    agents = config.get("agents") or []
    agent: dict[str, Any] = json.loads(json.dumps(agents[0])) if agents else {}
    agent["name"] = "mini-swe-agent"
    agent["model_name"] = f"hosted_vllm/{alias}"
    kwargs = dict(agent.get("kwargs") or {})
    kwargs.update(
        {
            "version": mini_version,
            "cost_limit": "0",
            "config_yaml": yaml.safe_dump(
                mini_swe_config(step_limit, api_base, session_id), sort_keys=False
            ),
        }
    )
    agent["kwargs"] = kwargs
    env = dict(agent.get("env") or {})
    env.update(agent_env(api_base, key_ref))
    agent["env"] = env
    config.update(
        {
            "job_name": job_name,
            "jobs_dir": str(jobs_dir),
            "n_attempts": 1,
            "n_concurrent_trials": 1,
            "timeout_multiplier": float(timeout_multiplier),
            "retry": {"max_retries": 0},
            "datasets": [{"path": str(tasks_root), "task_names": list(task_names)}],
            "agents": [agent],
        }
    )
    return config


def harbor_job_config(
    *,
    job_name: str,
    jobs_dir: Path,
    tasks_root: Path,
    task_names: list[str],
    alias: str,
    api_base: str,
    key_ref: str | None,
    step_limit: int,
    mini_version: str,
    timeout_multiplier: float = 1.0,
) -> dict[str, Any]:
    host = urllib.parse.urlsplit(api_base).hostname
    env = agent_env(api_base, key_ref)
    # Harbor's mini-swe adapter resolves its endpoint from OPENAI_BASE_URL /
    # OPENAI_API_BASE (ModelConnectionSpec, passthrough), so export both.
    env["OPENAI_BASE_URL"] = api_base
    env["OPENAI_API_BASE"] = api_base
    return {
        "job_name": job_name,
        "jobs_dir": str(jobs_dir),
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "timeout_multiplier": float(timeout_multiplier),
        "retry": {"max_retries": 0},
        "datasets": [{"path": str(tasks_root), "task_names": list(task_names)}],
        "agents": [
            {
                "name": "mini-swe-agent",
                "model_name": f"hosted_vllm/{alias}",
                # Per-run agent-phase egress: Harbor merges these hostnames into
                # the agent allowlist for agent.run() only.
                "extra_allowed_hosts": [host] if host else [],
                "kwargs": {
                    "version": mini_version,
                    "cost_limit": "0",
                    # Harbor takes a mapping (`config`), Pier takes YAML text
                    # (`config_yaml`); same document either way.
                    "config": mini_swe_config(step_limit, api_base, None),
                },
                "env": env,
            }
        ],
    }


# --------------------------------------------------------------------------
# trial collection
# --------------------------------------------------------------------------


def read_trial(trial_dir: Path) -> dict[str, Any]:
    result_path = trial_dir / "result.json"
    row: dict[str, Any] = {
        "trial_name": trial_dir.name,
        "trial_dir": str(trial_dir),
        "result_json": str(result_path) if result_path.exists() else None,
    }
    if not result_path.exists():
        row.update({"status": "incomplete", "detail": "no result.json"})
        return row
    doc = json.loads(result_path.read_text())
    task_id = doc.get("task_id") or {}
    task_path = task_id.get("path")
    task = Path(task_path).name if task_path else str(doc.get("task_name", "")).split("/")[-1]
    rewards = (doc.get("verifier_result") or {}).get("rewards")
    reward_file = trial_dir / "verifier" / "reward.json"
    if rewards is None and reward_file.exists():
        try:
            rewards = json.loads(reward_file.read_text())
        except json.JSONDecodeError:
            rewards = None
    exception = doc.get("exception_info") or {}
    agent_result = doc.get("agent_result") or {}
    model_name = ((doc.get("config") or {}).get("agent") or {}).get("model_name") or ""
    row.update(
        {
            "task": task,
            "trial_id": doc.get("id"),
            "task_checksum": doc.get("task_checksum"),
            "harness_model_name": model_name,
            "condition_from_harness": model_name.split("hosted_vllm/", 1)[-1] if model_name else None,
            "rewards": rewards,
            "exception_type": exception.get("exception_type"),
            "exception_message": redact(str(exception.get("exception_message", "")))[:500] or None,
            "n_agent_steps": doc.get("n_agent_steps") or agent_result.get("n_agent_steps"),
            "agent_usage": {
                "n_input_tokens": agent_result.get("n_input_tokens"),
                "n_output_tokens": agent_result.get("n_output_tokens"),
                "cost_usd": agent_result.get("cost_usd"),
            },
            "started_at": doc.get("started_at"),
            "finished_at": doc.get("finished_at"),
            "agent_execution": doc.get("agent_execution"),
            "artifacts": {
                name: str(path)
                for name, path in {
                    "model.patch": trial_dir / "artifacts" / "model.patch",
                    "ctrf.json": trial_dir / "verifier" / "ctrf.json",
                    "reward.json": reward_file,
                    "trajectory.json": trial_dir / "agent" / "trajectory.json",
                }.items()
                if path.exists()
            },
            "status": "scored" if rewards else ("errored" if exception else "incomplete"),
        }
    )
    return row


def collect_job_trials(job_dir: Path) -> list[dict[str, Any]]:
    if not job_dir.is_dir():
        return []
    return [read_trial(p) for p in sorted(job_dir.iterdir()) if p.is_dir()]


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def resolve_manifest_path(value: str, suite: str | None) -> Path:
    if value == "latest":
        if not suite:
            raise SystemExit(
                "--frozen latest requires --suite: runs/frozen holds one manifest per suite and "
                "'latest' is a lexical glob, so terminal-bench-2-* wins over deepswe-* whatever "
                "the timestamps say - and with it the wrong runner. Pass --suite, or better, the "
                "explicit manifest path that `freeze` printed."
            )
        # Within one suite the filename stamp is UTC, so lexical order is time order.
        frozen = sorted((REPO / "runs/frozen").glob(f"{suite}-*.json"))
        if not frozen:
            raise SystemExit(f"no frozen manifest for suite {suite!r} under runs/frozen")
        return frozen[-1]
    path = Path(value)
    if not path.is_absolute():
        path = REPO / path
    if not path.exists():
        raise SystemExit(f"frozen manifest not found: {path}")
    return path


def proxy_advertised_models(url: str, key: str | None) -> tuple[list[str] | None, str | None]:
    request = urllib.request.Request(url.rstrip("/") + "/models")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        return None, f"{type(exc).__name__}: {redact(str(exc))}"
    ids = [entry.get("id") for entry in payload.get("data", []) if isinstance(entry, dict)]
    return [i for i in ids if i], None


def trace_marker(trace_path: Path) -> dict[str, Any]:
    if not trace_path.exists():
        return {"path": str(trace_path), "exists": False}
    data = trace_path.read_bytes()
    return {
        "path": str(trace_path),
        "exists": True,
        "bytes": len(data),
        "lines": data.count(b"\n"),
        "sha256": sha256_bytes(data),
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def read_guard_events(event_log: Path, breach_log: Path) -> dict[str, Any]:
    """What the exec cancellation guard did during one job.

    Containment is only meaningful if it is observable: a cancelled agent exec
    records one `exec_contained` event per exec, a refused exec records
    `guard_unsupported`, and a sweep that could not prove the container clean
    records `containment_breach` in its own file.
    """
    events = read_jsonl(event_log)
    contained = [e for e in events if e.get("event") == "exec_contained"]
    return {
        "installed": any(e.get("event") == "guard_installed" for e in events),
        "ready": any(e.get("event") == "guard_ready" for e in events),
        "contained_execs": len(contained),
        "containment_elapsed_sec": [e.get("elapsed_sec") for e in contained],
        "unsupported": [
            e.get("detail") for e in events if e.get("event") == "guard_unsupported"
        ],
        "breaches": read_jsonl(breach_log),
    }


def cmd_run(args: argparse.Namespace) -> int:
    manifest_path = resolve_manifest_path(args.frozen, args.suite)
    manifest = json.loads(manifest_path.read_text())
    suite = manifest["suite"]
    runner = args.runner or manifest["runner"]
    if runner not in RUNNER_EXECUTABLE:
        raise SystemExit(f"unknown runner {runner!r}")

    failures: list[str] = []
    deviations: list[str] = []

    current_sources = source_digest()
    frozen_proxy = manifest["sources"]["proxy_sha256"]
    if current_sources["proxy_sha256"] != frozen_proxy:
        message = (
            f"proxy sources changed since freeze ({frozen_proxy[:12]} -> "
            f"{current_sources['proxy_sha256'][:12]})"
        )
        if not args.allow_source_drift:
            print(f"error: {message}; re-freeze or pass --allow-source-drift", file=sys.stderr)
            return 1
        deviations.append(message)

    roster_path = REPO / manifest["roster"]["path"]
    current_roster = sha256_file(roster_path)
    if current_roster != manifest["roster"]["sha256"]:
        message = f"roster {roster_path} changed since freeze"
        if not args.allow_source_drift:
            print(f"error: {message}; re-freeze or pass --allow-source-drift", file=sys.stderr)
            return 1
        deviations.append(message)

    # The roster and the proxy are not the only inputs that shape a trial: this
    # orchestrator generates the job config, the native bootstrap decides what
    # a cancelled agent exec leaves running, the suite template supplies every
    # knob the generator does not overwrite (verifier.disable, environment.delete,
    # any extra agent field), and the predeclaration fixes the plan being run.
    frozen_files = manifest["sources"].get("files") or {}
    guarded_paths = ["scripts/benchmark.py", NATIVE_HARNESS_REL]
    suite_template = SUITES.get(suite, {}).get("template")
    if suite_template:
        guarded_paths.append(suite_template)
    guarded: list[tuple[str, str | None, str | None]] = [
        (rel, frozen_files.get(rel), current_sources["files"].get(rel) or sha256_file(REPO / rel))
        for rel in guarded_paths
    ]
    predeclaration_rel = manifest["predeclaration"]["path"]
    guarded.append(
        (
            predeclaration_rel,
            manifest["predeclaration"]["sha256"],
            sha256_file(REPO / predeclaration_rel),
        )
    )
    for rel, frozen_digest, current in guarded:
        if frozen_digest and current == frozen_digest:
            continue
        message = (
            f"{rel} was not hashed at freeze time"
            if not frozen_digest
            else f"{rel} changed since freeze ({frozen_digest[:12]} -> {str(current)[:12]})"
        )
        if not args.allow_source_drift:
            print(f"error: {message}; re-freeze or pass --allow-source-drift", file=sys.stderr)
            return 1
        deviations.append(message)

    tool = tool_info(RUNNER_EXECUTABLE[runner], RUNNER_DIST[runner])
    if not tool.get("found"):
        print(f"error: {runner} CLI not on PATH", file=sys.stderr)
        return 1
    pinned = manifest["pins"].get(runner)
    if pinned and tool.get("version") != pinned:
        message = f"{runner} {tool.get('version')} != pinned {pinned}"
        if not args.allow_version_drift:
            print(f"error: {message}; pass --allow-version-drift to record it instead", file=sys.stderr)
            return 1
        deviations.append(message)

    # The exec cancellation guard is part of a run's identity: its policy
    # decides whether a cancelled agent is still running while the verifier
    # scores the container. A run without a working guard is the defect it
    # exists to fix, so a broken guard stops the run outright - no flag.
    frozen_guard = manifest["harness_settings"].get("native_exec_cancellation_guard") or {}
    native_guard = native_guard_snapshot(runner, tool)
    if native_guard.get("error"):
        print(
            f"error: native exec cancellation guard unavailable: {native_guard['error']}",
            file=sys.stderr,
        )
        return 1
    if not frozen_guard.get("policy_digest"):
        message = "the frozen manifest carries no exec cancellation guard policy"
        if not args.allow_source_drift:
            print(f"error: {message}; re-freeze or pass --allow-source-drift", file=sys.stderr)
            return 1
        deviations.append(message)
    for label, frozen_value, current_value, flag in (
        (
            "exec guard policy",
            frozen_guard.get("policy_digest"),
            native_guard.get("policy_digest"),
            args.allow_source_drift,
        ),
        (
            "exec guard patch target",
            frozen_guard.get("target_digest"),
            native_guard.get("target_digest"),
            args.allow_version_drift,
        ),
    ):
        if not frozen_value or frozen_value == current_value:
            continue
        message = (
            f"{label} changed since freeze ({str(frozen_value)[:12]} -> "
            f"{str(current_value)[:12]})"
        )
        if not flag:
            hint = (
                "re-freeze or pass --allow-source-drift"
                if label.endswith("policy")
                else "pass --allow-version-drift to record it instead"
            )
            print(f"error: {message}; {hint}", file=sys.stderr)
            return 1
        deviations.append(message)

    native_python_path = native_guard.get("venv_python")
    if not native_python_path:
        print(
            f"error: no venv interpreter next to {tool.get('resolved_path')}; the "
            "guard cannot be installed into the harness process",
            file=sys.stderr,
        )
        return 1

    planned = manifest["conformance_plan" if args.conformance else "plan"]
    requested = [
        item
        for item in planned
        if (not args.conditions or item["condition"] in args.conditions)
        and (not args.tasks or item["task"] in args.tasks)
        and (not args.repeat or item["repeat"] in args.repeat)
    ]
    if args.limit:
        requested = requested[: args.limit]
    if not requested:
        print("error: filters selected no planned trial", file=sys.stderr)
        return 1

    api_base = args.proxy_url or manifest["harness_settings"]["proxy_url_in_sandbox"]
    if args.no_proxy_auth:
        # Not a secret: the proxy is running without bearer auth, so this value
        # only satisfies litellm's requirement that some key be present.
        key_ref: str | None = "unauthenticated-local-proxy"
        key_value: str | None = None
        auth_note = "disabled (--no-proxy-auth)"
    else:
        key_value = os.environ.get(args.proxy_key_env)
        if not key_value:
            print(
                f"error: ${args.proxy_key_env} is unset. Export the local proxy bearer token, "
                "or pass --no-proxy-auth if the proxy runs unauthenticated.",
                file=sys.stderr,
            )
            return 1
        key_ref = "${%s}" % args.proxy_key_env
        auth_note = f"bearer via ${{{args.proxy_key_env}}} template, resolved by the harness at runtime"

    conditions_requested = _first_seen([item["condition"] for item in requested])
    advertised, preflight_error = (None, "skipped")
    if not args.skip_preflight:
        advertised, preflight_error = proxy_advertised_models(args.preflight_url, key_value)
        if advertised is None:
            print(
                f"error: proxy preflight failed at {args.preflight_url}: {preflight_error}",
                file=sys.stderr,
            )
            return 1
        missing = [c for c in conditions_requested if c not in advertised]
        if missing:
            print(
                f"error: proxy does not advertise {missing}; advertised={advertised}",
                file=sys.stderr,
            )
            return 1

    job_root = Path(args.job_root) if args.job_root else REPO / manifest["job_root_default"]
    if not job_root.is_absolute():
        job_root = REPO / job_root
    config_dir = job_root / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)

    template = None
    if runner == "pier":
        template_rel = SUITES[suite].get("template")
        if template_rel and (REPO / template_rel).exists():
            template = yaml.safe_load((REPO / template_rel).read_text())

    trace_path = Path(args.trace or manifest["roster"].get("trace_path") or "runs/reasonproxy.jsonl")
    if not trace_path.is_absolute():
        trace_path = REPO / trace_path

    tasks_root = Path(manifest["dataset"]["tasks_root"])
    mini_version = manifest["tools"]["mini_swe_agent_in_sandbox"]["version"]
    step_limit = manifest["harness_settings"]["agent_step_limit"]
    timeout_multiplier = float(manifest["harness_settings"].get("timeout_multiplier", 1.0))

    # One job per planned trial. Trials are serial on this host anyway
    # (n_concurrent_trials 1), and a 1:1 job/trial mapping is what makes the
    # X-Session-ID header, the sanitized command, the trace window and the
    # native reward all belong to exactly one arm of exactly one task.
    stamp = f"{now_stamp()}-{uuid4().hex[:8]}"
    jobs: list[dict[str, Any]] = []
    trials: list[dict[str, Any]] = []
    executed_keys: set[str] = set()

    for item in requested:
        condition, task, repeat = item["condition"], item["task"], item["repeat"]
        slug = condition.replace("/", "-")
        suffix = "conformance" if args.conformance else f"r{repeat}"
        job_name = f"{suite}__{slug}__{task}__{suffix}__{stamp}"
        session_id = f"{job_name}__agent"
        if runner == "pier":
            config = pier_job_config(
                job_name=job_name,
                jobs_dir=job_root,
                tasks_root=tasks_root,
                task_names=[task],
                alias=condition,
                api_base=api_base,
                key_ref=key_ref,
                step_limit=step_limit,
                mini_version=mini_version,
                timeout_multiplier=timeout_multiplier,
                session_id=session_id,
                template=template,
            )
            injected_session_id: str | None = session_id
        else:
            config = harbor_job_config(
                job_name=job_name,
                jobs_dir=job_root,
                tasks_root=tasks_root,
                task_names=[task],
                alias=condition,
                api_base=api_base,
                key_ref=key_ref,
                step_limit=step_limit,
                mini_version=mini_version,
                timeout_multiplier=timeout_multiplier,
            )
            # Harbor sets agent.session_id = f"{trial_name}__agent" itself and
            # appends the header flag after any custom config, so the expected
            # value is only knowable once the trial directory exists.
            injected_session_id = None
        config_path = config_dir / f"{job_name}.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        # The harness venv interpreter runs the bootstrap, which installs the
        # exec cancellation guard and then hands the *original* CLI script the
        # same arguments it would have received directly.
        argv = [
            native_python_path,
            str(REPO / NATIVE_HARNESS_REL),
            runner,
            tool["path"],
            "run",
            "-c",
            str(config_path),
            "-y",
        ]
        guard_event_log = job_root / job_name / "native-harness-events.jsonl"
        guard_breach_log = job_root / job_name / "native-harness-breaches.jsonl"

        record: dict[str, Any] = {
            "job_name": job_name,
            "condition": condition,
            "task": task,
            "repeat": repeat,
            "trial_key": item["trial_key"],
            "config_path": str(config_path),
            "config_sha256": sha256_file(config_path),
            "command": [redact(part) for part in argv],
            "job_dir": str(job_root / job_name),
            "injected_session_id": injected_session_id,
            "exec_guard": {
                "policy_digest": native_guard.get("policy_digest"),
                "target_digest": native_guard.get("target_digest"),
                "event_log": str(guard_event_log),
                "breach_log": str(guard_breach_log),
            },
            "trace_before": trace_marker(trace_path),
        }
        if args.dry_run:
            record.update({"exit_code": None, "dry_run": True})
            jobs.append(record)
            continue

        print(f"\n=== {job_name} ({runner}) ===", flush=True)
        guard_event_log.parent.mkdir(parents=True, exist_ok=True)
        record["started_at"] = utc_now()
        completed = subprocess.run(
            argv,
            cwd=str(REPO),
            env={
                **os.environ,
                "NATIVE_HARNESS_EVENT_LOG": str(guard_event_log),
                "NATIVE_HARNESS_BREACH_LOG": str(guard_breach_log),
            },
        )
        record["finished_at"] = utc_now()
        record["exit_code"] = completed.returncode
        record["trace_after"] = trace_marker(trace_path)
        if completed.returncode != 0:
            failures.append(f"{job_name}: {runner} exited {completed.returncode}")

        # A breach means a cancelled agent exec could not be proven dead, so
        # anything the verifier saw afterwards is not trustworthy. The guard
        # already refuses further execs on that container; surface it here too.
        guard_summary = read_guard_events(guard_event_log, guard_breach_log)
        record["exec_guard"].update(guard_summary)
        for breach in guard_summary["breaches"]:
            failures.append(
                f"{job_name}: exec containment breach ({breach.get('detail')})"
            )

        job_trials = [t for t in collect_job_trials(job_root / job_name) if t.get("task") in (task, None)]
        record["trials_found"] = len(job_trials)
        if not job_trials:
            trials.append({**item, "job_name": job_name, "status": "missing",
                           "detail": "no trial directory produced"})
            jobs.append(record)
            continue
        for trial in job_trials:
            harness_condition = trial.get("condition_from_harness")
            mismatch = bool(harness_condition and harness_condition != condition)
            trials.append(
                {
                    **item,
                    **trial,
                    "job_name": job_name,
                    "condition_mismatch": mismatch,
                    "session_id": injected_session_id
                    if injected_session_id is not None
                    else f"{trial['trial_name']}__agent",
                }
            )
            if mismatch:
                failures.append(
                    f"{job_name}/{trial['trial_name']}: harness recorded condition "
                    f"{harness_condition!r} but the job requested {condition!r}"
                )
        executed_keys.add(item["trial_key"])
        jobs.append(record)

    missing_keys = [] if args.dry_run else [
        item["trial_key"] for item in requested if item["trial_key"] not in executed_keys
    ]
    if missing_keys:
        failures.append(f"{len(missing_keys)} requested trial(s) produced no result")

    execution = {
        "schema": "reasonproxy.execution/1",
        "written_utc": utc_now(),
        "suite": suite,
        "runner": runner,
        "runner_version": tool.get("version"),
        # How the native CLI was actually launched, and under which guard.
        "native_launch": {
            "venv_python": native_python_path,
            "bootstrap": NATIVE_HARNESS_REL,
            "bootstrap_sha256": native_guard.get("bootstrap_sha256"),
            "policy_version": native_guard.get("policy_version"),
            "policy_digest": native_guard.get("policy_digest"),
            "target_digest": native_guard.get("target_digest"),
            "cancellation_policy": native_guard.get("cancellation_policy"),
            "patched_runtime": native_guard.get("patched_runtime"),
            "entrypoint": "runpy of the original console script, arguments unchanged",
        },
        "conformance": bool(args.conformance),
        "dry_run": bool(args.dry_run),
        "frozen_manifest": {
            "path": str(manifest_path.relative_to(REPO)) if manifest_path.is_relative_to(REPO) else str(manifest_path),
            "sha256": sha256_file(manifest_path),
        },
        "counts": {
            "planned_in_manifest": len(planned),
            "requested": len(requested),
            "executed": len(executed_keys),
            "missing": len(missing_keys),
        },
        "filters": {
            "conditions": args.conditions or None,
            "tasks": args.tasks or None,
            "repeats": args.repeat or None,
            "limit": args.limit,
        },
        "requested_trial_keys": [item["trial_key"] for item in requested],
        "missing_trial_keys": missing_keys,
        "endpoint": {
            "proxy_url_in_sandbox": api_base,
            "preflight_url": args.preflight_url,
            "auth": auth_note,
            "proxy_key_env": None if args.no_proxy_auth else args.proxy_key_env,
            "advertised_models": advertised,
            "preflight": preflight_error if args.skip_preflight else "ok",
        },
        "harness_settings": manifest["harness_settings"],
        "sources_at_run": current_sources,
        "roster_sha256_at_run": current_roster,
        "trace_path": str(trace_path),
        "host": host_snapshot(),
        "deviations": deviations,
        "jobs": jobs,
        "trials": trials,
        "failures": failures,
        "no_synthesis_note": "Missing results are listed above and are never zero-filled or imputed.",
    }

    out = job_root / f"execution-{'conformance-' if args.conformance else ''}{stamp}.json"
    out.write_text(json.dumps(execution, indent=2) + "\n")

    print()
    print(f"execution record: {out}")
    if args.dry_run:
        print(f"  dry run: {len(requested)} planned trial(s), {len(jobs)} job config(s) written, nothing executed")
    else:
        print(f"  requested {len(requested)}  executed {len(executed_keys)}  missing {len(missing_keys)}")
        contained = sum(job.get("exec_guard", {}).get("contained_execs") or 0 for job in jobs)
        breached = sum(len(job.get("exec_guard", {}).get("breaches") or []) for job in jobs)
        print(
            f"  exec guard {native_guard.get('policy_version')} "
            f"policy {str(native_guard.get('policy_digest'))[:12]}: "
            f"{contained} cancelled exec(s) contained, {breached} breach(es)"
        )
    for failure in failures:
        print(f"  ! {failure}")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def wilson_ci(successes: int, total: int, z: float = 1.959963984540054) -> list[float] | None:
    if total == 0:
        return None
    phat = successes / total
    denom = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denom
    return [round(max(0.0, centre - margin), 6), round(min(1.0, centre + margin), 6)]


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value on discordant pair counts."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def signflip_p(diffs: list[float], seed: int = 0, max_exact: int = 20, iters: int = 20000) -> dict[str, Any]:
    """Exact paired sign-flip permutation test on the sum of differences."""
    nonzero = [d for d in diffs if d != 0]
    n = len(nonzero)
    if n == 0:
        return {"p": 1.0, "method": "degenerate (all paired differences zero)", "n_nonzero": 0}
    observed = abs(sum(nonzero))
    if n <= max_exact:
        hits = 0
        for mask in range(1 << n):
            total = 0.0
            for i, value in enumerate(nonzero):
                total += -value if (mask >> i) & 1 else value
            if abs(total) >= observed - 1e-12:
                hits += 1
        return {"p": hits / (1 << n), "method": f"exact sign-flip over 2^{n}", "n_nonzero": n}
    rng = random.Random(seed)
    hits = 0
    for _ in range(iters):
        total = sum(value if rng.random() < 0.5 else -value for value in nonzero)
        if abs(total) >= observed - 1e-12:
            hits += 1
    return {
        "p": (hits + 1) / (iters + 1),
        "method": f"Monte-Carlo sign-flip, {iters} draws, seed {seed}",
        "n_nonzero": n,
    }


def bootstrap_ci(diffs: list[float], iters: int = 10000, seed: int = 0) -> list[float] | None:
    n = len(diffs)
    if n == 0:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        means.append(sum(diffs[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int(0.025 * iters)]
    hi = means[min(iters - 1, int(0.975 * iters))]
    return [round(lo, 6), round(hi, 6)]


def arm_summary(values: list[float], seed: int = 0, iters: int = 10000) -> dict[str, Any]:
    n = len(values)
    if n == 0:
        return {"n": 0}
    mean = sum(values) / n
    summary: dict[str, Any] = {"n": n, "mean": round(mean, 6)}
    if all(v in (0.0, 1.0) for v in values):
        successes = int(sum(values))
        summary.update({"passes": successes, "wilson95": wilson_ci(successes, n)})
    else:
        # An interval for this arm's mean, so the arm's own values are what gets
        # resampled. Mean-centred deviations would centre the interval on zero
        # and read, beside `mean`, as if the arm's own mean covered zero.
        summary["bootstrap95"] = bootstrap_ci(values, iters=iters, seed=seed)
    return summary


# --------------------------------------------------------------------------
# analyze
# --------------------------------------------------------------------------


def classify(
    row: dict[str, Any], adjudication: dict[str, Any], quota_detail: str | None = None
) -> dict[str, Any]:
    """Adjudicate one trial into a class, a graded reward and an operational reward.

    Four ideas are kept apart on purpose:

    * ``graded``      the official verifier ran; its reward stands as reported.
                      A graded 0 is a failure of the condition even when the
                      proxy degraded internally.
    * ``operational_failure``  the trial died in a way the condition owns:
                      agent timeout, nonzero agent exit (which is how a
                      malformed or leaked private tool call and format-error
                      exhaustion arrive), or a provider/proxy transport error
                      that a K-way fan-out can itself provoke, rate limiting
                      included. Zero credit in the operational metric; never
                      quietly dropped.
    * ``provider_quota_exhausted``  the account's free-tier quota was spent, so
                      the declared configuration never ran. Blocked and
                      unscored: excluded from both metrics and reported as a
                      coverage gap with its evidence. Decided by ``quota_detail``
                      before any reward is read, so the class can never depend
                      on the outcome it would otherwise replace.
    * ``infrastructure_excluded``  host, container or verifier-side failure,
                      attributable to no condition. Excluded from both metrics.
    """
    operational = set(adjudication.get("operational_failure_counted_as_reward_0", {}))
    infra = set(adjudication.get("infrastructure_excluded", []))
    status = row.get("status")
    rewards = row.get("rewards") or {}
    exception = row.get("exception_type")

    if row.get("exec_containment_breach"):
        verdict = {
            "class": "infrastructure_excluded",
            "reward": None,
            "operational": None,
            "detail": "exec containment breach",
        }
        if isinstance(rewards, dict) and "reward" in rewards:
            verdict["observed_reward_not_scored"] = _as_float(rewards["reward"])
        return verdict

    if status == "missing":
        return {
            "class": "missing",
            "reward": None,
            "operational": None,
            "detail": row.get("detail", "not executed"),
        }
    if quota_detail:
        verdict: dict[str, Any] = {
            "class": "provider_quota_exhausted",
            "reward": None,
            "operational": None,
            "detail": quota_detail,
        }
        if isinstance(rewards, dict) and "reward" in rewards:
            # Recorded, never scored: the cell is blocked, and hiding a value
            # the verifier did produce would be the opposite of provenance.
            verdict["observed_reward_not_scored"] = _as_float(rewards["reward"])
        return verdict
    if rewards and "reward" in rewards:
        reward = float(rewards["reward"])
        return {
            "class": "graded",
            "reward": reward,
            "operational": reward,
            "f2p": _as_float(rewards.get("f2p")),
            "p2p": _as_float(rewards.get("p2p")),
            "partial": _as_float(rewards.get("partial")),
            "detail": None,
        }
    if exception in operational:
        return {"class": "operational_failure", "reward": None, "operational": 0.0, "detail": exception}
    if exception in infra:
        return {"class": "infrastructure_excluded", "reward": None, "operational": None, "detail": exception}
    if exception:
        return {"class": "unclassified", "reward": None, "operational": None, "detail": exception}
    return {
        "class": "unclassified",
        "reward": None,
        "operational": None,
        "detail": f"status={status}, no verifier reward",
    }


def quota_evidence(
    row: dict[str, Any], records: list[dict[str, Any]], adjudication: dict[str, Any]
) -> str | None:
    """Account-level provider quota exhaustion for one trial, or None.

    Two sources, both named in the frozen predeclaration. The harness exception
    type, when the harness has one of its own. Otherwise the provider's error
    object as the proxy recorded it: a free-tier quota rejection arrives as the
    same HTTP 429 an ordinary rate limit uses, so the native harness collapses
    both into one generic error and only the provider's own error text separates
    them.

    Deliberately narrow. A plain rate limit, a 5xx, an overload, a stall or a
    protocol failure is an outcome of the condition and stays charged to it.
    """
    spec = adjudication.get("provider_quota_exhausted") or {}
    exception = row.get("exception_type")
    if exception and exception in (spec.get("harness_exceptions") or {}):
        return f"harness {exception}"
    markers = [m.lower() for m in spec.get("provider_error_markers") or []]
    if not markers:
        return None
    for where, text in _error_texts(row, records):
        lowered = text.lower()
        if any(marker in lowered for marker in markers):
            return f"{where}: {text[:200]}"
    return None


def _error_texts(row: dict[str, Any], records: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Every place a provider error string can appear for one trial."""
    texts: list[tuple[str, str]] = []
    message = row.get("exception_message")
    if isinstance(message, str) and message:
        texts.append(("harness message", message))
    for record in records:
        detail = record.get("detail")
        if isinstance(detail, str) and detail:
            texts.append((f"trace {record.get('stage')}", detail))
        for branch in record.get("branches") or []:
            error = branch.get("error") if isinstance(branch, dict) else None
            if isinstance(error, str) and error:
                texts.append((f"trace branch {branch.get('backend')}", error))
        for degradation in record.get("degradations") or []:
            reason = degradation.get("reason") if isinstance(degradation, dict) else None
            if isinstance(reason, str) and reason:
                texts.append(("trace degradation", reason))
    return texts


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def load_traces(trace_path: Path) -> list[dict[str, Any]]:
    if not trace_path.exists():
        return []
    records = []
    for line in trace_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def trace_records_for_trial(
    row: dict[str, Any],
    condition: str,
    by_session: dict[str, list[dict[str, Any]]],
    traces: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Trace records belonging to one trial.

    Exact route: the proxy records the caller's X-Session-ID, which is unique
    per trial (Pier gets it from the generated mini-swe config, Harbor sets
    ``{trial_name}__agent`` itself). Fallback route, used when no record carries
    that session id: same virtual model inside the trial's agent-execution
    window. The fallback can over-collect if trials overlap, which is why the
    method is reported per trial.
    """
    session = row.get("session_id")
    if isinstance(session, str) and session in by_session:
        return by_session[session], "session_id"
    window = row.get("agent_execution") or {}
    start = parse_iso(window.get("started_at"))
    end = parse_iso(window.get("finished_at"))
    if not start or not end:
        return [], "unjoinable"
    lo, hi = start.timestamp(), end.timestamp()
    return (
        [
            record
            for record in traces
            if record.get("virtual_model") == condition
            and isinstance(record.get("ts"), (int, float))
            and lo <= record["ts"] <= hi
        ],
        "time_window",
    )


def trace_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {"records": 0}
    outcomes: dict[str, int] = {}
    stages: dict[str, int] = {}
    reason_calls = 0
    with_reason = 0
    reduced = 0
    degraded = 0
    violations = 0
    input_tokens = 0
    output_tokens = 0
    unreported = 0
    for record in records:
        outcomes[str(record.get("outcome"))] = outcomes.get(str(record.get("outcome")), 0) + 1
        if record.get("outcome") == "error":
            key = f"{record.get('stage')}: {str(record.get('detail'))[:80]}"
            stages[key] = stages.get(key, 0) + 1
        calls = record.get("reason_calls") or 0
        reason_calls += calls
        with_reason += 1 if calls else 0
        for checkpoint in record.get("checkpoints") or []:
            if checkpoint.get("reduced"):
                reduced += 1
        if record.get("degraded"):
            degraded += 1
        violations += len(record.get("protocol_violations") or [])
        for usage in record.get("usage") or []:
            if usage.get("input_tokens") is None and usage.get("output_tokens") is None:
                unreported += 1
            input_tokens += usage.get("input_tokens") or 0
            output_tokens += usage.get("output_tokens") or 0
    return {
        "records": len(records),
        "outcomes": outcomes,
        "error_stages": stages,
        "reason_calls_total": reason_calls,
        "records_with_reason_call": with_reason,
        "validated_checkpoints": reduced,
        "degraded_records": degraded,
        "protocol_violations": violations,
        "usage": {
            "reported_input_tokens": input_tokens,
            "reported_output_tokens": output_tokens,
            "component_rows_without_reported_usage": unreported,
        },
        "caveat": (
            "reason_calls counts deliberation cycles entered, not validated checkpoints; "
            "cross-read with validated_checkpoints and degraded_records. Token sums include "
            "only provider-reported values."
        ),
    }


def cmd_analyze(args: argparse.Namespace) -> int:
    manifest_path = resolve_manifest_path(args.frozen, args.suite)
    manifest = json.loads(manifest_path.read_text())
    suite = manifest["suite"]
    adjudication = manifest.get("adjudication")
    declared_contrasts = manifest.get("contrasts")
    if not adjudication or not declared_contrasts:
        print(
            "error: this manifest was frozen before adjudication and contrasts were recorded "
            "in it. Re-freeze the suite and analyze the new manifest: an analysis adjudicates "
            "from its manifest only and never reads a live predeclaration from disk.",
            file=sys.stderr,
        )
        return 1

    if args.execution:
        execution_paths = [Path(p) if Path(p).is_absolute() else REPO / p for p in args.execution]
    else:
        job_root = Path(args.job_root) if args.job_root else REPO / manifest["job_root_default"]
        if not job_root.is_absolute():
            job_root = REPO / job_root
        execution_paths = sorted(job_root.glob("execution-*.json"))
    if not execution_paths:
        print("error: no execution record found; run first", file=sys.stderr)
        return 1

    executions = [json.loads(p.read_text()) for p in execution_paths]
    # A dry run writes an execution record with requested_trial_keys populated
    # and no trials, so counting it would inflate every coverage denominator.
    dry_run_records = [
        str(path) for path, record in zip(execution_paths, executions) if record.get("dry_run")
    ]
    live = [
        (path, record)
        for path, record in zip(execution_paths, executions)
        if not record.get("dry_run")
    ]
    if not live:
        print(
            f"error: all {len(execution_paths)} selected execution record(s) are dry runs; "
            "nothing was executed",
            file=sys.stderr,
        )
        return 1
    execution_paths = [path for path, _ in live]
    executions = [record for _, record in live]

    pilot_rows: list[dict[str, Any]] = []
    conformance_rows: list[dict[str, Any]] = []
    requested_keys: set[str] = set()
    key_sources: dict[str, list[str]] = {}
    for path, execution in live:
        if execution.get("conformance"):
            conformance_rows.extend(execution.get("trials") or [])
            continue
        breached_jobs = {
            job["job_name"]
            for job in execution.get("jobs") or []
            if (job.get("exec_guard") or {}).get("breaches")
        }
        for row in execution.get("trials") or []:
            if row.get("job_name") in breached_jobs:
                row = {**row, "exec_containment_breach": True}
            pilot_rows.append(row)
            key_sources.setdefault(str(row.get("trial_key")), []).append(str(path))
        requested_keys.update(execution.get("requested_trial_keys") or [])

    duplicates = {key: sources for key, sources in key_sources.items() if len(sources) > 1}
    if duplicates:
        # Averaging a repeat is the outcome-dependent re-run channel the
        # predeclaration prohibits: a losing trial could be run again and
        # averaged up with nothing in the artifacts to show it.
        print(
            f"error: {len(duplicates)} trial key(s) occur more than once in the selected "
            "execution records. Name the authoritative records explicitly with "
            "--execution <path...> instead; an accidental re-run is never averaged in.",
            file=sys.stderr,
        )
        for key, sources in sorted(duplicates.items()):
            print(f"  {key}: {', '.join(sources)}", file=sys.stderr)
        return 1

    conditions = manifest["conditions"]
    tasks = [t["id"] for t in manifest["tasks"]]

    trace_path = Path(args.trace) if args.trace else None
    if trace_path is None:
        candidate = executions[0].get("trace_path") if executions else None
        trace_path = Path(candidate) if candidate else REPO / "runs/reasonproxy.jsonl"
    if not trace_path.is_absolute():
        trace_path = REPO / trace_path
    traces = load_traces(trace_path)

    by_session: dict[str, list[dict[str, Any]]] = {}
    for record in traces:
        session = record.get("session_id")
        if isinstance(session, str) and session:
            by_session.setdefault(session, []).append(record)

    # Joined before adjudication, not after: a free-tier quota rejection reaches
    # the harness as the same 429 an ordinary rate limit does, so the provider's
    # own error text on the trace row is what separates them. The per-condition
    # trace statistics below reuse the same join.
    joined: list[tuple[list[dict[str, Any]], str]] = []
    join_methods: dict[str, int] = {}
    for row in pilot_rows:
        records, method = trace_records_for_trial(
            row, str(row.get("condition") or ""), by_session, traces
        )
        joined.append((records, method))
        join_methods[method] = join_methods.get(method, 0) + 1

    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    non_scoring: list[dict[str, Any]] = []
    for index, row in enumerate(pilot_rows):
        verdict = classify(row, adjudication, quota_evidence(row, joined[index][0], adjudication))
        entry = {
            "repeat": row.get("repeat"),
            "trial_key": row.get("trial_key"),
            "trial_name": row.get("trial_name"),
            "trial_dir": row.get("trial_dir"),
            "n_agent_steps": row.get("n_agent_steps"),
            "artifacts": row.get("artifacts"),
            "agent_execution": row.get("agent_execution"),
            "session_id": row.get("session_id"),
            **verdict,
        }
        cells.setdefault((row.get("task", ""), row.get("condition", "")), []).append(entry)
        if verdict["class"] != "graded":
            non_scoring.append({"task": row.get("task"), "condition": row.get("condition"), **verdict})

    # `operational` uses graded rewards plus zero credit for failures the
    # condition owns; `reward`/`f2p`/`p2p` are graded-only and descriptive.
    OPERATIONAL_CLASSES = ("graded", "operational_failure")

    def cell_value(task: str, condition: str, metric: str = "operational") -> dict[str, Any]:
        entries = cells.get((task, condition), [])
        usable_classes = OPERATIONAL_CLASSES if metric == "operational" else ("graded",)
        usable = [e for e in entries if e["class"] in usable_classes]
        values = [e.get(metric) for e in usable if isinstance(e.get(metric), (int, float))]
        if not entries:
            return {"status": "missing", "value": None, "repeats": 0, "detail": "not executed"}
        if not values:
            return {
                "status": entries[0]["class"],
                "value": None,
                "repeats": 0,
                "detail": "; ".join(sorted({str(e["detail"]) for e in entries if e["detail"]})) or None,
            }
        return {
            "status": "usable",
            "value": sum(values) / len(values),
            "repeats": len(values),
            "excluded_repeats": len(entries) - len(values),
            "classes": sorted({e["class"] for e in usable}),
        }

    table: list[dict[str, Any]] = []
    for task in tasks:
        row = {"task": task, "language": next((t.get("language") for t in manifest["tasks"] if t["id"] == task), None)}
        for condition in conditions:
            row[condition] = {
                "operational": cell_value(task, condition, "operational"),
                "graded_reward": cell_value(task, condition, "reward"),
                "f2p": cell_value(task, condition, "f2p"),
                "p2p": cell_value(task, condition, "p2p"),
            }
        table.append(row)

    planned_per_condition: dict[str, int] = {}
    for item in manifest["plan"]:
        planned_per_condition[item["condition"]] = planned_per_condition.get(item["condition"], 0) + 1
    requested_per_condition: dict[str, int] = {}
    for key in requested_keys:
        parts = key.split("/")
        if len(parts) >= 3:
            condition = "/".join(parts[1:3])
            requested_per_condition[condition] = requested_per_condition.get(condition, 0) + 1

    condition_summaries: dict[str, Any] = {}
    for condition in conditions:
        trial_classes: dict[str, int] = {}
        failure_detail: dict[str, int] = {}
        for (task, cond), entries in cells.items():
            if cond != condition:
                continue
            for entry in entries:
                trial_classes[entry["class"]] = trial_classes.get(entry["class"], 0) + 1
                if entry["class"] in (
                    "operational_failure",
                    "provider_quota_exhausted",
                    "infrastructure_excluded",
                    "unclassified",
                ):
                    key = f"{entry['class']}:{entry['detail']}"
                    failure_detail[key] = failure_detail.get(key, 0) + 1
        operational_values = [
            cell_value(task, condition, "operational")["value"]
            for task in tasks
            if cell_value(task, condition, "operational")["status"] == "usable"
        ]
        graded_values = [
            cell_value(task, condition, "reward")["value"]
            for task in tasks
            if cell_value(task, condition, "reward")["status"] == "usable"
        ]
        condition_summaries[condition] = {
            "coverage": {
                "tasks_planned": planned_per_condition.get(condition, 0),
                "trials_requested": requested_per_condition.get(condition, 0),
                "trial_rows": sum(trial_classes.values()),
                "by_class": trial_classes,
                "failure_detail": failure_detail,
            },
            "operational": {
                "primary": True,
                **arm_summary(
                    [v for v in operational_values if v is not None],
                    seed=args.seed,
                    iters=args.bootstrap,
                ),
                "tasks_with_value": len(operational_values),
            },
            "graded_only": {
                "primary": False,
                "descriptive": "accuracy conditional on reaching the verifier; denominators differ between arms",
                **arm_summary(
                    [v for v in graded_values if v is not None],
                    seed=args.seed,
                    iters=args.bootstrap,
                ),
                "tasks_with_value": len(graded_values),
            },
        }

    families = _first_seen([c.split("/", 1)[1] for c in conditions])
    contrasts: list[dict[str, Any]] = []
    for family in families:
        for declared in declared_contrasts:
            treatment = f"{declared['treatment_mode']}/{family}"
            reference = f"{declared['reference_mode']}/{family}"
            if treatment not in conditions or reference not in conditions:
                continue
            for metric in ("operational", "reward", "f2p"):
                paired: list[dict[str, Any]] = []
                unpaired: list[dict[str, Any]] = []
                for task in tasks:
                    left = cell_value(task, treatment, metric)
                    right = cell_value(task, reference, metric)
                    if left["status"] == "usable" and right["status"] == "usable":
                        paired.append(
                            {"task": task, "treatment": left["value"], "reference": right["value"]}
                        )
                    else:
                        unpaired.append(
                            {"task": task, "treatment": left["status"], "reference": right["status"]}
                        )
                entry: dict[str, Any] = {
                    "id": f"{declared['id']}@{family}",
                    "metric": metric,
                    "primary": bool(declared.get("primary")) and metric == "operational",
                    "descriptive_only": metric != "operational",
                    "treatment": treatment,
                    "reference": reference,
                    "n_paired_tasks": len(paired),
                    "tasks_excluded_from_pairing": unpaired,
                }
                if paired:
                    diffs = [p["treatment"] - p["reference"] for p in paired]
                    entry.update(
                        {
                            "treatment_summary": arm_summary(
                                [p["treatment"] for p in paired], seed=args.seed, iters=args.bootstrap
                            ),
                            "reference_summary": arm_summary(
                                [p["reference"] for p in paired], seed=args.seed, iters=args.bootstrap
                            ),
                            "mean_paired_difference": round(sum(diffs) / len(diffs), 6),
                            "permutation": signflip_p(diffs, seed=args.seed),
                            "bootstrap95_paired_difference": bootstrap_ci(diffs, iters=args.bootstrap, seed=args.seed),
                            "per_task": paired,
                        }
                    )
                    if all(v in (0.0, 1.0) for p in paired for v in (p["treatment"], p["reference"])):
                        b = sum(1 for p in paired if p["treatment"] == 1 and p["reference"] == 0)
                        c = sum(1 for p in paired if p["treatment"] == 0 and p["reference"] == 1)
                        entry["mcnemar_exact"] = {
                            "treatment_only": b,
                            "reference_only": c,
                            "discordant": b + c,
                            "p_two_sided": round(mcnemar_exact(b, c), 6),
                        }
                        if b + c == 0:
                            entry["mcnemar_note"] = "no discordant task pairs: the contrast is uninformative"
                    if all(p["reference"] == 0.0 for p in paired) and all(p["treatment"] == 0.0 for p in paired):
                        entry["floor_note"] = "both arms scored zero on every paired task"
                contrasts.append(entry)

    per_condition_traces: dict[str, Any] = {}
    for condition in conditions:
        matched: list[dict[str, Any]] = []
        for index, row in enumerate(pilot_rows):
            if row.get("condition") == condition:
                matched.extend(joined[index][0])
        per_condition_traces[condition] = trace_stats(matched)

    # A cell is never zero-filled, so an unrun, blocked or unclassified cell has
    # to be named here or the coverage denominators would be its only trace.
    coverage_gaps: list[dict[str, Any]] = []
    for task in tasks:
        for condition in conditions:
            cell = cell_value(task, condition, "operational")
            if cell["status"] != "usable":
                coverage_gaps.append(
                    {
                        "task": task,
                        "condition": condition,
                        "status": cell["status"],
                        "detail": cell.get("detail"),
                    }
                )
    conditions_with_rows = {condition for _task, condition in cells}
    conditions_with_no_trial_row = [c for c in conditions if c not in conditions_with_rows]

    analysis = {
        "schema": "reasonproxy.analysis/1",
        "written_utc": utc_now(),
        "suite": suite,
        "primary_metric": manifest["primary_metric"],
        "frozen_manifest": str(manifest_path.relative_to(REPO)) if manifest_path.is_relative_to(REPO) else str(manifest_path),
        "execution_records": [str(p) for p in execution_paths],
        "dry_run_records_skipped": dry_run_records,
        "counts": {
            "tasks_planned": len(tasks),
            "conditions_planned": len(conditions),
            "trials_planned_in_manifest": manifest["counts"]["planned_trials"],
            "trials_requested": len(requested_keys),
            "trial_rows_recorded": len(pilot_rows),
            "cells_planned": len(tasks) * len(conditions),
            "cells_with_operational_value": sum(
                1
                for task in tasks
                for condition in conditions
                if cell_value(task, condition, "operational")["status"] == "usable"
            ),
            "cells_graded": sum(
                1
                for task in tasks
                for condition in conditions
                if cell_value(task, condition, "reward")["status"] == "usable"
            ),
            "cells_without_operational_value": len(coverage_gaps),
            "cells_provider_quota_blocked": sum(
                1 for gap in coverage_gaps if gap["status"] == "provider_quota_exhausted"
            ),
        },
        "per_task_table": table,
        "condition_summaries": condition_summaries,
        "contrasts": contrasts,
        "non_scoring_outcomes": non_scoring,
        "coverage_gaps": coverage_gaps,
        "conditions_with_no_trial_row": conditions_with_no_trial_row,
        "conformance_rows": [
            {
                "task": row.get("task"),
                "condition": row.get("condition"),
                "status": row.get("status"),
                "rewards": row.get("rewards"),
                "exception_type": row.get("exception_type"),
            }
            for row in conformance_rows
        ],
        "trace": {
            "path": str(trace_path),
            "records_total": len(traces),
            "records_with_session_id": sum(len(v) for v in by_session.values()),
            "per_condition": per_condition_traces,
            "join": (
                "X-Session-ID equality when the proxy recorded one, otherwise "
                "virtual_model equality inside the trial's agent-execution window"
            ),
            "join_methods": join_methods,
        },
        "execution_failures": [f for execution in executions for f in execution.get("failures") or []],
        "deviations": [d for execution in executions for d in execution.get("deviations") or []],
        "interpretation": {
            "unit_of_analysis": "task",
            "primary_metric_definition": (
                "operational: graded binary reward with operational failures (agent timeout, "
                "nonzero agent exit including private-tool protocol exhaustion, provider/proxy "
                "transport errors) counted as 0"
            ),
            "graded_only_metric": (
                "descriptive accuracy over trials that reached the verifier; denominators differ "
                "between arms, so it is not an effect estimate"
            ),
            "zero_fill": "never; missing trials are missing, not zeros, and are listed with coverage counts",
            "provider_quota": (
                "account-level free-tier quota exhaustion is blocked and unscored, listed with "
                "its evidence, and reported as a provider-availability limit rather than a "
                "failure of the arm; transient rate limits stay charged to the condition"
            ),
            "failure_attribution": (
                "Protocol, quorum and timeout failures stay charged to the condition that produced "
                "them; only host/container/verifier-side failures are excluded, and those are listed"
            ),
            "conditional_subsets": (
                "No contrast is computed on an outcome-conditioned or reason-was-called subset; such "
                "subsets are descriptive only and never a causal effect"
            ),
            "power": "This pilot is underpowered by construction; treat every p-value as descriptive.",
            "frontier_baseline": "not run: paid frontier trials are authorization-blocked and are not substituted with public leaderboard values",
            "causal_claims": "none beyond the executed paired contrast under this exact pinned configuration",
            "external_reference": "Published DeepSWE/Terminal-Bench leaderboard values may be cited only as an external snapshot, never in a statistical statement with these numbers",
        },
    }

    out = Path(args.out) if args.out else execution_paths[-1].parent / f"analysis-{now_stamp()}.json"
    if not out.is_absolute():
        out = REPO / out
    out.write_text(json.dumps(analysis, indent=2) + "\n")

    print_analysis(analysis, tasks, conditions)
    print(f"\nanalysis written: {out}")

    if analysis["counts"]["cells_with_operational_value"] == 0:
        print("error: no usable outcome; nothing to analyze", file=sys.stderr)
        return 1
    if args.strict:
        blocking = [
            f"{item['class']}: {item['condition']} / {item['task']} ({item['detail']})"
            for item in non_scoring
            if item["class"] in ("missing", "unclassified", "provider_quota_exhausted")
        ]
        blocking += [
            f"no primary value: {gap['condition']} / {gap['task']} ({gap['status']})"
            for gap in coverage_gaps
        ]
        blocking += [
            f"requested condition produced no trial row: {condition}"
            for condition in conditions_with_no_trial_row
        ]
        blocking += analysis["execution_failures"]
        if blocking:
            print(f"error: --strict and {len(blocking)} incomplete item(s):", file=sys.stderr)
            for item in blocking:
                print(f"  {item}", file=sys.stderr)
            return 1
    return 0


def print_analysis(analysis: dict[str, Any], tasks: list[str], conditions: list[str]) -> None:
    counts = analysis["counts"]
    print(f"\nsuite {analysis['suite']}  primary metric: {analysis['primary_metric']}")
    print(
        f"planned {counts['trials_planned_in_manifest']} trials, requested {counts['trials_requested']}, "
        f"recorded {counts['trial_rows_recorded']}; cells with an operational value "
        f"{counts['cells_with_operational_value']}/{counts['cells_planned']}, graded {counts['cells_graded']}"
    )

    short = {
        "missing": "missing",
        "infrastructure_excluded": "infra-excl",
        "operational_failure": "op-fail",
        "unclassified": "unclassified",
        "provider_quota_exhausted": "quota-blk",
    }
    width = max((len(t) for t in tasks), default=4) + 2
    column = max(14, max((len(c) for c in conditions), default=14) + 2)
    header = "task".ljust(width) + "".join(c.ljust(column) for c in conditions)
    print("\nprimary: operational binary reward per task (g=graded, o=operational failure charged 0)")
    print(header)
    print("-" * len(header))
    for row in analysis["per_task_table"]:
        line = row["task"].ljust(width)
        for condition in conditions:
            cell = row[condition]["operational"]
            if cell["status"] == "usable":
                graded = row[condition]["graded_reward"]["status"] == "usable"
                text = f"{cell['value']:.2f}{'g' if graded else 'o'}"
                if cell.get("repeats", 1) > 1:
                    text += f" (n={cell['repeats']})"
            else:
                text = short.get(cell["status"], cell["status"])
            line += text.ljust(column)
        print(line)

    print("\ncondition summaries (task as unit)")
    for condition, summary in analysis["condition_summaries"].items():
        coverage = summary["coverage"]
        operational = summary["operational"]
        graded = summary["graded_only"]
        if operational.get("n"):
            extra = f" wilson95={operational['wilson95']}" if operational.get("wilson95") else ""
            print(
                f"  {condition:<22} operational mean={operational['mean']:.3f} over "
                f"{operational['n']}/{coverage['tasks_planned']} task(s){extra}"
            )
        else:
            print(f"  {condition:<22} operational: no usable task")
        if graded.get("n"):
            print(
                f"  {'':<22} graded-only  mean={graded['mean']:.3f} over {graded['n']} graded task(s) "
                "[descriptive; different denominator]"
            )
        print(f"  {'':<22} coverage {coverage['by_class'] or '{}'}")
        for key, count in sorted(coverage["failure_detail"].items()):
            print(f"  {'':<22}   {key} x{count}")

    print("\ncontrasts")
    for contrast in analysis["contrasts"]:
        tag = "PRIMARY" if contrast["primary"] else "descriptive"
        if not contrast["n_paired_tasks"]:
            print(f"  [{tag}] {contrast['id']} ({contrast['metric']}): no paired task")
            continue
        line = (
            f"  [{tag}] {contrast['id']} ({contrast['metric']}): "
            f"delta={contrast['mean_paired_difference']:+.3f} on {contrast['n_paired_tasks']} task(s), "
            f"perm p={contrast['permutation']['p']:.4f}"
        )
        if contrast.get("mcnemar_exact"):
            m = contrast["mcnemar_exact"]
            line += f", McNemar b={m['treatment_only']} c={m['reference_only']} p={m['p_two_sided']:.4f}"
        print(line)
        for note in ("mcnemar_note", "floor_note"):
            if contrast.get(note):
                print(f"      note: {contrast[note]}")

    if analysis["non_scoring_outcomes"]:
        print("\nnon-graded outcomes (charged to the condition unless infrastructure or quota; never zero-filled)")
        charges = {
            "operational_failure": "counted 0",
            "provider_quota_exhausted": "blocked, unscored",
        }
        for item in analysis["non_scoring_outcomes"]:
            charge = charges.get(item["class"], "excluded")
            print(
                f"  {str(item['condition']):<22} {str(item['task']):<44} "
                f"{item['class']} ({charge}): {item['detail']}"
            )

    if analysis["coverage_gaps"]:
        print(
            f"\ncoverage gaps: {len(analysis['coverage_gaps'])} planned cell(s) carry no primary "
            "value and are never zero-filled"
        )
        for gap in analysis["coverage_gaps"]:
            print(f"  {str(gap['condition']):<22} {str(gap['task']):<44} {gap['status']}")
        for condition in analysis["conditions_with_no_trial_row"]:
            print(f"  ! requested condition produced no trial row at all: {condition}")

    print("\nproxy trace, per condition")
    for condition, stats in analysis["trace"]["per_condition"].items():
        if not stats.get("records"):
            print(f"  {condition:<22} no trace record joined to any trial")
            continue
        print(
            f"  {condition:<22} records={stats['records']} reason_calls={stats['reason_calls_total']} "
            f"validated_checkpoints={stats['validated_checkpoints']} degraded={stats['degraded_records']} "
            f"violations={stats['protocol_violations']} outcomes={stats['outcomes']}"
        )
    print(f"  join methods: {analysis['trace']['join_methods']}")

    for failure in analysis["execution_failures"]:
        print(f"\n! execution failure: {failure}")
    for deviation in analysis["deviations"]:
        print(f"\n! deviation: {deviation}")
    print(f"\nfrontier baseline: {analysis['interpretation']['frontier_baseline']}")
    print(f"conditional subsets: {analysis['interpretation']['conditional_subsets']}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmark.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    freeze = sub.add_parser("freeze", help="resolve a predeclaration into an immutable manifest")
    freeze.add_argument("--suite", default="deepswe", choices=sorted(SUITES))
    freeze.add_argument("--roster", default="configs/research.yaml")
    freeze.add_argument("--predeclaration", default=None)
    freeze.add_argument("--proxy-url", default="http://host.docker.internal:8100/v1")
    freeze.add_argument("--step-limit", type=int, default=250)
    freeze.add_argument("--out")
    freeze.set_defaults(func=cmd_freeze)

    run = sub.add_parser("run", help="execute planned trials via the official CLI")
    run.add_argument("--frozen", required=True, help="path to a frozen manifest, or 'latest' with --suite")
    run.add_argument(
        "--suite",
        default=None,
        choices=sorted(SUITES),
        help="resolves --frozen latest, and is required with it",
    )
    run.add_argument("--runner", default=None, choices=sorted(RUNNER_EXECUTABLE))
    run.add_argument("--conditions", nargs="*", default=[])
    run.add_argument("--tasks", nargs="*", default=[])
    run.add_argument("--repeat", nargs="*", type=int, default=[])
    run.add_argument("--limit", type=int, default=None, help="execute the first N planned trials in frozen order")
    run.add_argument("--conformance", action="store_true", help="run the reserved conformance task(s) instead")
    run.add_argument("--job-root", default=None)
    run.add_argument("--proxy-url", default=None, help="endpoint as seen from inside the sandbox")
    run.add_argument("--preflight-url", default="http://127.0.0.1:8100/v1", help="endpoint as seen from the host")
    run.add_argument("--proxy-key-env", default="REASONPROXY_API_KEY")
    run.add_argument("--no-proxy-auth", action="store_true")
    run.add_argument("--skip-preflight", action="store_true")
    run.add_argument("--trace", default=None)
    run.add_argument("--allow-source-drift", action="store_true")
    run.add_argument("--allow-version-drift", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=cmd_run)

    analyze = sub.add_parser("analyze", help="adjudicate native rewards and compute paired statistics")
    analyze.add_argument("--frozen", required=True)
    analyze.add_argument("--suite", default=None, choices=sorted(SUITES))
    analyze.add_argument("--execution", nargs="*", default=[])
    analyze.add_argument("--job-root", default=None)
    analyze.add_argument("--trace", default=None)
    analyze.add_argument("--bootstrap", type=int, default=10000)
    analyze.add_argument("--seed", type=int, default=0)
    analyze.add_argument(
        "--strict",
        action="store_true",
        help="exit nonzero on any planned cell without a primary value: missing, unclassified or "
        "provider-quota-blocked, plus any requested condition with no trial row",
    )
    analyze.add_argument("--out")
    analyze.set_defaults(func=cmd_analyze)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
