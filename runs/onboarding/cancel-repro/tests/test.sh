#!/bin/bash
# DEVELOPMENT FIXTURE VERIFIER.
#
# Scores HARNESS PROCESS ISOLATION, never model capability. Nothing an agent
# could write changes this reward: it is decided entirely by whether the
# harness killed the agent phase's process tree when it cancelled the phase,
# and by whether it left unrelated container processes alone.
#
# Reward 1 requires, all together:
#   * the fixture actually armed during the agent phase;
#   * neither agent-phase counter advances ANYWHERE in the observation window;
#   * no live process from the agent phase remains (zombies do not count as
#     live -- a reaped-but-unwaited orphan has an empty /proc/<pid>/cmdline);
#   * the unrelated pre-existing service is still ticking and still itself.
#
# Bare-shell only: ubuntu:24.04 ships no python, and /proc answers every
# question this fixture asks.
set -u

# Overridable only so the decision logic below can be exercised against a fake
# /proc and a fake state dir on a dev box; Harbor never sets these.
STATE_DIR="${CANCEL_REPRO_STATE_DIR:-/var/tmp/cancel-repro}"
VERIFIER_DIR="${CANCEL_REPRO_VERIFIER_DIR:-/logs/verifier}"
PROC_ROOT="${CANCEL_REPRO_PROC_ROOT:-/proc}"
SNAPSHOT_DIR="$VERIFIER_DIR/cancel-repro"

SAMPLES=10
INTERVAL=0.5              # 10 x 0.5s = 5.0s observation window
MIN_SERVICE_ADVANCE=4     # service ticks every 0.25s -> ~20 in the window

WRITER_NEEDLE="tick-writer.sh"
SOLVE_NEEDLE="/solution/solve.sh"
SERVICE_NEEDLE="cancel-repro-service.sh"

mkdir -p "$SNAPSHOT_DIR"

slurp() { if [ -f "$1" ]; then tr -d '[:space:]' < "$1" 2>/dev/null; fi; }

to_int() {
    case "$1" in
        '' | *[!0-9]*) printf '0\n' ;;
        *) printf '%s\n' "$((10#$1))" ;;
    esac
}

# Live iff /proc/<pid>/cmdline still names the expected program. Zombies expose
# an empty cmdline, so this never mistakes an unreaped corpse for a survivor --
# which matters here because PID 1 is `sleep infinity` and never reaps orphans.
proc_matches() {
    local pid="$1" needle="$2" cmd
    [ -n "$pid" ] || return 1
    cmd=$(tr '\0' ' ' < "$PROC_ROOT/$pid/cmdline" 2>/dev/null) || return 1
    case "$cmd" in *"$needle"*) return 0 ;; esac
    return 1
}

proc_state() {
    local pid="$1" line rest
    if [ -z "$pid" ] || [ ! -r "$PROC_ROOT/$pid/stat" ]; then
        printf 'gone\n'
        return
    fi
    line=$(cat "$PROC_ROOT/$pid/stat" 2>/dev/null) || { printf 'gone\n'; return; }
    rest=${line#*') '}
    printf '%s\n' "${rest%% *}"
}

# Count every live process still running agent-phase code, whatever its pid.
# Deliberately pid-agnostic: it also catches respawns and the exec'd wrapper.
count_agent_procs() {
    local n=0 p cmd
    for p in "$PROC_ROOT"/[0-9]*; do
        cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
        case "$cmd" in
            *"$WRITER_NEEDLE"* | *"$SOLVE_NEEDLE"*) n=$((n + 1)) ;;
        esac
    done
    printf '%s\n' "$n"
}

verdict() { if [ "$1" -eq 1 ]; then printf 'PASS\n'; else printf 'FAIL\n'; fi; }

echo "== cancel-repro verifier =="
echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "window=${SAMPLES}x${INTERVAL}s"
echo

armed_at=$(slurp "$STATE_DIR/agent.armed")
a_pid=$(slurp "$STATE_DIR/agent-a.pid")
b_pid=$(slurp "$STATE_DIR/agent-b.pid")
s_pid=$(slurp "$STATE_DIR/service.pid")

a_first=$(slurp "$STATE_DIR/agent-a.tick")
b_first=$(slurp "$STATE_DIR/agent-b.tick")
s_first=$(slurp "$STATE_DIR/service.tick")

echo "armed_at=${armed_at:-<missing>}"
echo "pids: a=${a_pid:-<missing>} b=${b_pid:-<missing>} service=${s_pid:-<missing>}"
echo "first ticks: a=${a_first:-<missing>} b=${b_first:-<missing>} service=${s_first:-<missing>}"
echo "agent-phase processes still live at window start: $(count_agent_procs)"
echo

fixture_armed=1
for value in "$armed_at" "$a_pid" "$b_pid" "$s_pid" "$a_first" "$b_first" "$s_first"; do
    [ -n "$value" ] || fixture_armed=0
done

a_moved=0
b_moved=0
a_prev="$a_first"
b_prev="$b_first"
s_last="$s_first"

