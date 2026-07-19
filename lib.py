"""Kai harness — the toolbox.

Pure functions plus thin, defensive wrappers around the external tools Kai drives:
git, the GitHub `gh` CLI, the project quality gate, headless Claude Code, and Telegram.

Design rules for this module:
- Standard library only. No third-party dependencies (the runtime Mac has stock python3.9).
- Subprocess calls always use argument lists, never `shell=True`.
- Network functions (Telegram) never raise on failure — they log and return a safe default.
- The genuinely pure functions (`one_way_door`, the env parser, `extract_last_json`) carry
  no I/O so they can be unit-tested directly.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request


# --------------------------------------------------------------------------- #
# Logging                                                                      #
# --------------------------------------------------------------------------- #

def _log(msg):
    """Write a single prefixed line to stderr (captured by launchd's StandardErrorPath)."""
    sys.stderr.write("[kai] %s\n" % msg)
    sys.stderr.flush()


def tail(text, n=40):
    """Return the last ``n`` lines of ``text`` (used for gate/agent output tails)."""
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-n:])


# --------------------------------------------------------------------------- #
# Config loading (pure)                                                        #
# --------------------------------------------------------------------------- #

def load_env(path="~/.config/kai-harness/env"):
    """Parse a simple ``KEY=VALUE`` env file into a dict.

    Blank lines and ``#`` comments are ignored. The value is split on the first ``=``
    so values may themselves contain ``=``. Optional surrounding single/double quotes
    are stripped. A missing file yields an empty dict rather than an error.
    """
    env = {}
    full = os.path.expanduser(path)
    try:
        with open(full, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return env
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if key:
            env[key] = val
    return env


def load_project(path):
    """Load a ``kai.project.json`` descriptor and enforce the prod-branch safety rule.

    Asserts ``merge_target != prod_branch`` — Kai must never target the production
    branch, since promotion to prod is a human gate.
    """
    with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
        project = json.load(f)
    prod = project.get("prod_branch")
    target = project.get("merge_target")
    assert target != prod, (
        "merge_target (%r) must not equal prod_branch (%r) — prod is a human gate"
        % (target, prod)
    )
    return project


# --------------------------------------------------------------------------- #
# Subprocess wrapper                                                           #
# --------------------------------------------------------------------------- #

def run(args, cwd=None, env=None, timeout=None, check=False):
    """Run ``args`` (a list) and return ``(returncode, stdout, stderr)``.

    Never uses a shell. Child stdin is always ``/dev/null`` so a subprocess can never
    hang waiting on the supervisor's stdin. ``timeout`` raises ``TimeoutExpired`` (the
    caller decides how to treat it); ``check`` raises ``CalledProcessError`` on failure.
    """
    proc = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        timeout=timeout,
        check=check,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------- #
# GitHub via the `gh` CLI                                                      #
# --------------------------------------------------------------------------- #

def gh_issue_list(repo, label):
    """List open issues carrying ``label`` as ``[{number, title, body}, ...]``."""
    rc, out, err = run([
        "gh", "issue", "list",
        "--repo", repo,
        "--label", label,
        "--state", "open",
        "--json", "number,title,body",
    ])
    if rc != 0:
        _log("gh_issue_list failed (%s): %s" % (rc, tail(err, 10)))
        return []
    try:
        data = json.loads(out or "[]")
    except ValueError as e:
        _log("gh_issue_list parse error: %s" % e)
        return []
    return [
        {"number": it.get("number"), "title": it.get("title", ""), "body": it.get("body", "")}
        for it in data
    ]


def gh_issue_edit_labels(repo, num, add=None, remove=None):
    """Add and/or remove labels on an issue. Returns True on success."""
    args = ["gh", "issue", "edit", str(num), "--repo", repo]
    for lbl in (add or []):
        args += ["--add-label", lbl]
    for lbl in (remove or []):
        args += ["--remove-label", lbl]
    if len(args) == 6:  # nothing to add or remove
        return True
    rc, out, err = run(args)
    if rc != 0:
        _log("gh_issue_edit_labels failed (%s): %s" % (rc, tail(err, 10)))
    return rc == 0


def gh_issue_comment(repo, num, body):
    """Post a comment on an issue. Returns True on success."""
    rc, out, err = run([
        "gh", "issue", "comment", str(num), "--repo", repo, "--body", body,
    ])
    if rc != 0:
        _log("gh_issue_comment failed (%s): %s" % (rc, tail(err, 10)))
    return rc == 0


def gh_pr_create(repo, head, base, title, body):
    """Open a PR from ``head`` into ``base``. Returns ``{url, number}`` or None on failure."""
    rc, out, err = run([
        "gh", "pr", "create",
        "--repo", repo,
        "--head", head,
        "--base", base,
        "--title", title,
        "--body", body,
    ])
    if rc != 0:
        _log("gh_pr_create failed (%s): %s" % (rc, tail(err, 10)))
        return None
    url = (out or "").strip().splitlines()[-1].strip() if out.strip() else ""
    number = None
    m = re.search(r"/pull/(\d+)", url)
    if m:
        number = int(m.group(1))
    return {"url": url, "number": number}


def gh_pr_diff(repo, num):
    """Return the unified diff text for a PR (empty string on failure)."""
    rc, out, err = run(["gh", "pr", "diff", str(num), "--repo", repo])
    if rc != 0:
        _log("gh_pr_diff failed (%s): %s" % (rc, tail(err, 10)))
        return ""
    return out or ""


def gh_pr_files(repo, num):
    """Return the list of changed file paths for a PR."""
    rc, out, err = run([
        "gh", "pr", "view", str(num), "--repo", repo, "--json", "files",
    ])
    if rc != 0:
        _log("gh_pr_files failed (%s): %s" % (rc, tail(err, 10)))
        return []
    try:
        data = json.loads(out or "{}")
    except ValueError as e:
        _log("gh_pr_files parse error: %s" % e)
        return []
    return [f.get("path") for f in data.get("files", []) if f.get("path")]


def gh_pr_merge(repo, num, method="squash"):
    """Merge a PR using ``method`` (squash|merge|rebase). Returns True on success.

    This is the ONLY place Kai performs a merge, and the supervisor calls it only after
    the approve + green + not-one-way-door gate has passed.
    """
    flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}.get(method, "--squash")
    rc, out, err = run(["gh", "pr", "merge", str(num), "--repo", repo, flag])
    if rc != 0:
        _log("gh_pr_merge failed (%s): %s" % (rc, tail(err, 10)))
    return rc == 0


