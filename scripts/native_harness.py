#!/usr/bin/env python3
"""Run an *unmodified* native harness CLI with one recorded runtime patch.

Why this file exists
--------------------
Both native harnesses execute the agent inside the task container with

    docker compose exec <flags> main bash -c '<agent command>'

and both bound the agent phase on the host with ``asyncio.wait_for(...)``.
When that bound fires, the only thing either harness terminates is the *host*
``docker compose exec`` client (harbor: ``DockerEnvironment._terminate_process``,
pier: ``process.terminate()`` in ``_run_docker_compose_command``). Killing the
client does not kill the process the daemon started inside the container: the
exec'd ``bash`` and everything it forked keeps running, keeps calling the model
endpoint and keeps mutating the task workspace while the harness moves on to
the verifier phase. That is the defect this bootstrap closes; upstream still
has it, so upgrading is not a fix.

What it does
------------
``install("harbor")`` / ``install("pier")`` monkeypatch exactly one method,
``DockerEnvironment._run_docker_compose_command``, plus the stdlib
``asyncio.create_subprocess_exec`` entry point those methods use, so that a
main-container Linux exec is *contained*: on any abnormal exit from the exec
(cancellation, harness timeout, host error) the in-container command and its
active descendants are terminated and reaped **before** the exception
propagates - i.e. before the verifier phase can observe the container - and the
host docker client plus its host-side descendants are reaped too.

Nothing else is touched: no agent logic, no prompts, no scorer, no timeouts, no
model wiring. The normal-completion path runs the original code with the
original arguments and returns the original ``ExecResult``.

Usage
-----
    python scripts/native_harness.py harbor /abs/path/to/harbor run -c cfg.yaml -y
    python scripts/native_harness.py pier   /abs/path/to/pier   run -c cfg.yaml -y
    python scripts/native_harness.py --describe harbor

The interpreter must be the harness's own venv python (the ``python`` sibling of
the CLI executable). If it is not, and the target package cannot be imported,
this file re-execs itself once with that interpreter rather than guessing.

After the guard is installed the *original* console script is executed with
``runpy`` and byte-identical arguments, so ``harbor run ...`` behaves exactly as
it would have, guard aside.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import importlib
import importlib.metadata
import importlib.util
import inspect
import json
import os
import runpy
import signal
import subprocess
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "install",
    "policy",
    "policy_digest",
    "describe",
    "main",
    "ExecGuardError",
    "ExecGuardUnsupportedError",
    "ExecContainmentBreachError",
    "GUARD_ENV_VAR",
    "POLICY_VERSION",
]

# --------------------------------------------------------------------------
# policy constants
#
# These are the guard's entire behavioural surface. They are hashed into
# ``policy_digest()`` and frozen by scripts/benchmark.py, so changing any of
# them changes a run's identity instead of silently altering containment.
# --------------------------------------------------------------------------

POLICY_VERSION = "native-exec-cancellation-guard/2"

#: Scoped identifier injected as a container environment variable. It is the
#: primary ownership signal: a Linux child inherits its parent's environment
#: across fork/exec, so every descendant of the exec'd command carries it -
#: including descendants that were re-parented to PID 1 by double-forking.
GUARD_ENV_VAR = "NATIVE_EXEC_GUARD_ID"

#: Seconds the owned in-container tree is given to exit after SIGTERM before
#: SIGKILL. Short on purpose: the harness is already tearing the phase down.
TERM_GRACE_SEC = 2

#: Extra kill+verify rounds (0.25s apart) after the first sweep. These exist
#: for the cancellation-before-PID-publication race: a process the daemon was
#: still starting when we swept appears a moment later and is caught here.
ABORT_LATE_ROUNDS = 4
POST_CLIENT_LATE_ROUNDS = 2

#: Hard ceilings. Containment blocks the event loop, so it must be bounded.
SWEEP_TIMEOUT_SEC = 20
PROBE_TIMEOUT_SEC = 60
HOST_CLIENT_TERM_GRACE_SEC = 0.25
HOST_PS_TIMEOUT_SEC = 5

#: Compose ``exec`` options that consume the following argv element. Used to
#: locate the service name in an exec argv without guessing.
_VALUE_FLAGS = frozenset({"-w", "--workdir", "-e", "--env", "-u", "--user", "--index"})

#: Options that make containment impossible; a main-container exec carrying one
#: is rejected rather than run unguarded.
_REJECTED_FLAGS = frozenset({"-d", "--detach"})

_ENV_ATTR = "_native_guard_state"
_PROCESS_BREACH: dict[str, Any] = {}

# --------------------------------------------------------------------------
# in-container scripts
#
# Deliberately plain bash + /proc: the main container is a harness-built image
# that is guaranteed to ship bash (both harnesses wrap main-container commands
# in ``bash -c`` for exactly that reason) and Linux /proc. No agent, no python,
# no uploaded helper, no third-party tooling inside the container.
# --------------------------------------------------------------------------

PROBE_SH = r"""
set -u
MARK=${1:?marker}
WANT="NATIVE_EXEC_GUARD_ID=$MARK"

fail() { printf 'PROBE_FAIL %s\n' "$1"; exit 3; }

[ -n "${BASH_VERSION:-}" ] || fail no_bash
[ -d /proc ] || fail no_proc
[ -r /proc/self/environ ] || fail self_environ_unreadable

found=0
while IFS= read -r -d '' kv; do
  if [ "x$kv" = "x$WANT" ]; then found=1; fi
done < /proc/self/environ
[ "$found" = 1 ] || fail marker_not_visible_in_environ

raw=""
read -r raw < /proc/1/stat 2>/dev/null || fail proc1_stat_unreadable
rest=${raw##*") "}
[ "x$rest" != "x$raw" ] || fail proc1_stat_unparsable
set -- $rest
[ $# -ge 3 ] || fail proc1_stat_too_short
case "$1" in [RSDZTtWXxKWPI]) ;; *) fail proc1_state_unexpected ;; esac

# Descendants may run as a different user than the exec that spawned them
# (sudo, su, uid-dropping wrappers). Containment therefore needs to read a
# foreign process's environ; prove that here instead of discovering it during
# a cancellation.
[ -r /proc/1/environ ] || fail foreign_environ_unreadable

