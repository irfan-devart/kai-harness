#!/usr/bin/env python3
"""Kai — the supervisor loop.

Kai reads a GitHub board (issues + labels) and, for each ready card:

  1. spawns Dae (headless Claude Code) to BUILD the change on a fresh branch,
  2. runs the project's quality gate ITSELF (never trusting the builder's self-check),
  3. spawns the Tech-Lead (headless Claude Code) to REVIEW the diff adversarially,
  4. MERGES the PR itself — but only when ALL THREE hold:
        reviewer approves  AND  Kai's own gate is green  AND  it is not a one-way door.

The builder never merges its own work. One-way-door changes (auth, migrations, money,
secrets, deletion, prod-deploy config) are escalated to a human over Telegram with the
PR left open. The production branch is never a merge target — that assertion is enforced
at startup by ``load_project``.

Run:
    zsh -lc 'python3 supervisor.py --project /path/to/kai.project.json --once'
    zsh -lc 'python3 supervisor.py --project /path/to/kai.project.json'   # continuous
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

import lib


# --------------------------------------------------------------------------- #
# State locations — always relative to this script, never the cwd.            #
# --------------------------------------------------------------------------- #

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
OFFSET_FILE = os.path.join(STATE_DIR, "telegram_offset")
INBOUND_LOG = os.path.join(STATE_DIR, "inbound.log")
LEDGER = os.path.join(STATE_DIR, "ledger.jsonl")

# Idle heartbeat cadence for continuous mode (seconds). Kept long so Kai does not spam.
HEARTBEAT_INTERVAL = 6 * 60 * 60  # 6 hours


def ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


# --------------------------------------------------------------------------- #
# Telegram helpers                                                            #
# --------------------------------------------------------------------------- #

def notify(env, text):
    """Send a Telegram line to the configured chat (best-effort, never raises)."""
    return lib.tg_send(env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID"), text)


def _issue_url(project, num):
    """Canonical GitHub issue (card) URL.

    A bare https://github.com/... URL is auto-linkified by Telegram and, on iOS with the
    GitHub app installed, opens the card in the app via universal links (falls back to the
    browser otherwise). Kept as a plain URL on its own line — no markup to escape.
    """
    return "https://github.com/%s/issues/%s" % (project.get("github_repo"), num)


def _read_offset():
    try:
        with open(OFFSET_FILE, "r", encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


def _write_offset(value):
    try:
        with open(OFFSET_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(value)))
    except OSError as e:
        lib._log("could not persist telegram offset: %s" % e)


def poll_telegram(env):
    """Record inbound Telegram messages and advance the saved offset.

    Approval-reply handling is a later increment — for now we ONLY record inbound
    messages to ``state/inbound.log`` and do not act on any command they contain.
    """
    token = env.get("TELEGRAM_BOT_TOKEN")
    offset = _read_offset()
    updates = lib.tg_get_updates(token, offset)
    if not updates:
        return
    try:
        with open(INBOUND_LOG, "a", encoding="utf-8") as f:
            for u in updates:
                entry = {"ts": time.time()}
                entry.update(u)
                f.write(json.dumps(entry) + "\n")
    except OSError as e:
        lib._log("could not write inbound log: %s" % e)
    uids = [u["update_id"] for u in updates if u.get("update_id") is not None]
    if uids:
        _write_offset(max(uids) + 1)  # next offset = last update_id + 1


# --------------------------------------------------------------------------- #
# Ledger                                                                       #
# --------------------------------------------------------------------------- #

def ledger_append(record):
    """Append one JSON line describing a card outcome to ``state/ledger.jsonl``."""
    row = {"ts": time.time()}
    row.update(record)
    try:
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        lib._log("could not append to ledger: %s" % e)


# --------------------------------------------------------------------------- #
# Label transitions                                                            #
# --------------------------------------------------------------------------- #

def move_label(project, num, frm, to):
    """Move a card from one kai state label to another (add ``to``, remove ``frm``)."""
    labels = project.get("labels", {})
    add = [labels[to]] if to and to in labels else []
    remove = [labels[frm]] if frm and frm in labels else []
    return lib.gh_issue_edit_labels(project["github_repo"], num, add=add, remove=remove)


# --------------------------------------------------------------------------- #
# Prompt context builders                                                      #
# --------------------------------------------------------------------------- #

def build_dae_context(card, project, branch, feedback=None):
    """Assemble the task context handed to the builder (Dae)."""
    parts = [
        "Card #%s: %s" % (card["number"], card["title"]),
        "",
        "Acceptance criteria / description:",
        card.get("body") or "(no description provided)",
        "",
        "Repo conventions file (read and follow it if present): %s"
        % project.get("conventions_path", "(none specified)"),
        "Project gate command (run it yourself to self-verify green before finishing): %s"
        % project.get("gate_cmd", "(none specified)"),
        "You are on branch: %s" % branch,
        "Reminder: do NOT push, do NOT open a PR, do NOT merge. Build and commit only.",
    ]
    if feedback:
        parts += [
            "",
            "This is a FIX pass. The Tech-Lead review found blocking issues you must "
            "resolve on this same branch:",
            feedback,
        ]
    return "\n".join(parts)


def build_tech_lead_context(card, pr, diff):
    """Assemble the task context handed to the reviewer (Tech-Lead)."""
    return "\n".join([
        "Reviewing PR #%s for card #%s: %s" % (pr.get("number"), card["number"], card["title"]),
        "",
        "Acceptance criteria / description the change must meet:",
        card.get("body") or "(no description provided)",
        "",
        "Unified diff under review:",
        diff or "(empty diff)",
    ])


# --------------------------------------------------------------------------- #
# One pass over the board                                                      #
# --------------------------------------------------------------------------- #

def work_once(project, env, state):
    """Process at most one ready card. Returns a short status string.

    Statuses: idle | merged | escalated | blocked | gate-red | dae-blocked | error
    """
    repo = project["github_repo"]
    repo_dir = project["repo_dir"]
    merge_target = project["merge_target"]
    prod_branch = project.get("prod_branch")
    proj_name = project.get("project")
    labels = project["labels"]
    agents_dir = project["agents_dir"]
    gate_cmd = project["gate_cmd"]
    branch_prefix = project.get("branch_prefix", "kai/")
    max_fix_attempts = int(project.get("max_fix_attempts", 1))

    # 1) Record inbound Telegram (do not act on it yet).
    poll_telegram(env)

    # 2) Any ready cards?
    ready = lib.gh_issue_list(repo, labels["ready"])
    if not ready:
        return "idle"

    # 3) Take the first ready card and move it into "doing".
    card = min(ready, key=lambda c: c["number"] or 0)  # oldest first: respect Argo dependency-ordered creation
    num = card["number"]
    title = card["title"]
    branch = "%s%s" % (branch_prefix, num)
    move_label(project, num, "ready", "doing")
    notify(env, "\n".join([
        "BUILDING — %s #%s" % (proj_name, num),
        title,
        "",
        "Dae is writing the change; then I run the quality gate and Tech-Lead review myself.",
        "Card: %s" % _issue_url(project, num),
        "Next: I'll message you when it merges to %s, or if it needs you." % merge_target,
    ]))

    # 4) Fresh branch off origin/<merge_target>.
    base = "origin/%s" % merge_target
    lib.git_fetch(repo_dir)
    if not lib.git_create_branch(repo_dir, branch, base):
        move_label(project, num, "doing", "blocked")
        notify(env, "\n".join([
            "COULD NOT START — %s #%s" % (proj_name, num),
            title,
            "",
            "I couldn't create the work branch %s off %s." % (branch, base),
            "Card: %s" % _issue_url(project, num),
            "Next: needs a human — the repo may be in a bad state.",
        ]))
        ledger_append({"issue": num, "action": "branch", "result": "error"})
        return "error"

    pr = None
    attempt = 0
    feedback = None

    # Build → gate → PR → review, with a bounded single fix-retry driven by the reviewer.
    while True:
        # 5) BUILD with Dae.
        dae = lib.run_claude_agent(
            os.path.join(agents_dir, "dae.md"),
            build_dae_context(card, project, branch, feedback),
            repo_dir,
            expect_json=True,
        )
        if not isinstance(dae, dict) or dae.get("_parse_error") or not dae.get("done"):
            reason = (
                dae.get("notes") or dae.get("_parse_error")
                if isinstance(dae, dict) else str(dae)
            ) or "builder did not report done"
            lib.gh_issue_comment(repo, num, "Kai: builder stopped without a shippable change.\n\n%s" % reason)
            move_label(project, num, "doing", "blocked")
            notify(env, "\n".join([
                "BLOCKED (builder stopped) — %s #%s" % (proj_name, num),
                title,
                "",
                "Dae couldn't produce a shippable change and stopped. Nothing was pushed.",
                "Why: %s" % lib.tail(str(reason), 6),
                "Card: %s" % _issue_url(project, num),
                "Next: needs you — the card may be unclear or too big.",
            ]))
            lib.git_reset_hard(repo_dir)  # abandon partial work cleanly
            ledger_append({"issue": num, "action": "build", "result": "dae-blocked", "notes": reason})
            return "dae-blocked"

        # 6) INDEPENDENT gate — authoritative, never trusts Dae's self-check.
        green, gate_tail = lib.run_gate(repo_dir, gate_cmd)
        if not green:
            lib.gh_issue_comment(repo, num, "Kai: independent gate is RED — not pushing.\n\n```\n%s\n```" % gate_tail)
            move_label(project, num, ("review" if pr else "doing"), "blocked")
            msg = [
                "BLOCKED (quality gate red) — %s #%s" % (proj_name, num),
                title,
                "",
                "My own `%s` failed, so I did NOT push or merge. Nothing landed on %s." % (gate_cmd, merge_target),
                "Gate tail:",
                lib.tail(gate_tail, 8),
                "Card: %s" % _issue_url(project, num),
            ]
            if pr:
                msg.append("PR: %s" % pr.get("url"))
            msg.append("Next: needs a fix — I never merge a red build.")
            notify(env, "\n".join(msg))
            lib.git_reset_hard(repo_dir)
            ledger_append({"issue": num, "action": "gate", "result": "gate-red"})
            return "gate-red"

        # 7) Gate green → push and (on first pass) open the PR into merge_target.
        if not lib.git_push(repo_dir, branch):
            move_label(project, num, ("review" if pr else "doing"), "blocked")
            notify(env, "\n".join([
                "BLOCKED (push failed) — %s #%s" % (proj_name, num),
                title,
                "Card: %s" % _issue_url(project, num),
                "Next: needs a human.",
            ]))
            ledger_append({"issue": num, "action": "push", "result": "error"})
            return "error"

        if pr is None:
            pr = lib.gh_pr_create(
                repo, head=branch, base=merge_target,
                title="[kai] %s" % title,
                body="Automated by Kai for #%s.\n\n%s\n\nCloses #%s" % (num, card.get("body") or "", num),
            )
            if not pr:
                move_label(project, num, "doing", "blocked")
                notify(env, "\n".join([
                    "BLOCKED (PR creation failed) — %s #%s" % (proj_name, num),
                    title,
                    "Card: %s" % _issue_url(project, num),
                    "Next: needs a human.",
                ]))
                ledger_append({"issue": num, "action": "pr", "result": "error"})
                return "error"
            move_label(project, num, "doing", "review")
        else:
            # Retry pass: new commits are on the existing PR; ensure it reads as in-review.
            move_label(project, num, "doing", "review")

        # 8) REVIEW with the Tech-Lead (independent, adversarial).
        diff = lib.gh_pr_diff(repo, pr["number"])
        files = lib.gh_pr_files(repo, pr["number"]) or lib.git_diff_names(repo_dir, base, branch)
        review = lib.run_claude_agent(
            os.path.join(agents_dir, "tech-lead.md"),
            build_tech_lead_context(card, pr, diff),
            repo_dir,
            expect_json=True,
        )
        if not isinstance(review, dict) or review.get("_parse_error"):
            # A review we cannot read is NOT an approval — hold for a human.
            reason = review.get("_parse_error") if isinstance(review, dict) else str(review)
            lib.gh_issue_comment(repo, num, "Kai: reviewer output was unparseable — holding for human.\n\n%s" % lib.tail(str(reason), 20))
            move_label(project, num, "review", "blocked")
            notify(env, "\n".join([
                "HELD FOR YOU (review unreadable) — %s #%s" % (proj_name, num),
                title,
                "",
                "The Tech-Lead's output couldn't be parsed. An unreadable review is never an "
                "approval, so I held it — nothing was merged.",
                "PR: %s" % pr.get("url"),
                "Card: %s" % _issue_url(project, num),
                "Next: needs you to eyeball the PR.",
            ]))
            ledger_append({"issue": num, "action": "review", "result": "unparseable"})
            return "blocked"

        approve = bool(review.get("approve"))
        blocking = review.get("blocking") or []
        tl_summary = review.get("summary", "")

        # 9) Decide. One-way door is checked from BOTH the reviewer and Kai's own analysis.
        owd_engine = lib.one_way_door(files, diff, project)
        owd = bool(review.get("one_way_door")) or owd_engine
        if owd:
            src = "reviewer" if review.get("one_way_door") else "engine (globs/keywords)"
            brief = "\n".join([
                "ESCALATION — one-way door on %s #%s" % (proj_name, num),
                title,
                "",
                "This change touches something hard to reverse (auth, secrets, migrations, "
                "money, or prod config), so I will NOT merge it on my own.",
                "Detected by: %s" % src,
                "Reviewer verdict: %s" % (tl_summary or "(none)"),
                ("Blocking: %s" % "; ".join(blocking)) if blocking else "",
                "",
                "The PR is OPEN and waiting for your decision — nothing was merged.",
                "PR: %s" % pr.get("url"),
                "Card: %s" % _issue_url(project, num),
                "Next: your call — review the PR, then merge it into %s yourself if you want it."
                % merge_target,
            ])
            move_label(project, num, "review", "blocked")
            notify(env, brief)
            ledger_append({"issue": num, "action": "review", "result": "escalated-one-way-door", "pr": pr.get("url")})
            return "escalated"

        if not approve:
            # Reviewer rejected. Comment the blocking items, then either fix-retry or block.
            body = "Kai: Tech-Lead did not approve.\n\nBlocking:\n" + "\n".join("- %s" % b for b in blocking)
            lib.gh_issue_comment(repo, num, body)
            if attempt < max_fix_attempts:
                attempt += 1
                feedback = "\n".join("- %s" % b for b in blocking) or tl_summary
                move_label(project, num, "review", "doing")
                continue  # loop Dae once more with the reviewer's feedback
            move_label(project, num, "review", "blocked")
            notify(env, "\n".join([
                "HELD FOR YOU (not approved) — %s #%s" % (proj_name, num),
                title,
                "",
                "Tech-Lead didn't approve after %s fix attempt(s). Nothing was merged." % attempt,
                "PR: %s" % pr.get("url"),
                "Card: %s" % _issue_url(project, num),
                "Next: your call — review the blocking notes on the PR.",
            ]))
            ledger_append({"issue": num, "action": "review", "result": "blocked-not-approved", "pr": pr.get("url")})
            return "blocked"

        # 10) approve == True AND gate green (this same pass) AND not one-way door → MERGE.
        if lib.gh_pr_merge(repo, pr["number"], method="squash"):
            # A squash-merge into a NON-default branch (merge_target, e.g. dev) does NOT
            # auto-close the linked issue, so Kai closes it and clears the review label
            # itself — otherwise merged cards pile up forever in "review".
            lib.gh_issue_close(repo, num)
            lib.gh_issue_edit_labels(repo, num, remove=[labels["review"]])
            notify(env, "\n".join([
                "MERGED to %s — %s #%s" % (merge_target, proj_name, num),
                title,
                "",
                "Passed my gate + Tech-Lead review. Card closed.",
                "PR: %s" % pr.get("url"),
                "Next: nothing needed. Promoting %s → prod (%s) stays your manual gate."
                % (merge_target, prod_branch),
            ]))
            ledger_append({"issue": num, "action": "merge", "result": "merged", "pr": pr.get("url"), "target": merge_target})
            return "merged"
        # Merge call itself failed (e.g. branch protection / conflict) — do not retry blindly.
        move_label(project, num, "review", "blocked")
        notify(env, "\n".join([
            "HELD FOR YOU (merge failed) — %s #%s" % (proj_name, num),
            title,
            "",
            "Approved and green, but the merge call itself failed (conflict or branch "
            "protection). I did not retry.",
            "PR: %s" % pr.get("url"),
            "Card: %s" % _issue_url(project, num),
            "Next: needs a human.",
        ]))
        ledger_append({"issue": num, "action": "merge", "result": "merge-failed", "pr": pr.get("url")})
        return "blocked"


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(description="Kai autonomous dev-harness supervisor")
    parser.add_argument("--project", required=True, help="path to kai.project.json")
    parser.add_argument("--once", action="store_true", help="run a single pass then exit")
    parser.add_argument("--poll-interval", type=int, default=60,
                        help="seconds between passes in continuous mode (default 60)")
    args = parser.parse_args(argv)

    ensure_state_dir()

    # load_project asserts merge_target != prod_branch and refuses to run otherwise.
    project = lib.load_project(args.project)
    env = lib.load_env()
    state = {}

    online = "\n".join([
        "Kai is online for %s." % project.get("project"),
        "Autonomous merges land on `%s`; prod (`%s`) stays your manual gate." % (
            project.get("merge_target"), project.get("prod_branch")),
        "Watching the board — I'll message you on each build, merge, or anything that needs you.",
        "Board: https://github.com/%s/issues" % project.get("github_repo"),
    ])
    lib._log("Kai online for %s (target %s, prod %s protected)" % (
        project.get("project"), project.get("merge_target"), project.get("prod_branch")))
    notify(env, online)

    if args.once:
        status = work_once(project, env, state)
        lib._log("pass complete: %s" % status)
        return 0

    last_heartbeat = time.time()
    while True:
        try:
            status = work_once(project, env, state)
            lib._log("pass complete: %s" % status)
            if status == "idle":
                now = time.time()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    notify(env, "Kai heartbeat — idle. Board is clear for %s; prod (%s) protected. Nothing needs you." % (
                        project.get("project"), project.get("prod_branch")))
                    last_heartbeat = now
        except Exception as e:  # one bad card must never kill the loop
            tb = traceback.format_exc()
            lib._log("unhandled error in pass: %s\n%s" % (e, tb))
            notify(env, "Kai hit an error (the loop keeps running): %s" % lib.tail(str(e), 4))
        time.sleep(max(1, args.poll_interval))


if __name__ == "__main__":
    sys.exit(main())