# --------------------------------------------------------------------------- #
# Git                                                                          #
# --------------------------------------------------------------------------- #

def _git(repo_dir, *args):
    """Run a git command scoped to ``repo_dir`` via ``git -C``."""
    return run(["git", "-C", repo_dir, *args])


def git_current_sha(repo_dir):
    """Return the current HEAD sha, or empty string on failure."""
    rc, out, err = _git(repo_dir, "rev-parse", "HEAD")
    return (out or "").strip() if rc == 0 else ""


def git_fetch(repo_dir, remote="origin"):
    """Fetch from ``remote`` so branches can be based on up-to-date remote refs."""
    rc, out, err = _git(repo_dir, "fetch", remote, "--prune")
    if rc != 0:
        _log("git_fetch failed (%s): %s" % (rc, tail(err, 10)))
    return rc == 0


def git_create_branch(repo_dir, name, base):
    """Create (or reset) branch ``name`` at ``base`` and check it out.

    Uses ``checkout -B`` so a re-run for the same card starts from a clean base rather
    than failing on an existing branch.
    """
    rc, out, err = _git(repo_dir, "checkout", "-B", name, base)
    if rc != 0:
        _log("git_create_branch failed (%s): %s" % (rc, tail(err, 10)))
    return rc == 0


def git_push(repo_dir, branch):
    """Push ``branch`` to origin and set upstream. Returns True on success."""
    rc, out, err = _git(repo_dir, "push", "-u", "origin", branch)
    if rc != 0:
        _log("git_push failed (%s): %s" % (rc, tail(err, 10)))
    return rc == 0


def git_checkout(repo_dir, ref):
    """Check out an existing ref (branch/sha)."""
    rc, out, err = _git(repo_dir, "checkout", ref)
    if rc != 0:
        _log("git_checkout failed (%s): %s" % (rc, tail(err, 10)))
    return rc == 0


