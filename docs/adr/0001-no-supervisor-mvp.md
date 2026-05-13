# ADR-0001: No Supervisor in MVP

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-05-10 |
| Deciders | Ziyu |
| Supersedes | – |
| Superseded by | (will be reconsidered for v0.4) |

## Context

Eonlet's design has a daemon called `eonletd` (the *supervisor*) that owns:
- The registry of running eonlets
- Inter-eonlet message routing
- Auto-restart on worker crash
- Global resource arbitration (API key rotation, budget aggregation)
- Health checks

This is necessary for Phase B (multi-eonlet runtime). For Phase A (single-eonlet excellence), the question is whether to introduce the supervisor early or defer it.

## Decision

**Defer the supervisor until v0.4 (Phase B).**

In the MVP, the CLI directly forks worker processes. Eonlet state is discovered by scanning `~/.eonlet/eonlets/*/` and reading per-instance files:
- `pid` — current PID, or absent if not running
- `status` — `running` / `paused` / `dead` / `hibernated`
- `heartbeat` — timestamp updated by worker every 10 seconds

`eonlet ls`, `eonlet ps`, `eonlet pause`, etc. work by:
1. Scanning the filesystem
2. Reading the per-instance files
3. Sending OS signals to PIDs (`kill -STOP`, `kill -CONT`, `kill -TERM`)
4. Connecting to `runtime.sock` for live interaction

## Rationale

### Why we wanted a supervisor

- Single source of truth for "what's running"
- Auto-restart on crash
- Inter-eonlet message routing (Phase B)
- Centralized observability
- "Feels professional" — systemd, k8s, supervisord all have a supervisor

### Why we're deferring it

**Complexity.** A supervisor doubles the binaries (CLI + supervisor + worker), adds an IPC layer (CLI ↔ supervisor), introduces a daemon-management problem (the supervisor itself needs systemd/launchd to be supervised), and adds a single point of failure.

**No Phase A user need.** A single user running 1–10 eonlets does not need:
- Cross-eonlet routing (we don't do multi-eonlet in Phase A)
- Auto-restart (a dead eonlet just shows up dead in `ls`; user restarts it)
- Global resource arbitration (budgets are per-eonlet)

**Filesystem-as-database is good enough.** Reading 10 files in `~/.eonlet/eonlets/` takes microseconds. `kill -0` to check liveness is microseconds. We don't need an in-memory registry.

**Removes a release-blocking dep.** With no supervisor, `pip install eonlet && eonlet create` just works. Adding a supervisor would require either:
- Auto-starting it on first command (added complexity, surprising behavior)
- Or making the user run `eonletd start` first (extra step, breaks the 5-minute quickstart)

**Easier to debug.** With no supervisor, `eonlet attach <id>` is a straight Unix-socket connection. With a supervisor, every command has to route through it; debugging becomes "is the bug in the CLI, the supervisor, the worker, or the IPC between them?"

## Consequences

### Positive

- Simpler MVP scope
- Faster to ship
- Standard Unix paradigm (PID files + signals + sockets)
- Single binary (CLI) controls everything
- No "is the daemon running?" failure mode

### Negative

- No auto-restart in MVP. If a worker crashes, user has to `restart` manually. (Acceptable: this should be rare with good code.)
- Inter-eonlet messaging in v0.4 will need a bigger refactor than if we'd built around supervisor from day 1. We accept this cost.
- "Eonlet runs forever" isn't quite true — if the user's machine reboots, eonlets are gone. (User can `eonlet create` again, and event-sourced state recovers everything.)

### Neutral

- `eonlet ls` does filesystem I/O instead of asking a daemon. With 1000+ eonlets, this could be slow; we'll address that in v0.4 by introducing the supervisor.

## When We'll Revisit

This ADR will be reconsidered at the start of Phase B (v0.4 development). Specifically:

- When multi-eonlet messaging is in scope, a routing daemon becomes natural
- When users report wanting auto-restart strongly enough that "just restart it" stops being acceptable
- When the filesystem scan in `eonlet ls` starts being slow for power users

At that point, we'll write ADR-NNNN to introduce `eonletd` with a **backward-compatible** opt-in: users who don't need it can keep running CLI-direct, and `eonletd` is started transparently when needed (or by `eonlet daemon start`).

## Alternatives Considered

### A. Build supervisor from day 1

Rejected because of complexity and the fact that we have no Phase A user need for it. We'd be building infrastructure for a future use case at the cost of shipping speed.

### B. Use systemd directly (each eonlet is a systemd user unit)

Rejected because:
- Doesn't work on macOS (launchd has a different file format and capabilities)
- Forces users to think about systemd, which is foreign to many
- Brittle to user environments (no systemd-user on some systems, doesn't work in WSL well)

### C. Use a lightweight third-party process manager (supervisord, pm2, etc.)

Rejected because:
- Adds an external dependency that users have to install
- We lose control over the lifecycle protocol; their semantics differ from ours
- Their config formats are different from our YAML

### D. Single-process model where one Python process hosts all eonlets as coroutines

Rejected because:
- Crash of one eonlet kills all
- Memory pressure from one eonlet affects all
- Loses the "agent as OS citizen" mental model that's a core differentiator

## References

- [SPEC.md §5](../SPEC.md#5-process-model) — process model
- [DIRECTORY_LAYOUT.md §3](../DIRECTORY_LAYOUT.md#3-eonlet-instance) — per-eonlet directory
- [CLI_REFERENCE.md](../CLI_REFERENCE.md) — commands that operate without a supervisor

## Update history

- 2026-05-10: Initial decision, accepted.