n=0
for d in /proc/[0-9]*; do n=$((n + 1)); done
[ "$n" -ge 1 ] || fail proc_enumeration_empty

nap=int
if sleep 0.1 2>/dev/null; then nap=frac; fi

# Optional, not required: without readlink the guard loses only the
# inherited-stdio-pipe signal (marker, ppid chain and process group remain).
fdlink=no
if readlink /proc/self/fd/0 >/dev/null 2>&1; then fdlink=yes; fi

printf 'PROBE_OK bash=%s procs=%s nap=%s fdlink=%s euid=%s\n' \
  "$BASH_VERSION" "$n" "$nap" "$fdlink" "$(id -u 2>/dev/null || printf '?')"
"""

REAPER_SH = r"""
set -u
MARK=${1:?marker}
MODE=${2:?mode}
GRACE=${3:?grace}
ROUNDS=${4:?rounds}

WANT="NATIVE_EXEC_GUARD_ID=$MARK"
SELF=$$

NAP=int
if sleep 0.1 2>/dev/null; then NAP=frac; fi

nap() {
  if [ "$NAP" = frac ]; then sleep "$1"; else sleep 1; fi
}

# Does this pid carry our scoped marker in its initial environment? Processes
# come and go while /proc is being walked, so the redirections are grouped and
# silenced: a pid that vanishes mid-read is simply not owned.
marked() {
  local target=$1 kv
  [ -r "/proc/$target/environ" ] || return 1
  { while IFS= read -r -d '' kv; do
      if [ "x$kv" = "x$WANT" ]; then return 0; fi
    done < "/proc/$target/environ"
  } 2>/dev/null
  return 1
}

S_STATE=""
S_PPID=0
S_PGRP=0
# /proc/<pid>/stat is "pid (comm) state ppid pgrp session ...". comm can hold
# spaces and parens, so split after the last ") " rather than by field index.
statof() {
  local target=$1 raw rest
  { read -r raw < "/proc/$target/stat"; } 2>/dev/null || return 1
  rest=${raw##*") "}
  if [ "x$rest" = "x$raw" ]; then return 1; fi
  set -- $rest
  if [ $# -lt 3 ]; then return 1; fi
  S_STATE=$1
  S_PPID=$2
  S_PGRP=$3
  return 0
}

# A zombie has already terminated: it cannot call a model, write a file, or
# fork. It is also unreapable from here (its parent is PID 1, which in a task
# image is a keepalive command that never waits). Treating zombies as owned
# would signal the dead forever and report a false breach, so they are skipped
# everywhere - which is also why "survivors" below means *live* survivors.
is_zombie() {
  statof "$1" || return 1
  [ "$S_STATE" = Z ]
}

HAVE_READLINK=0
if readlink /proc/self/fd/0 >/dev/null 2>&1; then HAVE_READLINK=1; fi
PIPES=""
NOTPIPE=""

# Fourth ownership signal: the exec's own stdio pipe. containerd creates it for
# this exec alone, and a descendant inherits the open file description across
# fork, setsid and execve - including an execve that replaces the environment.
# Nothing that predates the exec can hold it, so matching on it cannot touch an
# unrelated process.
capture_pipes() {
  local pid fd t
  [ "$HAVE_READLINK" = 1 ] || return 0
  [ -z "$PIPES" ] || return 0
  for pid in $MARKEDSET; do
    for fd in 0 1 2; do
      t=$(readlink "/proc/$pid/fd/$fd" 2>/dev/null) || continue
      case "$t" in
        pipe:*)
          case "$PIPES" in
            *" $t "*) ;;
            *) PIPES="$PIPES $t " ;;
          esac
          ;;
      esac
    done
  done
}

# Negative results are cached: an already-running process never acquires this
# exec's pipe later, so each foreign pid is probed at most once per reaper run.
holds_pipe() {
  local target=$1 fd t
  [ -n "$PIPES" ] || return 1
  case "$NOTPIPE" in *" $target "*) return 1 ;; esac
  for fd in 0 1 2; do
    t=$(readlink "/proc/$target/fd/$fd" 2>/dev/null) || continue
    case "$PIPES" in *" $t "*) return 0 ;; esac
  done
  NOTPIPE="$NOTPIPE $target "
  return 1
}

OWNED=""
MARKEDSET=""

# Ownership is the union of four independent, inherited signals, minus PID 1,
# minus zombies and minus this reaper. Nothing outside that union is ever
# signalled, so pre-existing container services and concurrent execs (which
# carry a different marker and a different pipe) are untouched.
collect() {
  local d pid cur hops roots groups
  MARKEDSET=""
  for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    if [ "$pid" = 1 ] || [ "$pid" = "$SELF" ]; then continue; fi
    if is_zombie "$pid"; then continue; fi
    if marked "$pid"; then MARKEDSET="$MARKEDSET $pid "; fi
  done
  OWNED=$MARKEDSET
  capture_pipes

  # Roots: marked processes whose parent is not marked. A docker exec's root
  # shows ppid 0 inside the container's pid namespace; a re-parented orphan
  # shows ppid 1.
  roots=""
  for pid in $MARKEDSET; do
    if statof "$pid"; then
      case "$MARKEDSET" in
        *" $S_PPID "*) ;;
        *) roots="$roots $pid " ;;
      esac
    fi
  done

  # Only adopt a process group when a root *leads* it: that proves the group
  # was created for this exec and is not shared with a container service.
  groups=""
  for pid in $roots; do
    if statof "$pid"; then
      if [ "$S_PGRP" = "$pid" ] && [ "$S_PGRP" != 1 ]; then
        groups="$groups $S_PGRP "
      fi
    fi
  done

  for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    if [ "$pid" = 1 ] || [ "$pid" = "$SELF" ]; then continue; fi
    case "$OWNED" in *" $pid "*) continue ;; esac
    if ! statof "$pid"; then continue; fi
    if [ "$S_STATE" = Z ]; then continue; fi
    case "$groups" in
      *" $S_PGRP "*) OWNED="$OWNED $pid "; continue ;;
    esac
    # Ancestor chain: catches a descendant that replaced its environment.
    cur=$S_PPID
    hops=0
    while [ "$cur" != 0 ] && [ "$cur" != 1 ] && [ "$hops" -lt 64 ]; do
      case "$MARKEDSET" in *" $cur "*) OWNED="$OWNED $pid "; break ;; esac
      if ! statof "$cur"; then break; fi
      cur=$S_PPID
      hops=$((hops + 1))
    done
    case "$OWNED" in *" $pid "*) continue ;; esac
    # Last signal: still holding this exec's stdio pipe. Catches a descendant
    # that is simultaneously env-scrubbed, setsid'd and re-parented.
    if holds_pipe "$pid"; then OWNED="$OWNED $pid "; fi
  done
}

