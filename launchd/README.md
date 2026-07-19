# Kai harness — launchd services

Kai runs as a **LaunchAgent** (as `jarvis`, in the login session) so it inherits the login PATH
(claude / node / pnpm / gh / git) and the user's auth. The Mac never sleeps, so it stays up. This is
why launchd, not cron: cron would fire discrete `--once` passes with no crash-restart; a persistent
agent wants a managed service.

## Kai supervisor (the builder loop) — RUNNING

Builds any `kai:ready` card: Dae builds -> Kai's own `pnpm check` gate -> Tech-Lead review -> merge
into `dev` -> close card -> Telegram. Continuous loop, polling the board every 60s. `KeepAlive`
restarts it if it dies. Never touches `main` (prod stays Irfan's manual gate).

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

Tail live: `tail -f ~/projects/kai-harness/logs/supervisor.err.log`

## Argo planner (the card maker) — NOT a service yet

Run deliberately when a PRD is ready and we want to review its cards:

    python3 planner.py --project projects/supercoach.project.json --once --dry-run   # propose, create nothing
    python3 planner.py --project projects/supercoach.project.json --once             # create kai:ready cards

Only add a `com.kai.planner.plist` LaunchAgent once we want autonomous, continuous planning.
Until then, card creation stays deliberate (write PRD -> run Argo -> review -> Kai builds).