i=0
while [ "$i" -lt "$SAMPLES" ]; do
    i=$((i + 1))
    sleep "$INTERVAL"

    a_now=$(slurp "$STATE_DIR/agent-a.tick")
    b_now=$(slurp "$STATE_DIR/agent-b.tick")
    s_now=$(slurp "$STATE_DIR/service.tick")

    [ "$a_now" = "$a_prev" ] || a_moved=1
    [ "$b_now" = "$b_prev" ] || b_moved=1

    echo "sample $i/$SAMPLES a=$a_now b=$b_now service=$s_now"

    a_prev="$a_now"
    b_prev="$b_now"
    s_last="$s_now"
done
echo

agent_procs_live=$(count_agent_procs)
a_state=$(proc_state "$a_pid")
b_state=$(proc_state "$b_pid")
s_state=$(proc_state "$s_pid")

a_live=0; proc_matches "$a_pid" "$WRITER_NEEDLE" && a_live=1
b_live=0; proc_matches "$b_pid" "$WRITER_NEEDLE" && b_live=1
s_live=0; proc_matches "$s_pid" "$SERVICE_NEEDLE" && s_live=1

service_advance=$(( $(to_int "$s_last") - $(to_int "$s_first") ))

writer_a_frozen=0
[ "$a_moved" -eq 0 ] && writer_a_frozen=1
writer_b_frozen=0
[ "$b_moved" -eq 0 ] && writer_b_frozen=1

agent_processes_reaped=0
if [ "$agent_procs_live" -eq 0 ] && [ "$a_live" -eq 0 ] && [ "$b_live" -eq 0 ]; then
    agent_processes_reaped=1
fi

unrelated_service_alive=0
if [ "$s_live" -eq 1 ] && [ "$service_advance" -ge "$MIN_SERVICE_ADVANCE" ]; then
    unrelated_service_alive=1
fi

reward=0
if [ "$fixture_armed" -eq 1 ] \
    && [ "$writer_a_frozen" -eq 1 ] && [ "$writer_b_frozen" -eq 1 ] \
    && [ "$agent_processes_reaped" -eq 1 ] && [ "$unrelated_service_alive" -eq 1 ]; then
    reward=1
fi

echo "proc states: a=$a_state b=$b_state service=$s_state"
echo "agent-phase processes still live at window end: $agent_procs_live"
echo "service advance over window: $service_advance (min $MIN_SERVICE_ADVANCE)"
echo
echo "fixture_armed                          : $(verdict "$fixture_armed")"
echo "agent_writer_frozen_same_session       : $(verdict "$writer_a_frozen")"
echo "agent_writer_frozen_separate_session   : $(verdict "$writer_b_frozen")"
echo "agent_processes_reaped                 : $(verdict "$agent_processes_reaped")"
echo "unrelated_service_alive                : $(verdict "$unrelated_service_alive")"
echo

if [ "$fixture_armed" -eq 0 ]; then
    echo "VERDICT: FIXTURE-INVALID -- the agent phase never armed the writers."
    echo "  Nothing was measured. Re-run with a larger --agent-timeout-multiplier"
    echo "  so the cold docker cp/chmod/exec round trip fits inside the agent"
    echo "  deadline. This is NOT evidence about the cancellation guard."
elif [ "$reward" -eq 1 ]; then
    echo "VERDICT: PASS -- the agent phase's process tree (both sessions) was"
    echo "  gone for the whole verifier window, and the unrelated pre-existing"
    echo "  service kept running."
elif [ "$writer_a_frozen" -eq 0 ] || [ "$writer_b_frozen" -eq 0 ] \
    || [ "$agent_processes_reaped" -eq 0 ]; then
    echo "VERDICT: LEAK -- agent-phase processes outlived the cancelled agent"
    echo "  phase and were still executing during verification."
else
    echo "VERDICT: OVER-KILL -- the agent phase was cleaned up, but the"
    echo "  unrelated pre-existing service did not survive it."
fi

# Host-side forensics: /logs is bind-mounted, so this lands in the trial dir.
cp -a "$STATE_DIR/." "$SNAPSHOT_DIR/" 2>/dev/null
cat > "$SNAPSHOT_DIR/summary.json" <<EOF
{
  "armed_at": "$armed_at",
  "samples": $SAMPLES,
  "sample_interval_sec": $INTERVAL,
  "agent_writer_same_session": {"pid": "$a_pid", "state": "$a_state", "first_tick": "$a_first", "last_tick": "$a_prev", "advanced": $a_moved},
  "agent_writer_separate_session": {"pid": "$b_pid", "state": "$b_state", "first_tick": "$b_first", "last_tick": "$b_prev", "advanced": $b_moved},
  "unrelated_service": {"pid": "$s_pid", "state": "$s_state", "first_tick": "$s_first", "last_tick": "$s_last", "advance": $service_advance},
  "agent_processes_live_at_window_end": $agent_procs_live
}
EOF

cat > "$VERIFIER_DIR/reward.json" <<EOF
{
  "reward": $reward,
  "fixture_armed": $fixture_armed,
  "agent_writer_frozen_same_session": $writer_a_frozen,
  "agent_writer_frozen_separate_session": $writer_b_frozen,
  "agent_processes_reaped": $agent_processes_reaped,
  "unrelated_service_alive": $unrelated_service_alive
}
EOF

echo
echo "reward=$reward (wrote $VERIFIER_DIR/reward.json)"
exit 0
