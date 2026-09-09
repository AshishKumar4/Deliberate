#!/bin/sh
# Persistent counter writer started by the oracle solution during the agent
# phase. Every process descended from that phase -- including this one -- must
# be dead once the harness cancels the phase.
#
# Invoked as `sh /solution/tick-writer.sh <label>`, so the exec bit that
# survived the upload does not matter.
set -u

label="${1:?usage: tick-writer.sh <label>}"
STATE_DIR=/var/tmp/cancel-repro
SCRATCH_DIR=/var/tmp/cancel-repro.scratch

mkdir -p "$STATE_DIR" "$SCRATCH_DIR"
echo $$ > "$STATE_DIR/agent-$label.pid"

n=0
while :; do
    n=$((n + 1))
    printf '%012d\n' "$n" > "$SCRATCH_DIR/agent-$label.tick"
    mv -f "$SCRATCH_DIR/agent-$label.tick" "$STATE_DIR/agent-$label.tick"
    sleep 0.25
done