signal_owned() {
  local sig=$1 pid n=0
  for pid in $OWNED; do
    if kill "-$sig" "$pid" 2>/dev/null; then n=$((n + 1)); fi
  done
  printf 'GUARD_SIGNAL sig=%s count=%s pids=%s\n' "$sig" "$n" "$OWNED"
}

collect
printf 'GUARD_SCAN initial=%s\n' "$OWNED"

if [ -n "$OWNED" ] && [ "$MODE" = term ]; then
  signal_owned TERM
  if [ "$NAP" = frac ]; then ticks=$((GRACE * 10)); else ticks=$GRACE; fi
  i=0
  while [ "$i" -lt "$ticks" ]; do
    collect
    if [ -z "$OWNED" ]; then break; fi
    nap 0.1
    i=$((i + 1))
  done
fi

collect
if [ -n "$OWNED" ]; then signal_owned KILL; fi

r=0
while [ "$r" -lt "$ROUNDS" ]; do
  nap 0.25
  collect
  if [ -n "$OWNED" ]; then signal_owned KILL; fi
  r=$((r + 1))
done

collect
if [ -z "$OWNED" ]; then
  printf 'GUARD_RESULT contained=1 survivors=\n'
else
  printf 'GUARD_RESULT contained=0 survivors=%s\n' "$OWNED"
fi
exit 0
"""


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class ExecGuardError(RuntimeError):
    """Base class for guard failures."""


class ExecGuardUnsupportedError(ExecGuardError):
    """A main-container exec cannot be guarded, so it is refused.

    Raised instead of running the command unguarded: an unguarded main-container
    exec is the exact failure mode this bootstrap exists to prevent, and a
    silent fallback would make a trial look valid when it is not.
    """


class ExecContainmentBreachError(ExecGuardError):
    """Containment could not be proven for a cancelled exec on this environment.

    Latched for the harness process, including fresh verifier environments.
    The benchmark launcher uses one trial per process; a separate verifier
    must never grade artifacts from an uncontained agent.
    """


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(message: str) -> None:
    print(f"[native-harness] {message}", file=sys.stderr, flush=True)


def _append_jsonl(env_var: str, payload: dict[str, Any]) -> None:
    path = os.environ.get(env_var)
    if not path:
        return
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError as exc:  # never let telemetry break a run
        _log(f"could not write {env_var}={path}: {exc}")


def _event(kind: str, **fields: Any) -> dict[str, Any]:
    payload = {"ts": _now(), "event": kind, "pid": os.getpid(), **fields}
    _append_jsonl("NATIVE_HARNESS_EVENT_LOG", payload)
    _log(json.dumps(payload, sort_keys=True))
    return payload


def _breach(**fields: Any) -> None:
    payload = {"ts": _now(), "event": "containment_breach", "pid": os.getpid(), **fields}
    _append_jsonl("NATIVE_HARNESS_BREACH_LOG", payload)
    _append_jsonl("NATIVE_HARNESS_EVENT_LOG", payload)
    _log("CONTAINMENT BREACH " + json.dumps(payload, sort_keys=True))


# --------------------------------------------------------------------------
# package targets
# --------------------------------------------------------------------------


class _Target:
    """A resolved, verified patch target inside one installed harness."""

    def __init__(
        self,
        package: str,
        dist: str,
        module_name: str,
        main_service: str,
        cls: type,
        identified: dict[str, dict[str, Any]],
    ) -> None:
        self.package = package
        self.dist = dist
        self.module_name = module_name
        self.main_service = main_service
        self.cls = cls
        self.identified = identified


_PACKAGES: dict[str, dict[str, Any]] = {
    "harbor": {
        "dist": "harbor",
        "root": "harbor",
        "module": "harbor.environments.docker.docker",
        "identify_modules": (
            "harbor.environments.docker.docker",
            "harbor.environments.docker.docker_unix",
            "harbor.environments.base",
            "harbor.constants",
        ),
        # Methods whose bodies the guard's race analysis depends on. Their
        # source hashes go into the manifest so an upstream change to the
        # cancellation path cannot pass unnoticed.
        "identify_methods": (
            "_run_docker_compose_command",
            "_collect_streamed_output",
            "_collect_buffered_output",
            "_terminate_process",
            "_compose_exec",
            "exec",
            "service_exec",
        ),
    },
    "pier": {
        "dist": "datacurve-pier",
        "root": "pier",
        "module": "pier.environments.docker.docker",
        "identify_modules": (
            "pier.environments.docker.docker",
            "pier.environments.docker.docker_unix",
            "pier.environments.base",
        ),
        "identify_methods": (
            "_run_docker_compose_command",
            "exec",
        ),
    },
}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _resolve_main_service(package: str, cls: type) -> str:
    """The compose service the guard is responsible for, read from the package."""
    if package == "harbor":
        constants = importlib.import_module("harbor.constants")
        name = getattr(constants, "MAIN_SERVICE_NAME", None)
        if not isinstance(name, str) or not name:
            raise ExecGuardUnsupportedError(
                "harbor.constants.MAIN_SERVICE_NAME is missing; cannot tell which "
                "compose service is the main container"
            )
        return name
    # Pier has no constant: its DockerEnvironment.exec appends the literal.
    source = inspect.getsource(cls.exec)
    if 'exec_command.append("main")' not in source:
        raise ExecGuardUnsupportedError(
            "pier DockerEnvironment.exec no longer targets the literal 'main' "
            "service; the guard cannot identify the main container"
        )
    return "main"


def _verify_target(package: str, cls: type, module: Any) -> None:
    """Fail closed unless the exec path still looks like the one we patch."""
    method = getattr(cls, "_run_docker_compose_command", None)
    if method is None or not asyncio.iscoroutinefunction(method):
        raise ExecGuardUnsupportedError(
            f"{package}: DockerEnvironment._run_docker_compose_command is missing "
            "or is not a coroutine function"
        )
    params = list(inspect.signature(method).parameters)
    if params[:2] != ["self", "command"]:
        raise ExecGuardUnsupportedError(
            f"{package}: unexpected _run_docker_compose_command signature "
            f"{params!r}; the guard needs (self, command, ...)"
        )
    exec_method = getattr(cls, "exec", None)
    if exec_method is None or not asyncio.iscoroutinefunction(exec_method):
        raise ExecGuardUnsupportedError(f"{package}: DockerEnvironment.exec is missing")

    module_source = inspect.getsource(module)
    if "_is_windows_container" not in module_source:
        raise ExecGuardUnsupportedError(
            f"{package}: DockerEnvironment no longer exposes _is_windows_container, "
            "so the guard cannot distinguish Linux from Windows containers"
        )
    if "asyncio.create_subprocess_exec" not in module_source:
        raise ExecGuardUnsupportedError(
            f"{package}: DockerEnvironment no longer spawns compose through "
            "asyncio.create_subprocess_exec; the guard cannot observe its argv"
        )


def _identify(package: str, cls: type, spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Exact runtime identification of what is being patched."""
    modules: dict[str, Any] = {}
    for name in spec["identify_modules"]:
        mod = importlib.import_module(name)
        path = Path(getattr(mod, "__file__", "") or "")
        modules[name] = {"file": str(path), "sha256": _sha256_file(path)}
    methods: dict[str, Any] = {}
    for name in spec["identify_methods"]:
        attr = inspect.getattr_static(cls, name, None)
        func = getattr(attr, "__func__", attr)
        if func is None:
            methods[name] = {"present": False}
            continue
        try:
            source = inspect.getsource(func)
        except (OSError, TypeError):
            methods[name] = {"present": True, "source_sha256": None}
            continue
        methods[name] = {
            "present": True,
            "source_sha256": _sha256_text(source),
            "signature": str(inspect.signature(func)),
        }
    try:
        version = importlib.metadata.version(spec["dist"])
    except importlib.metadata.PackageNotFoundError:
        version = None
    return {
        "package": {"name": package, "dist": spec["dist"], "version": version},
        "modules": modules,
        "methods": methods,
        "class": {"qualname": f"{cls.__module__}.{cls.__qualname__}"},
    }


