# Kai — autonomous dev-harness engine

Kai is a supervisor loop that runs continuously on a Mac under launchd. It reads a
GitHub board (issues + labels), and for each ready card: spawns a headless Claude Code
process ("Dae") to build the change on a branch, independently runs the project's
quality gate, spawns another headless Claude Code process ("Tech-Lead") to review the
diff, and **merges the PR itself only if the reviewer approves AND the gate is green AND
it is not a one-way-door change**. It notifies a human over Telegram and escalates
one-way doors instead of merging.

The builder (Dae) **never merges its own work** — only the supervisor merges.

## The 3-repo model

Kai is deliberately split across three homes so the engine, the work, and the human
narrative never bleed into each other:

1. **kai-harness** (this repo) — the *engine*. The supervisor loop, the toolbox
   (`lib.py`), and the agent prompts (`agents/`). Project-agnostic; carries no
   project-specific configuration.
2. **each project repo** — carries a `kai.project.json` descriptor at its root that
   tells Kai how to build and gate *that* project (repo dir, GitHub repo, branches,
   gate command, one-way-door rules, labels). See `kai.project.example.json`.
3. **the digital brain** — the human narrative: what got built, why, and the decisions
   around it. Kai writes machine outcomes to `state/ledger.jsonl`; the human story
   lives in the brain, not here.

## How to run

```zsh
# single pass, then exit — the normal way to test a change end to end
zsh -lc 'python3 supervisor.py --project /path/to/kai.project.json --once'

# continuous supervisor loop (default 60s poll interval)
zsh -lc 'python3 supervisor.py --project /path/to/kai.project.json'

# custom poll interval
zsh -lc 'python3 supervisor.py --project /path/to/kai.project.json --poll-interval 120'
```

It runs under launchd via `zsh -lc` so it inherits the **login PATH** — that is how the
subprocesses find `claude`, `node`, `pnpm`, `gh`, and `git`. See
`launchd/com.kai.supervisor.plist` for the service template.

Environment (Telegram credentials) is read from `~/.config/kai-harness/env`:

```
TELEGRAM_BOT_TOKEN=123456:abcdef
TELEGRAM_CHAT_ID=987654321
```

## Safety rules (the non-negotiables)

- **The writer never merges.** Dae builds and commits on a branch; it does not push,
  open PRs, or merge. Only the supervisor merges.
- **The supervisor merges only on approve + green + not-one-way-door.** All three must
  hold: the Tech-Lead review approves, Kai's own independent gate run is green, and the
  change touches no one-way door.
- **The prod branch is never a merge target.** Kai merges into `merge_target` (e.g.
  `dev`), never into `prod_branch`. The supervisor asserts `merge_target != prod_branch`
  at startup and refuses to run otherwise — promotion to prod stays a human gate.
- **One-way doors escalate, they do not merge.** Auth, data migrations, money/payments,
  secrets, deletion, and production deploy config are escalated to a human over Telegram
  with the PR left open.

## Layout

```
supervisor.py                     the loop (spawn Dae, gate, spawn Tech-Lead, merge)
lib.py                            the toolbox: gh/git/gate/telegram/claude wrappers
agents/dae.md                     builder prompt (Daedalus)
agents/tech-lead.md               reviewer prompt (independent, adversarial)
kai.project.example.json          the project descriptor schema
launchd/com.kai.supervisor.plist  launchd service template
state/                            runtime state (git-ignored): ledger, offsets, logs
tests/test_lib.py                 unit tests for the pure functions
```
