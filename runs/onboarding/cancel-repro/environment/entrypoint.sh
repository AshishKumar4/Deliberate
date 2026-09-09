#!/bin/sh
# Container init for the cancellation-leak fixture.
#
# Everything started here exists BEFORE the agent phase begins. A cancellation
# guard that cleans up the agent's leaked process tree must leave it alone, so
# the verifier uses it as the "don't over-kill" control.
set -eu

STATE_DIR=/var/tmp/cancel-repro
SCRATCH_DIR=/var/tmp/cancel-repro.scratch

mkdir -p "$STATE_DIR" "$SCRATCH_DIR"

# Own session, so the service is parentage-indistinguishable from the agent's
# setsid descendant: a guard cannot pass by killing every session but PID 1's.
setsid /usr/local/bin/cancel-repro-service.sh \
    < /dev/null > "$STATE_DIR/service.log" 2>&1 &

exec "$@"
