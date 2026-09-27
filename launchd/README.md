# Kai harness — launchd services

Kai runs as a **LaunchAgent** (as your user, in the login session) so it inherits the login PATH
(claude / node / pnpm / gh / git) and the user's auth. This is
why launchd, not cron: cron would fire discrete `--once` passes with no crash-restart; a persistent
agent wants a managed service.

## Kai supervisor (the builder loop) 

Builds any `kai:ready` card: Dae builds -> Kai's own gate command -> Tech-Lead review -> merge
into `dev` -> close card -> Telegram. Continuous loop, polling the board every 300s. `KeepAlive`
restarts it if it dies. Never touches the prod branch (promotion stays a human gate).

Install / start:

    cp launchd/com.kai.supervisor.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kai.supervisor.plist

Stop:

    launchctl bootout gui/$(id -u)/com.kai.supervisor

Status / restart:

    launchctl list | grep kai                                 # PID + last exit status
    launchctl kickstart -k gui/$(id -u)/com.kai.supervisor    # force restart

## Logs (on disk)

- `logs/supervisor.err.log` — the `[kai]` log lines + tracebacks (the main debug log)
- `logs/supervisor.out.log` — stdout
- `state/ledger.jsonl` — structured per-card outcomes (merged / gate-red / escalated / ...)

Tail live: `tail -f $KAI_HARNESS_DIR/logs/supervisor.err.log`

## Argo planner (the card maker) — service BUILT, not loaded

Argo keeps the board stocked with `kai:ready` cards from the PRD/milestones. It never writes
code and never merges. Continuous loop, polling every 300s; dedups against open cards AND
already-merged (closed) issues so it won't re-propose shipped work.

The `com.kai.planner.plist` LaunchAgent and its `argoctl` control script exist in the repo but
are **deliberately NOT loaded** — card creation stays a human decision until we choose to turn
autonomous planning on. Manage it exactly like `kaictl` manages the supervisor:

    ./argoctl start      # deploy plist + load (starts the continuous planner loop)
    ./argoctl stop       # unload + remove the installed plist
    ./argoctl status     # loaded? running? + last log lines
    ./argoctl logs       # tail -f logs/planner.err.log

Run deliberately (no service) when a PRD is ready and we want to review its cards first:

    python3 planner.py --project projects/my-app.project.json --once --dry-run   # propose, create nothing
    python3 planner.py --project projects/my-app.project.json --once             # create kai:ready cards

Logs: `logs/planner.err.log` (the `[kai]` lines + tracebacks) and `logs/planner.out.log`;
structured planning outcomes in `state/planner-ledger.jsonl`.
