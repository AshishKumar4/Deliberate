#!/bin/sh
# UNRELATED pre-existing service, started by the entrypoint at container start.
#
# Its heartbeat must keep advancing across the whole verifier window: that is
# what proves the cancellation guard is narrow (kills the agent phase's process
# tree) rather than blunt (nukes the container).
#
# Ticks are fixed width so the file size never changes -- readers can never see
# a torn value, and `mv` from a scratch dir makes each publish atomic.
set -u

STATE_DIR=/var/tmp/cancel-repro
SCRATCH_DIR=/var/tmp/cancel-repro.scratch

mkdir -p "$STATE_DIR" "$SCRATCH_DIR"
echo $$ > "$STATE_DIR/service.pid"

n=0
while :; do
    n=$((n + 1))
    printf '%012d\n' "$n" > "$SCRATCH_DIR/service.tick"
    mv -f "$SCRATCH_DIR/service.tick" "$STATE_DIR/service.tick"
    sleep 0.25
done