def git_reset_hard(repo_dir, ref="HEAD"):
    """Reset the worktree to ``ref`` and remove untracked files — a clean slate.

    Used to abandon a failed build so the next card starts uncontaminated.
    """
    rc, out, err = _git(repo_dir, "reset", "--hard", ref)
    if rc != 0:
        _log("git_reset_hard failed (%s): %s" % (rc, tail(err, 10)))
    # Also drop untracked files/dirs left behind by the abandoned build.
    _git(repo_dir, "clean", "-fd")
    return rc == 0


def git_diff_names(repo_dir, base, branch):
    """Return the list of files that differ between ``base`` and ``branch``."""
    rc, out, err = _git(repo_dir, "diff", "--name-only", "%s..%s" % (base, branch))
    if rc != 0:
        _log("git_diff_names failed (%s): %s" % (rc, tail(err, 10)))
        return []
    return [line.strip() for line in (out or "").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Quality gate                                                                 #
# --------------------------------------------------------------------------- #

def run_gate(repo_dir, gate_cmd, timeout=1800):
    """Run the project's gate command and return ``(green, tail_of_output)``.

    Executed through ``zsh -lc`` so the login PATH (pnpm/node/etc.) is available.
    ``green`` is True only on return code 0. On failure (or timeout) the last ~40 lines
    of combined stdout+stderr are returned for escalation. This is Kai's OWN independent
    check — the supervisor never trusts the builder's self-reported gate result.
    """
    try:
        rc, out, err = run(["zsh", "-lc", gate_cmd], cwd=repo_dir, timeout=timeout)
    except subprocess.TimeoutExpired:
        _log("run_gate timed out after %ss" % timeout)
        return False, "gate timed out after %ss" % timeout
    except OSError as e:
        _log("run_gate failed to start: %s" % e)
        return False, "gate failed to start: %s" % e
    combined = (out or "")
    if err:
        combined = combined + ("\n" if combined else "") + err
    return (rc == 0), tail(combined, 40)


# --------------------------------------------------------------------------- #
# Telegram (stdlib urllib; never raises)                                       #
# --------------------------------------------------------------------------- #

def tg_send(token, chat_id, text):
    """Send a Telegram message. Returns True on HTTP 200, False otherwise (never raises)."""
    if not token or not chat_id:
        _log("tg_send skipped — missing token or chat_id")
        return False
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return getattr(resp, "status", resp.getcode()) == 200
    except Exception as e:  # network/HTTP/parse — all non-fatal for the loop
        _log("tg_send failed: %s" % e)
        return False


def tg_get_updates(token, offset=0):
    """Fetch pending Telegram updates as ``[{update_id, chat_id, text}, ...]``.

    Uses ``timeout=0`` (no long polling) so a single pass returns promptly. Returns an
    empty list on any error — the loop must never die because Telegram was unreachable.
    """
    if not token:
        return []
    url = "https://api.telegram.org/bot%s/getUpdates?offset=%s&timeout=0" % (token, int(offset))
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        _log("tg_get_updates failed: %s" % e)
        return []
    out = []
    for upd in payload.get("result", []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        out.append({
            "update_id": upd.get("update_id"),
            "chat_id": (msg.get("chat") or {}).get("id"),
            "text": msg.get("text", ""),
        })
    return out


# --------------------------------------------------------------------------- #
# JSON extraction (pure)                                                       #
# --------------------------------------------------------------------------- #

def _last_balanced_object(text):
    """Return the substring of the LAST balanced top-level ``{...}`` object, or None.

    Tracks JSON double-quoted strings (with escapes) so braces inside strings do not
    throw off the depth count. Best-effort fallback when no fenced block is present.
    """
    last = None
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    last = text[start:i + 1]
                    start = None
    return last


def extract_last_json(text):
    """Extract and parse the LAST ```json fenced block, else the last balanced object.

    Returns the parsed object, or None if nothing parses. Callers that require JSON
    (agents run with ``expect_json``) translate None into a ``_parse_error`` result.
    """
    if not text:
        return None
    # 1) Prefer the last ```json ... ``` fenced block.
    fenced = re.findall(r"```json\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        try:
            return json.loads(fenced[-1].strip())
        except ValueError:
            pass  # fall through to the balanced-object scan
    # 2) Otherwise the last balanced {...} object anywhere in the text.
    obj = _last_balanced_object(text)
    if obj is not None:
        try:
            return json.loads(obj)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- #
# Headless Claude Code agents                                                  #
# --------------------------------------------------------------------------- #

def run_claude_agent(agent_md_path, task_context, repo_dir, expect_json=False, timeout=1200):
    """Spawn a headless Claude Code process and return its result.

    The prompt is ``<agent_md>\\n\\n---\\nTASK CONTEXT:\\n<task_context>``. Claude is
    invoked as ``claude -p <prompt> --permission-mode bypassPermissions --add-dir <repo_dir>``
    through ``zsh -lc`` (so ``claude`` is on the login PATH), with cwd set to the repo.

    The prompt is passed as a positional shell argument (``$1``) rather than interpolated
    into the command string, so no shell escaping of the prompt is needed and there is no
    injection surface. Child stdin is ``/dev/null`` (via ``run``).

    - ``expect_json=False`` → returns raw stdout text.
    - ``expect_json=True``  → returns the parsed JSON dict, or ``{"_parse_error": tail}``
      if no JSON could be extracted.
    A timeout or unreadable prompt is returned as an error value, never raised.
    """
    try:
        with open(os.path.expanduser(agent_md_path), "r", encoding="utf-8") as f:
            agent_md = f.read()
    except OSError as e:
        msg = "could not read agent prompt %s: %s" % (agent_md_path, e)
        _log(msg)
        return {"_parse_error": msg} if expect_json else msg

    prompt = "%s\n\n---\nTASK CONTEXT:\n%s" % (agent_md, task_context)

    # zsh -lc '<script>' <argv0> <argv1...> — the prompt and repo_dir arrive as $1/$2,
    # keeping arbitrary prompt content out of the parsed command line entirely.
    cmd = [
        "zsh", "-lc",
        'claude -p "$1" --permission-mode bypassPermissions --add-dir "$2"',
        "kai-agent",  # $0
        prompt,       # $1
        repo_dir,     # $2
    ]
    try:
        rc, out, err = run(cmd, cwd=repo_dir, timeout=timeout)
    except subprocess.TimeoutExpired:
        msg = "claude agent timed out after %ss" % timeout
        _log(msg)
        return {"_parse_error": msg} if expect_json else msg
    except OSError as e:
        msg = "claude agent failed to start: %s" % e
        _log(msg)
        return {"_parse_error": msg} if expect_json else msg

    stdout = out or ""
    if rc != 0:
        _log("claude agent returned %s: %s" % (rc, tail(err or stdout, 10)))

    if not expect_json:
        return stdout

    parsed = extract_last_json(stdout)
    if parsed is None:
        combined = stdout + (("\n" + err) if err else "")
        return {"_parse_error": tail(combined, 40)}
    return parsed


# --------------------------------------------------------------------------- #
# One-way-door classification (pure)                                           #
# --------------------------------------------------------------------------- #

def _glob_match(path, pattern):
    """fnmatch with pragmatic ``**/`` support (fnmatch has no native ``**`` semantics).

    A ``**/``-prefixed pattern also matches the remainder anywhere in the path and against
    the basename, so e.g. ``**/*.secret*`` catches a root-level ``config.secret.json``.
    """
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.startswith("**/"):
        remainder = pattern[3:]
        if fnmatch.fnmatch(path, remainder):
            return True
        base = path.rsplit("/", 1)[-1]
        if fnmatch.fnmatch(base, remainder):
            return True
    return False


def one_way_door(changed_files, diff_text, project):
    """True if the change touches a one-way door (hard-to-reverse / high-blast-radius).

    A one-way door is triggered when ANY changed file matches ANY ``one_way_door_globs``
    entry, OR ANY ``one_way_door_keywords`` term appears in the diff text (case-insensitive).
    Pure and unit-testable — takes the already-computed files/diff/project as inputs.
    """
    globs = project.get("one_way_door_globs", []) or []
    keywords = project.get("one_way_door_keywords", []) or []

    for raw_path in (changed_files or []):
        norm = str(raw_path).replace("\\", "/")
        for pattern in globs:
            if _glob_match(norm, pattern):
                return True

    haystack = (diff_text or "").lower()
    for kw in keywords:
        if kw and str(kw).lower() in haystack:
            return True

    return False
