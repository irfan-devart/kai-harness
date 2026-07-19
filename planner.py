#!/usr/bin/env python3
"""Kai harness — Argo, the planner loop.

Argo keeps the board stocked with sharp, buildable cards so Kai (the build supervisor) always has
ready work. Each pass: read the current board; if the `kai:ready` queue is short, read the product's
PRD / milestones, spawn the Argo planner agent (headless Claude Code) to propose the next few cards,
and create them as GitHub issues labelled `kai:ready`.

Argo NEVER writes code and NEVER merges. It only creates cards and escalates planning calls. It is a
separate loop from the build supervisor (supervisor.py): Argo feeds the board, Kai drains it.

Run:
    zsh -lc 'python3 planner.py --project projects/supercoach.project.json --once'
    zsh -lc 'python3 planner.py --project projects/supercoach.project.json --once --dry-run'
    zsh -lc 'python3 planner.py --project projects/supercoach.project.json'   # continuous
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

import lib


STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
LEDGER = os.path.join(STATE_DIR, "planner-ledger.jsonl")


def ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def notify(env, text):
    """Best-effort Telegram line (never raises)."""
    return lib.tg_send(env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID"), text)


def ledger_append(record):
    row = {"ts": time.time()}
    row.update(record)
    try:
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        lib._log("planner ledger write failed: %s" % e)


def read_closed_titles(repo, limit=100):
    """Titles of the most recent ``limit`` CLOSED issues — the already-built, do-not-recreate set.

    Once Kai merges a card it CLOSES the linked issue, so a closed title is shipped work. A
    continuously-running Argo must not re-propose it just because the card has drained off the
    open board. Uses ``lib.run`` directly (same pattern as ``read_sources``); returns an empty
    list on any failure — a history read must never kill a planning pass.
    """
    rc, out, err = lib.run([
        "gh", "issue", "list", "--repo", repo,
        "--state", "closed", "--limit", str(int(limit)),
        "--json", "number,title",
    ])
    if rc != 0:
        lib._log("planner could not read closed issues: %s" % lib.tail(err, 6))
        return []
    try:
        data = json.loads(out or "[]")
    except ValueError as e:
        lib._log("planner closed-issues parse error: %s" % e)
        return []
    return [it.get("title", "") for it in data if it.get("title")]


def read_sources(repo_dir, sources, ref):
    """Read planning source files from a git ref (e.g. origin/dev), not the working tree.

    Reads each path via ``git show <ref>:<path>`` so Argo always plans from the integration branch,
    independent of whatever branch happens to be checked out in the repo.
    """
    chunks = []
    for rel in sources:
        rc, out, err = lib.run(["git", "-C", repo_dir, "show", "%s:%s" % (ref, rel)])
        if rc == 0:
            chunks.append("===== %s (%s) =====\n%s" % (rel, ref, out))
        else:
            lib._log("planner could not read %s@%s: %s" % (rel, ref, lib.tail(err, 5)))
            chunks.append("===== %s =====\n(could not read %s@%s)" % (rel, rel, ref))
    return "\n\n".join(chunks)


def build_argo_context(source_text, existing_titles, closed_titles, counts, need,
                       current_milestone, conventions_path):
    lines = [
        "PLANNING SOURCE (product PRD / milestones — the source of truth for what to build):",
        source_text or "(no source provided)",
        "",
        "CURRENT MILESTONE to plan: %s. Earlier milestones are COMPLETE — do NOT re-card them."
        % (current_milestone or "(unspecified — infer the earliest un-carded milestone)"),
        "",
        "CURRENT BOARD STATE: ready=%d, doing=%d, review=%d." % (
            counts["ready"], counts["doing"], counts["review"]),
        "Open card titles already on the board (do NOT duplicate any of these):",
    ]
    lines += (["- %s" % t for t in existing_titles] or ["(none)"])
    lines += [
        "",
        "ALREADY BUILT AND MERGED (do NOT recreate or re-propose these):",
    ]
    lines += (["- %s" % t for t in closed_titles] or ["(none)"])
    lines += [
        "",
        "ADD AT MOST %d card(s), in build order, for the earliest un-carded work in %s."
        % (need, current_milestone or "the current milestone"),
        "Cards must respect the repo conventions in %s and the docs/." % conventions_path,
        "Return the strict JSON described in your instructions — nothing after the JSON block.",
    ]
    return "\n".join(lines)


def work_once(project, env, dry_run=False):
    """One planning pass. Returns a short status string."""
    repo = project["github_repo"]
    repo_dir = project["repo_dir"]
    merge_target = project["merge_target"]
    labels = project["labels"]
    agents_dir = project["agents_dir"]
    conventions_path = project.get("conventions_path", "CONVENTIONS.md")
    target_ready = int(project.get("planner_target_ready", 3))
    sources = project.get("planning_sources", []) or []
    current_milestone = project.get("current_milestone")

    # 1) Is the ready queue already stocked?
    ready = lib.gh_issue_list(repo, labels["ready"])
    if len(ready) >= target_ready:
        return "stocked (%d ready, target %d)" % (len(ready), target_ready)

    doing = lib.gh_issue_list(repo, labels["doing"])
    review = lib.gh_issue_list(repo, labels["review"])
    counts = {"ready": len(ready), "doing": len(doing), "review": len(review)}
    existing_titles = [c["title"] for c in (ready + doing + review)]
    # Closed issues = already built and merged (Kai closes each card on merge). Feed their
    # titles to the planner as a distinct do-NOT-recreate set, bounded to the most recent ~100.
    closed_titles = read_closed_titles(repo, limit=100)
    need = target_ready - len(ready)

    # 2) Need a planning source to plan from.
    if not sources:
        notify(env, "Argo: no planning_sources set for %s — add the PRD path to the descriptor."
               % project.get("project"))
        ledger_append({"action": "plan", "result": "no-sources"})
        return "no-sources"
    lib.git_fetch(repo_dir)  # so origin/<merge_target> reflects the latest integration branch
    source_text = read_sources(repo_dir, sources, "origin/%s" % merge_target)

    # 3) Spawn the Argo planner agent (independent, headless).
    argo = lib.run_claude_agent(
        os.path.join(agents_dir, "argo.md"),
        build_argo_context(source_text, existing_titles, closed_titles, counts, need,
                           current_milestone, conventions_path),
        repo_dir,
        expect_json=True,
    )
    if not isinstance(argo, dict) or argo.get("_parse_error"):
        reason = argo.get("_parse_error") if isinstance(argo, dict) else str(argo)
        notify(env, "Argo planner output unreadable for %s — held. %s"
               % (project.get("project"), lib.tail(str(reason), 6)))
        ledger_append({"action": "plan", "result": "unparseable"})
        return "unparseable"

    # 4) Escalation takes precedence — never guess a scope/product call.
    escalate = argo.get("escalate")
    if escalate:
        notify(env, "\n".join([
            "ESCALATION (planning) — %s" % project.get("project"),
            str(escalate),
            "Argo will not guess a scope/product call. Board left as-is.",
        ]))
        ledger_append({"action": "plan", "result": "escalated", "note": str(escalate)})
        return "escalated"

    cards = (argo.get("cards") or [])[:need]
    if not cards:
        return "nothing-to-add"

    # 5) Dry-run: show proposals, create nothing.
    if dry_run:
        lib._log("DRY-RUN — %d proposed card(s), NOT created:" % len(cards))
        for i, c in enumerate(cards, 1):
            lib._log("  [%d] %s" % (i, (c.get("title") or "(no title)")))
            body_preview = lib.tail((c.get("body") or ""), 12).replace("\n", "\n        ")
            lib._log("        %s" % body_preview)
        ledger_append({"action": "plan", "result": "dry-run", "count": len(cards)})
        return "dry-run:%d" % len(cards)

    # 6) Create the cards as kai:ready issues.
    created = []
    for c in cards:
        title = (c.get("title") or "").strip()
        body = (c.get("body") or "").strip()
        if not title or not body:
            lib._log("skipping malformed card (missing title/body)")
            continue
        r = lib.gh_issue_create(repo, title, body, labels=[labels["ready"]])
        if r:
            created.append(r)
            ledger_append({"action": "card", "result": "created", "issue": r.get("number"), "title": title})
        else:
            ledger_append({"action": "card", "result": "create-failed", "title": title})

    if created:
        lines = ["Argo stocked %d card(s) on %s (kai:ready):" % (len(created), project.get("project"))]
        for r in created:
            lines.append("#%s %s" % (r.get("number"), r.get("url")))
        notify(env, "\n".join(lines))
    return "stocked:%d" % len(created)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Argo — Kai-harness planner (board-owner)")
    parser.add_argument("--project", required=True, help="path to kai.project.json")
    parser.add_argument("--once", action="store_true", help="run a single pass then exit")
    parser.add_argument("--dry-run", action="store_true", help="propose cards but create nothing")
    parser.add_argument("--poll-interval", type=int, default=300,
                        help="seconds between passes in continuous mode (default 300)")
    args = parser.parse_args(argv)

    ensure_state_dir()
    project = lib.load_project(args.project)  # asserts merge_target != prod_branch
    env = lib.load_env()

    if args.once:
        status = work_once(project, env, dry_run=args.dry_run)
        lib._log("planner pass: %s" % status)
        return 0

    while True:
        try:
            status = work_once(project, env, dry_run=args.dry_run)
            lib._log("planner pass: %s" % status)
        except Exception as e:  # a bad pass must never kill the loop
            lib._log("planner error: %s\n%s" % (e, traceback.format_exc()))
            notify(env, "Argo planner error (loop continues): %s" % lib.tail(str(e), 4))
        time.sleep(max(30, args.poll_interval))


if __name__ == "__main__":
    sys.exit(main())