def _resolve_target(package: str) -> _Target:
    spec = _PACKAGES.get(package)
    if spec is None:
        raise ExecGuardUnsupportedError(
            f"unknown package {package!r}; expected one of {sorted(_PACKAGES)}"
        )
    module = importlib.import_module(spec["module"])
    cls = getattr(module, "DockerEnvironment", None)
    if cls is None:
        raise ExecGuardUnsupportedError(
            f"{spec['module']}.DockerEnvironment not found; the guard has nothing to patch"
        )
    _verify_target(package, cls, module)
    main_service = _resolve_main_service(package, cls)
    identified = _identify(package, cls, spec)
    return _Target(
        package=package,
        dist=spec["dist"],
        module_name=spec["module"],
        main_service=main_service,
        cls=cls,
        identified=identified,
    )


# --------------------------------------------------------------------------
# argv analysis
# --------------------------------------------------------------------------


def _service_index(command: list[str]) -> int:
    """Index of the compose service name in a ``compose exec`` argv.

    Raises ``ExecGuardUnsupportedError`` when the argv cannot be read, because
    "cannot read it" and "is not a main-container exec" must not be conflated:
    the first has to be refused, the second is simply out of scope.
    """
    index = 1
    while index < len(command):
        token = str(command[index])
        if not token.startswith("-"):
            return index
        if token in _REJECTED_FLAGS:
            raise ExecGuardUnsupportedError(
                f"compose exec carries {token!r}; a detached exec cannot be contained"
            )
        if token in _VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            index += 1
            continue
        index += 1
    raise ExecGuardUnsupportedError(
        f"cannot locate the service name in compose exec argv {command!r}"
    )


def _inject_marker(command: list[str], service_index: int, marker: str) -> list[str]:
    """Return a copy of *command* with the scoped marker added as ``-e``.

    Inserted immediately before the service name, so every existing flag,
    the service, the shell wrapper and the command string keep their exact
    positions and values. Nothing is removed or rewritten.
    """
    injected = [f"{GUARD_ENV_VAR}={marker}"]
    for token in command:
        if str(token).startswith(f"{GUARD_ENV_VAR}="):
            raise ExecGuardUnsupportedError(
                f"{GUARD_ENV_VAR} is already present in the exec argv; refusing to "
                "nest guard scopes"
            )
    return [
        *(str(token) for token in command[:service_index]),
        "-e",
        *injected,
        *(str(token) for token in command[service_index:]),
    ]


# --------------------------------------------------------------------------
# guard scope
# --------------------------------------------------------------------------


