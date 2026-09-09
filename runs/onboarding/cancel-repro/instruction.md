DEVELOPMENT FIXTURE — there is nothing here for an agent to solve.

This task is scored with Harbor's official `oracle` agent, which executes
`solution/solve.sh` verbatim and never calls a model. The reward is a property
of the *harness*, not of any agent's reasoning:

- `solution/solve.sh` starts two persistent counter writers inside the
  container and then blocks forever, so the agent phase can only end by
  host-side cancellation (`[agent].timeout_sec`).
- `tests/test.sh` runs afterwards, in the same container, and checks that
  **neither counter advances anywhere in its observation window** and that both
  writer processes are gone — while an **unrelated service started at container
  start keeps ticking**.

Reward 1 therefore means "the harness killed exactly the agent phase's process
tree when it cancelled it". Reward 0 means either the tree leaked into the
verifier phase (the bug) or the cleanup was too broad and took out unrelated
container processes.
