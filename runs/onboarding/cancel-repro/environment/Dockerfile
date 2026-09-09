# DEVELOPMENT FIXTURE image for the Docker exec cancellation-leak repro.
#
# ubuntu:24.04 is already cached by the terminal-bench-2 suite, and the fixture
# needs nothing beyond bash/coreutils/util-linux, so the build stays offline
# and takes only the two COPY layers below.
FROM ubuntu:24.04

# The fixture parks one agent descendant in its own session, so setsid(1) is a
# hard requirement. Fail at build time rather than half-arming the fixture.
RUN command -v setsid > /dev/null

COPY preexisting-service.sh /usr/local/bin/cancel-repro-service.sh
COPY entrypoint.sh /usr/local/bin/cancel-repro-entrypoint.sh
RUN chmod 0755 /usr/local/bin/cancel-repro-service.sh \
               /usr/local/bin/cancel-repro-entrypoint.sh

WORKDIR /app

# Harbor's compose overlay sets `command: ["sh", "-c", "sleep infinity"]` but
# never overrides ENTRYPOINT, so this wrapper gets to start the unrelated
# service at container start and then hand PID 1 to the keepalive.
ENTRYPOINT ["/usr/local/bin/cancel-repro-entrypoint.sh"]