class _GuardScope:
    """One guarded main-container exec."""

    __slots__ = ("env", "marker", "command", "service", "target", "prefix", "proc_env", "process")

    def __init__(self, env: Any, marker: str, command: list[str], service: str, target: _Target):
        self.env = env
        self.marker = marker
        self.command = command
        self.service = service
        self.target = target
        self.prefix: list[str] | None = None
        self.proc_env: dict[str, str] | None = None
        self.process: Any = None

    def bind(self, argv: list[str], proc_env: dict[str, str] | None) -> None:
        """Adopt the *observed* compose invocation as the containment transport.

        The argv the harness actually spawned is authoritative: it carries the
        real project name, project directory, ``-f`` compose files and daemon
        environment. Reusing it verbatim means the reaper talks to exactly the
        same daemon and project as the exec it is containing, with no
        reimplementation of either package's compose wiring.
        """
        tail = len(self.command)
        if len(argv) < tail or argv[len(argv) - tail :] != self.command:
            raise ExecGuardUnsupportedError(
                "the guarded exec argv is not the tail of the spawned command line; "
                "cannot derive a containment transport"
            )
        prefix = argv[: len(argv) - tail]
        # The reaper is built by appending a fresh `exec` to this prefix, so the
        # prefix must be a `docker compose` invocation - not merely something
        # docker-shaped. A different transport is refused rather than guessed at.
        if (
            len(prefix) < 2
            or "docker" not in Path(prefix[0]).name
            or prefix[1] != "compose"
        ):
            raise ExecGuardUnsupportedError(
                f"unexpected exec transport {prefix[:2]!r}; the guard contains "
                "`docker compose exec` invocations only"
            )
        self.prefix = prefix
        self.proc_env = dict(proc_env) if proc_env else dict(os.environ)
        state = _env_state(self.env)
        state["prefix"] = prefix
        state["proc_env"] = self.proc_env

    def transport(self) -> tuple[list[str], dict[str, str]] | None:
        if self.prefix is not None and self.proc_env is not None:
            return self.prefix, self.proc_env
        # Cancellation may land before this exec ever reached the daemon. A
        # previously observed transport on the same environment still lets us
        # sweep for this exec's marker, which is what closes the
        # cancel-before-publication race.
        state = _env_state(self.env)
        prefix = state.get("prefix")
        proc_env = state.get("proc_env")
        if prefix and proc_env is not None:
            return list(prefix), dict(proc_env)
        return None


def _env_state(env: Any) -> dict[str, Any]:
    state = getattr(env, _ENV_ATTR, None)
    if state is None:
        state = {}
        setattr(env, _ENV_ATTR, state)
    return state


_ACTIVE_SCOPE: ContextVar[_GuardScope | None] = ContextVar("native_guard_scope", default=None)


# --------------------------------------------------------------------------
# containment
# --------------------------------------------------------------------------


def _reaper_argv(
    prefix: list[str], service: str, marker: str, mode: str, rounds: int
) -> list[str]:
    return [
        *prefix,
        "exec",
        "-T",
        "-u",
        "0",
        service,
        "bash",
        "-c",
        REAPER_SH,
        "native-exec-guard",
        marker,
        mode,
        str(TERM_GRACE_SEC),
        str(rounds),
    ]


def _parse_result(stdout: str) -> tuple[bool | None, str]:
    contained: bool | None = None
    survivors = ""
    for line in stdout.splitlines():
        if line.startswith("GUARD_RESULT "):
            fields, _, survivors = line.partition(" survivors=")
            survivors = survivors.strip()
            for field in fields.split()[1:]:
                key, _, value = field.partition("=")
                if key == "contained":
                    contained = value == "1"
    return contained, survivors


def _run_reaper(
    prefix: list[str],
    proc_env: dict[str, str],
    service: str,
    marker: str,
    mode: str,
    rounds: int,
) -> dict[str, Any]:
    """One synchronous containment pass.

    Synchronous on purpose. The abort path runs while the calling task is
    already being cancelled, where any ``await`` can be interrupted again and
    ``asyncio.shield`` does not survive a second cancellation. Blocking the
    loop for a bounded window is the only way to guarantee the sweep completes
    before the exception - and therefore the verifier - proceeds.
    """
    argv = _reaper_argv(prefix, service, marker, mode, rounds)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            env=proc_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=SWEEP_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {"mode": mode, "ok": False, "error": "reaper timed out", "elapsed_sec": round(time.monotonic() - started, 3)}
    except OSError as exc:
        return {"mode": mode, "ok": False, "error": f"{type(exc).__name__}: {exc}", "elapsed_sec": round(time.monotonic() - started, 3)}
    contained, survivors = _parse_result(completed.stdout or "")
    return {
        "mode": mode,
        "ok": completed.returncode == 0 and contained is not None,
        "return_code": completed.returncode,
        "contained": contained,
        "survivors": survivors,
        "elapsed_sec": round(time.monotonic() - started, 3),
        "stdout_tail": (completed.stdout or "")[-800:],
        "stderr_tail": (completed.stderr or "")[-400:],
    }


def _host_process_table() -> dict[int, tuple[int, str]]:
    """One snapshot of the host process table: pid -> (ppid, command name)."""
    try:
        completed = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=HOST_PS_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    table: dict[int, tuple[int, str]] = {}
    for line in (completed.stdout or "").splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        try:
            table[int(fields[0])] = (int(fields[1]), fields[2].strip())
        except ValueError:
            continue
    return table


def _host_descendants(table: dict[int, tuple[int, str]], pid: int) -> list[int]:
    """Host-side descendants of *pid* in *table*, deepest first.

    ``docker compose`` is a CLI plugin: the process the harness spawned is
    ``docker``, which forks ``docker-compose``. Signalling only the direct
    child leaves that plugin attached to the daemon, so the whole host subtree
    is enumerated explicitly. The harness's own process group is never used as
    a kill target - the harness lives in it.
    """
    children: dict[int, list[int]] = {}
    for child, (parent, _comm) in table.items():
        children.setdefault(parent, []).append(child)
    ordered: list[int] = []
    seen = {pid}
    frontier = [pid]
    mine = os.getpid()
    while frontier:
        current = frontier.pop()
        for child in children.get(current, ()):
            if child in (mine, 1) or child in seen:
                continue
            seen.add(child)
            ordered.append(child)
            frontier.append(child)
    ordered.reverse()
    return ordered


