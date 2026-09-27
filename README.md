# Kai: AI agents that close tickets, with guardrails

Kai turns a GitHub board into a queue that AI agents work through on their own. One agent builds each ticket, a second agent reviews it adversarially, Kai runs your quality gate itself, and only then does anything merge. Anything risky goes to a human instead.

## Why this exists

Coding agents are good at writing code. They are bad at knowing when to stop, and nobody should let one merge its own work. Most "autonomous dev" setups skip that problem. Kai is built around it:

- **The builder never merges.** It commits on a branch and stops.
- **The reviewer did not write the code.** It is a separate agent with a separate prompt, told to find the reason not to ship.
- **The gate is run independently.** Kai runs your test/lint command itself and ignores what the builder claims.
- **Hard-to-reverse changes never merge on their own.** Auth, migrations, payments, secrets, deletions and deploy config are escalated to a human with the PR left open.
- **Prod is never a merge target.** Kai merges into a staging branch like `dev`. Promotion to prod stays a human decision, and Kai refuses to start if the two branches are the same.

The goal is a team that ships more without lowering the bar: agents do the ticket work, humans keep the decisions that matter.

## How it works

```
 GitHub issue          Dae             Kai              Tech-Lead          Kai
 labelled      ->   builds on   ->   runs the    ->   reviews the   ->   merges to dev
 kai:ready          a branch         gate itself      diff              (or escalates)
```

1. **Argo (optional planner)** reads your PRD and current milestone and creates small, dependency-ordered cards with acceptance criteria. It never writes code.
2. **Kai (the supervisor)** picks the oldest `kai:ready` card, creates a branch and hands it to Dae.
3. **Dae (the builder)** implements the simplest change that meets the acceptance criteria, runs the gate, and commits. It stops and says so if the card is too big or unclear.
4. Kai pushes, opens the PR and runs the gate independently.
5. **Tech-Lead (the reviewer)** reviews the diff against the acceptance criteria and flags one-way doors.
6. Kai merges only if **review approves AND gate is green AND no one-way door is touched**. Otherwise the card is blocked or escalated, with the reason posted as a comment.
7. You get a Telegram message with links to the card and PR. Outcomes are written to `state/ledger.jsonl`.

The agents are headless [Claude Code](https://claude.com/claude-code) processes. Their prompts are plain Markdown in `agents/`, so you can read and change exactly what each one is told.

## Requirements

- macOS (the service templates use launchd; the Python itself is portable)
- Python 3, standard library only
- `git`, the GitHub CLI `gh` (authenticated), and the `claude` CLI on your login PATH
- A repo with a gate command (for example `pnpm check` or `make test`) and a staging branch
- Optional: a Telegram bot for notifications

## Setup

1. Clone this repo, for example to `~/kai-harness`.
2. Copy `kai.project.example.json` to `projects/my-app.project.json` and fill it in: repo path, GitHub repo, branches, gate command, one-way-door rules. Files in `projects/` are git-ignored.
3. Create the labels `kai:ready`, `kai:doing`, `kai:review`, `kai:blocked` in your repo.
4. Optional, for Telegram: create `~/.config/kai-harness/env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:abcdef
   TELEGRAM_CHAT_ID=987654321
   ```
5. Label an issue `kai:ready` and run one pass:
   ```zsh
   zsh -lc 'python3 supervisor.py --project projects/my-app.project.json --once'
   ```

When a single pass behaves, run it continuously as a launchd service: see `launchd/README.md` and `./kaictl start`. The planner has its own service and control script (`./argoctl`) and is off by default; card creation stays a human decision until you turn it on.

## Configuration

| Key | What it does |
|-----|--------------|
| `repo_dir`, `github_repo` | Where the code lives locally and on GitHub |
| `prod_branch`, `merge_target` | Kai merges into `merge_target` only, never `prod_branch` |
| `gate_cmd` | The command that must pass before anything merges |
| `one_way_door_globs`, `one_way_door_keywords` | Paths and added-code keywords that force escalation |
| `conventions_path` | Your repo's engineering rules, passed to every agent |
| `max_fix_attempts` | How many times Dae may retry after review feedback |
| `planning_sources`, `current_milestone`, `planner_target_ready` | Planner input and how many ready cards to keep queued |
| `project_board` | Optional: keeps a GitHub Projects status column in sync |

## Layout

```
supervisor.py        the loop: branch, build, gate, review, merge or escalate
planner.py           Argo: stocks the board from a PRD (never merges)
lib.py               gh / git / gate / Telegram / claude wrappers
agents/              the prompts for Dae, Tech-Lead and Argo
kaictl, argoctl      start / stop / status / logs for the two services
launchd/             service templates and install notes
tests/               unit tests for the pure functions
```

Run the tests with `python3 -m unittest discover -s tests`.

## Status

Built and run against a real Next.js product repo. It is a working harness, not a packaged product: expect to read the code and adapt the prompts and config to your repo.

## License

MIT. See `LICENSE`.
