#!/bin/bash
# DEVELOPMENT FIXTURE SOLUTION -- a process-isolation probe, not a solution to
# any problem. Run by Harbor's official `oracle` agent; no model is involved.
#
# Arms two persistent counter writers and then blocks forever, so the agent
# phase can only end via host-side cancellation ([agent].timeout_sec):
#
#   a: ordinary child -- same process group and session as the exec'd shell.
#   b: setsid child   -- its own session, reparented to PID 1, so a cleanup
#                        that only signals the exec's process group misses it.
#
# Both writers stay inside the container and die with it (`harbor run` deletes
# the environment), so nothing escapes the host.
set -u

STATE_DIR=/var/tmp/cancel-repro
SCRATCH_DIR=/var/tmp/cancel-repro.scratch

mkdir -p "$STATE_DIR" "$SCRATCH_DIR"
rm -f "$STATE_DIR/agent-a.tick" "$STATE_DIR/agent-a.pid" \
      "$STATE_DIR/agent-b.tick" "$STATE_DIR/agent-b.pid" \
      "$STATE_DIR/agent.armed"

sh /solution/tick-writer.sh a < /dev/null > /dev/null 2>&1 &
setsid sh /solution/tick-writer.sh b < /dev/null > /dev/null 2>&1 &

# Publish the armed marker only once both writers have a pid and a first tick.
# Without it the verifier could not tell "the agent deadline fired before the
# writers ever started" (fixture mis-tuned) from "the writers were cleaned up"
# (fixture passed).
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
    if [ -s "$STATE_DIR/agent-a.pid" ] && [ -s "$STATE_DIR/agent-a.tick" ] \
        && [ -s "$STATE_DIR/agent-b.pid" ] && [ -s "$STATE_DIR/agent-b.tick" ]; then
        date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE_DIR/agent.armed"
        break
    fi
    sleep 0.1
done

# Harbor redirects this to /logs/agent/oracle.txt, i.e. straight onto the host:
# an empty oracle.txt means the agent deadline beat the arming handshake.
echo "cancel-repro: writers armed (a=$(cat "$STATE_DIR/agent-a.pid" 2>/dev/null), b=$(cat "$STATE_DIR/agent-b.pid" 2>/dev/null)); blocking until the harness cancels the agent phase"

while :; do
    sleep 1
done