def _signal_pid(pid: int, sig: int) -> bool:
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _reap_host_client(scope: _GuardScope) -> dict[str, Any]:
    """Reap the host docker client and its host-side descendants.

    The pid is identity-checked against a live process-table snapshot first.
    asyncio's child watcher runs on its own thread and may reap the client
    while this synchronous sweep holds the loop, so a stale ``returncode`` is
    not proof the pid is still ours - and signalling a recycled pid would hit
    an unrelated host process.
    """
    process = scope.process
    if process is None:
        return {"client": None, "note": "no host client was spawned"}
    pid = getattr(process, "pid", None)
    if pid is None:
        return {"client": None, "note": "host client has no pid"}
    table = _host_process_table()
    if not table:
        return {"client": pid, "note": "host process table unavailable; not signalling"}
    entry = table.get(pid)
    if entry is None:
        return {"client": pid, "note": "host client already exited"}
    comm = entry[1]
    if "docker" not in Path(comm).name.lower():
        return {
            "client": pid,
            "note": f"pid no longer holds the docker client (comm={comm!r}); not signalling",
        }
    descendants = _host_descendants(table, pid)
    termed = [p for p in (*descendants, pid) if _signal_pid(p, signal.SIGTERM)]
    time.sleep(HOST_CLIENT_TERM_GRACE_SEC)
    # Re-verify identity, then escalate. If the client has already exited its
    # remaining descendants are re-parented and no longer attributable, but the
    # in-container tree they were attached to is already dead by this point.
    fresh = _host_process_table()
    entry_now = fresh.get(pid)
    still_ours: list[int] = []
    if entry_now and "docker" in Path(entry_now[1]).name.lower():
        still_ours = [*_host_descendants(fresh, pid), pid]
    killed = [p for p in still_ours if _signal_pid(p, signal.SIGKILL)]
    return {
        "client": pid,
        "comm": comm,
        "descendants": descendants,
        "termed": termed,
        "killed": killed,
    }


def _contain(scope: _GuardScope, exc: BaseException) -> None:
    """Terminate and reap the owned in-container tree, then the host client.

    Runs on every abnormal exit from a guarded exec, before the exception is
    re-raised, so nothing the exec started is still alive when the harness
    moves on to the verifier. Never raises: the caller must re-raise the
    original exception with its identity intact.
    """
    started = time.monotonic()
    passes: list[dict[str, Any]] = []
    client: dict[str, Any] = {}
    contained: bool | None = None
    failure: str | None = None
    try:
        transport = scope.transport()
        if transport is None:
            # No compose invocation was ever observed for this environment, so
            # the daemon never received this exec: there is nothing in the
            # container to contain.
            contained = True
            failure = None
        else:
            prefix, proc_env = transport
            # 1. In-container first: kill the owned tree while the client is
            #    still attached, so no output is lost and no window exists in
            #    which the client is gone but the agent is still running.
            passes.append(
                _run_reaper(
                    prefix, proc_env, scope.service, scope.marker, "term", ABORT_LATE_ROUNDS
                )
            )
            # 2. Then the host docker client and its host-side descendants.
            client = _reap_host_client(scope)
            # 3. Verify after the client is gone. Anything the daemon had
            #    started but not yet reported now exists and is killed here.
            passes.append(
                _run_reaper(
                    prefix,
                    proc_env,
                    scope.service,
                    scope.marker,
                    "kill",
                    POST_CLIENT_LATE_ROUNDS,
                )
            )
            final = passes[-1]
            contained = bool(final.get("ok")) and final.get("contained") is True
            if not contained:
                failure = final.get("error") or f"survivors={final.get('survivors')!r}"
    except BaseException as guard_exc:  # noqa: BLE001 - must not mask the original
        contained = False
        failure = f"{type(guard_exc).__name__}: {guard_exc}"

    record = {
        "marker": scope.marker,
        "service": scope.service,
        "package": scope.target.package,
        "abort": f"{type(exc).__name__}: {exc}"[:300],
        "contained": contained,
        "passes": passes,
        "host_client": client,
        "elapsed_sec": round(time.monotonic() - started, 3),
    }
    if contained:
        _event("exec_contained", **record)
        return
    _PROCESS_BREACH.update(marker=scope.marker, detail=failure)
    _breach(detail=failure, **record)


# --------------------------------------------------------------------------
# establishment probe
# --------------------------------------------------------------------------


_REAL_CREATE_SUBPROCESS_EXEC: Callable[..., Any] = asyncio.create_subprocess_exec


async def _probe(scope: _GuardScope) -> dict[str, Any]:
    """Prove containment is possible in *this* container before running.

    Verifies end to end that a ``-e`` injected marker is visible in
    ``/proc/<pid>/environ``, that a foreign process's environ is readable, and
    that /proc can be enumerated and parsed - i.e. every fact the reaper needs.
    """
    prefix, proc_env = scope.prefix or [], scope.proc_env or {}
    marker = f"probe-{uuid.uuid4().hex}"
    argv = [
        *prefix,
        "exec",
        "-T",
        "-u",
        "0",
        "-e",
        f"{GUARD_ENV_VAR}={marker}",
        scope.service,
        "bash",
        "-c",
        PROBE_SH,
        "native-exec-guard-probe",
        marker,
    ]
    process = await _REAL_CREATE_SUBPROCESS_EXEC(
        *argv,
        env=proc_env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=PROBE_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise ExecGuardUnsupportedError(
            f"containment probe timed out after {PROBE_TIMEOUT_SEC}s in service "
            f"{scope.service!r}"
        )
    text = (stdout or b"").decode(errors="replace")
    if process.returncode != 0 or "PROBE_OK" not in text:
        raise ExecGuardUnsupportedError(
            "containment probe failed for service "
            f"{scope.service!r} (exit {process.returncode}): {text.strip()[-400:]!r}. "
            "Refusing to run a main-container exec that cannot be contained."
        )
    detail = next((line for line in text.splitlines() if line.startswith("PROBE_OK")), "")
    return {"probe": detail, "at": _now()}


async def _ensure_ready(scope: _GuardScope) -> None:
    """Probe once per environment, before that environment's first guarded exec.

    A success is cached; a failure is not. Failing to probe is reported and the
    exec is refused either way, but a container that was simply not up yet
    must not be latched off for the rest of the trial - the next exec probes
    again and either succeeds or is refused for the same reason.
    """
    state = _env_state(scope.env)
    if state.get("probe"):
        return
    try:
        result = await _probe(scope)
    except ExecGuardUnsupportedError as exc:
        _event("guard_unsupported", package=scope.target.package, detail=str(exc))
        raise
    state["probe"] = result
    _event(
        "guard_ready",
        package=scope.target.package,
        service=scope.service,
        detail=result["probe"],
    )


# --------------------------------------------------------------------------
# patches
# --------------------------------------------------------------------------


async def _guarded_create_subprocess_exec(program: Any, *args: Any, **kwargs: Any) -> Any:
    """Observe the compose invocation of a guarded exec; passthrough otherwise."""
    scope = _ACTIVE_SCOPE.get()
    if scope is None or scope.process is not None:
        return await _REAL_CREATE_SUBPROCESS_EXEC(program, *args, **kwargs)
    scope.bind([str(program), *(str(a) for a in args)], kwargs.get("env"))
    await _ensure_ready(scope)
    process = await _REAL_CREATE_SUBPROCESS_EXEC(program, *args, **kwargs)
    scope.process = process
    return process


def _install_subprocess_interception() -> None:
    if getattr(asyncio.create_subprocess_exec, "_native_guard", False):
        return
    _guarded_create_subprocess_exec._native_guard = True  # type: ignore[attr-defined]
    asyncio.create_subprocess_exec = _guarded_create_subprocess_exec  # type: ignore[assignment]
    asyncio.subprocess.create_subprocess_exec = _guarded_create_subprocess_exec  # type: ignore[assignment]


def _patch_compose_runner(target: _Target) -> None:
    cls = target.cls
    original = inspect.getattr_static(cls, "_run_docker_compose_command")
    original_func = getattr(original, "__func__", original)
    if getattr(original_func, "_native_guard", False):
        return

    @functools.wraps(original_func)
    async def guarded(self: Any, command: Any, *args: Any, **kwargs: Any) -> Any:
        tokens = [str(token) for token in command]
        if not tokens or tokens[0] != "exec":
            return await original_func(self, command, *args, **kwargs)
        service_index = _service_index(tokens)
        if tokens[service_index] != target.main_service:
            # Sidecar service exec (egress control, third-party images). Out of
            # this guard's scope: it owns the main container's agent lifecycle.
            return await original_func(self, command, *args, **kwargs)
        if getattr(self, "_is_windows_container", False):
            raise ExecGuardUnsupportedError(
                "the native exec cancellation guard implements Linux /proc "
                "containment only, and this task declares a Windows container; "
                "refusing to run an unguarded main-container exec"
            )
        breach = _PROCESS_BREACH
        if breach:
            raise ExecContainmentBreachError(
                "a previous cancelled exec in this harness process could not be "
                f"contained ({breach.get('detail')}); refusing further "
                "main-container execs, including verification"
            )

        marker = uuid.uuid4().hex
        scope = _GuardScope(
            env=self,
            marker=marker,
            command=_inject_marker(tokens, service_index, marker),
            service=target.main_service,
            target=target,
        )
        token = _ACTIVE_SCOPE.set(scope)
        try:
            return await original_func(self, scope.command, *args, **kwargs)
        except BaseException as exc:
            _ACTIVE_SCOPE.reset(token)
            token = None
            _contain(scope, exc)
            raise
        finally:
            if token is not None:
                _ACTIVE_SCOPE.reset(token)

    guarded._native_guard = True  # type: ignore[attr-defined]
    cls._run_docker_compose_command = guarded  # type: ignore[assignment]


_INSTALLED: dict[str, dict[str, Any]] = {}


def install(package: str) -> dict[str, Any]:
    """Install the Docker exec cancellation guard into *package* in this process.

    ``package`` is ``"harbor"`` or ``"pier"``. Returns the identification record
    of what was patched. Raises ``ExecGuardUnsupportedError`` if the installed
    harness no longer matches the exec path this guard understands - it never
    silently no-ops.
    """
    if package in _INSTALLED:
        return _INSTALLED[package]
    target = _resolve_target(package)
    _install_subprocess_interception()
    _patch_compose_runner(target)
    record = {
        "policy": policy(),
        "runtime": {
            **target.identified,
            "python": {"executable": sys.executable, "version": sys.version.split()[0]},
            "main_service": target.main_service,
        },
    }
    record["runtime"]["target_digest"] = _digest(
        {"modules": target.identified["modules"], "methods": target.identified["methods"]}
    )
    _INSTALLED[package] = record
    _event(
        "guard_installed",
        package=package,
        dist=target.identified["package"],
        policy_version=POLICY_VERSION,
        policy_digest=record["policy"]["digest"],
        target_digest=record["runtime"]["target_digest"],
    )
    return record


# --------------------------------------------------------------------------
# policy description
# --------------------------------------------------------------------------


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def policy() -> dict[str, Any]:
    """The guard's effective, hashable cancellation policy."""
    body = {
        "version": POLICY_VERSION,
        "summary": (
            "On any abnormal exit (asyncio cancellation, harness exec timeout, "
            "host error) from a Linux main-container `docker compose exec`, the "
            "in-container command and its active descendants are SIGTERMed, then "
            "SIGKILLed and verified gone, before the exception propagates and the "
            "verifier phase starts; the host docker client and its host-side "
            "descendants are reaped afterwards, then containment is re-verified."
        ),
        "patched": {
            "method": "DockerEnvironment._run_docker_compose_command",
            "also": "asyncio.create_subprocess_exec (observation only; passthrough "
            "when no guarded exec is active)",
            "scope": "compose `exec` argv whose service is the harness main service",
            "out_of_scope": "sidecar-service execs, non-exec compose commands "
            "(build/up/ps/cp/down), Windows containers (explicitly rejected)",
        },
        "ownership_signals": [
            f"{GUARD_ENV_VAR}=<per-exec uuid4> present in /proc/<pid>/environ "
            "(inherited across fork/exec, survives re-parenting)",
            "descendant of a marked root via /proc/<pid>/stat ppid chain (<=64 hops)",
            "member of a process group led by a marked root",
            "holder of this exec's inherited stdio pipe, matched by "
            "/proc/<pid>/fd/{0,1,2} link target (skipped when the container has "
            "no readlink)",
        ],
        "never_signalled": [
            "pid 1",
            "the reaper shell itself",
            "zombies (already terminated; unreapable from inside, and excluded "
            "from the survivor count for the same reason)",
            "anything outside the union above",
        ],
        "ladder": {
            "term_grace_sec": TERM_GRACE_SEC,
            "abort_late_rounds": ABORT_LATE_ROUNDS,
            "post_client_late_rounds": POST_CLIENT_LATE_ROUNDS,
            "late_round_interval_sec": 0.25,
            "sweep_timeout_sec": SWEEP_TIMEOUT_SEC,
            "probe_timeout_sec": PROBE_TIMEOUT_SEC,
        },
        "fail_closed": [
            "install-time verification of the patched method's presence, "
            "coroutine-ness and signature",
            "per-environment containment probe (marker visible in "
            "/proc/self/environ, foreign environ readable, /proc enumerable) "
            "before the first main-container exec runs",
            "unparseable exec argv, detached (-d) exec, or Windows container: "
            "the exec is refused, never run unguarded",
            "unproven containment latches the harness process: every later "
            "main-container exec, including fresh verifier environments, is refused",
        ],
        "preserved": [
            "argv, shell wrapper, cwd, user, stdin, stdout streaming, exit code",
            "the original exception object and its type on the abort path",
            "normal completion (no sweep, no extra container work)",
            "unrelated pre-existing container services and concurrently marked execs",
        ],
        "transport": "the compose argv prefix and environment observed from the "
        "guarded exec itself, reused verbatim for the reaper",
        "container_dependencies": {
            "required": ["bash", "Linux /proc", "kill", "sleep"],
            "optional": ["readlink (adds the inherited-stdio-pipe signal)"],
        },
        "known_limits": [
            "a descendant that simultaneously replaces its environment, calls "
            "setsid, re-parents to PID 1 and closes the exec's stdio shares no "
            "inherited signal with the exec and is not detected",
            "SIGKILL of the harness process itself leaves no opportunity to "
            "contain anything",
            "no claim is made about deliberate escapes from a co-operating or "
            "hostile in-container process beyond the four signals above",
        ],
        "scripts": {
            "reaper_sha256": _sha256_text(REAPER_SH),
            "probe_sha256": _sha256_text(PROBE_SH),
        },
    }
    body["digest"] = _digest(body)
    return body


def policy_digest() -> str:
    return policy()["digest"]


def describe(package: str) -> dict[str, Any]:
    """Identification record for the manifest: policy plus patched runtime.

    Performs a real install in this process, so a harness whose exec path has
    drifted is reported as unpatchable at freeze/run time rather than at
    cancellation time.
    """
    record: dict[str, Any] = {
        "bootstrap": str(Path(__file__).resolve()),
        "bootstrap_sha256": _sha256_file(Path(__file__).resolve()),
        "package": package,
        "install_ok": False,
    }
    try:
        installed = install(package)
    except Exception as exc:  # reported, not raised: the caller decides
        record["policy"] = policy()
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record
    record.update(installed)
    record["install_ok"] = True
    return record


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


_REEXEC_FLAG = "NATIVE_HARNESS_REEXEC"


def _sibling_python(executable: Path) -> Path | None:
    """The venv interpreter next to a console script.

    A CLI on PATH is usually a symlink into its uv tool venv
    (~/.local/bin/pier -> .../tools/datacurve-pier/bin/pier), and the
    interpreter lives next to the *target*, so resolve before looking.
    """
    candidates = [Path(os.path.realpath(executable)).parent, executable.parent]
    for directory in candidates:
        for name in ("python3", "python"):
            candidate = directory / name
            if candidate.exists():
                return candidate
    return None


def _maybe_reexec(package: str, executable: Path, argv: list[str]) -> None:
    """Re-exec once under the harness venv interpreter if needed.

    The guard has to live in the same interpreter as the harness it patches.
    Rather than importing from a foreign environment - or worse, running
    unpatched - hand the whole invocation to the venv python next to the CLI.
    """
    root = _PACKAGES[package]["root"]
    if importlib.util.find_spec(root) is not None:
        return
    if os.environ.get(_REEXEC_FLAG) == "1":
        raise SystemExit(
            f"error: {root!r} is not importable from {sys.executable} even after "
            "re-exec; run this bootstrap with the harness venv python"
        )
    python = _sibling_python(executable)
    if python is None:
        raise SystemExit(
            f"error: {root!r} is not importable from {sys.executable} and no python "
            f"interpreter sits next to {executable}"
        )
    env = {**os.environ, _REEXEC_FLAG: "1"}
    _log(f"re-exec via {python} (package {root!r} not importable here)")
    os.execve(str(python), [str(python), str(Path(__file__).resolve()), *argv], env)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__, file=sys.stderr)
        return 2

    if args[0] == "--describe":
        if len(args) != 2:
            print("usage: native_harness.py --describe <harbor|pier>", file=sys.stderr)
            return 2
        print(json.dumps(describe(args[1]), indent=2, sort_keys=True))
        return 0
    if args[0] == "--policy":
        print(json.dumps(policy(), indent=2, sort_keys=True))
        return 0

    if len(args) < 2:
        print(
            "usage: native_harness.py <harbor|pier> <native-cli-path> [args...]",
            file=sys.stderr,
        )
        return 2
    package, executable_arg, *rest = args
    if package not in _PACKAGES:
        print(
            f"error: unknown package {package!r}; expected one of {sorted(_PACKAGES)}",
            file=sys.stderr,
        )
        return 2
    executable = Path(executable_arg)
    if not executable.is_file():
        print(f"error: native CLI not found: {executable}", file=sys.stderr)
        return 2

    _maybe_reexec(package, executable, args)

    record = install(package)
    _event(
        "launch",
        package=package,
        executable=str(executable),
        argv=rest,
        policy_digest=record["policy"]["digest"],
        target_digest=record["runtime"]["target_digest"],
    )

    # Run the *original* console script, byte-identical arguments. runpy keeps
    # this the harness's own entry point rather than a reimplementation of it.
    sys.argv = [str(executable), *rest]
    try:
        runpy.run_path(str(executable), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(str(code), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
