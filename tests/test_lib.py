"""Unit tests for the pure functions in lib.py.

Run from the repo root with:  python3 -m unittest -v
"""

import os
import sys
import tempfile
import unittest

# Make the repo-root modules importable regardless of the invocation directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib  # noqa: E402


# --------------------------------------------------------------------------- #
# one_way_door                                                                 #
# --------------------------------------------------------------------------- #

PROJECT = {
    "one_way_door_globs": ["firestore.rules", "**/auth/**", "**/migrations/**",
                           "wrangler.jsonc", "**/*.secret*"],
    "one_way_door_keywords": ["secret", "migration", "payment", "delete user",
                              "drop table", "deploy prod"],
}


class TestOneWayDoor(unittest.TestCase):
    def test_glob_match_nested_auth(self):
        files = ["src/components/App.tsx", "src/auth/login.ts"]
        self.assertTrue(lib.one_way_door(files, "some clean diff text", PROJECT))

    def test_glob_match_root_level_file(self):
        # A top-level file named exactly like a no-slash glob still matches.
        self.assertTrue(lib.one_way_door(["firestore.rules"], "", PROJECT))
        self.assertTrue(lib.one_way_door(["wrangler.jsonc"], "", PROJECT))

    def test_glob_match_root_secret_via_doublestar(self):
        # "**/*.secret*" must catch a root-level secret file too.
        self.assertTrue(lib.one_way_door(["config.secret.json"], "", PROJECT))

    def test_glob_match_nested_migration(self):
        self.assertTrue(lib.one_way_door(["db/migrations/0007_add_col.sql"], "", PROJECT))

    def test_keyword_match_in_diff(self):
        # No file globs hit, but an ADDED non-comment code line mentions a keyword
        # (case-insensitive). Returns the specific "keyword:<kw>" trigger string.
        files = ["src/checkout/Cart.tsx"]
        diff = "+ const flow = capturePayment();\n+ const x = 1"
        self.assertEqual(lib.one_way_door(files, diff, PROJECT), "keyword:payment")

    def test_clean_change_is_negative(self):
        files = ["src/components/Button.tsx", "README.md"]
        diff = "+ const label = 'Save';\n- const label = 'OK';"
        self.assertFalse(lib.one_way_door(files, diff, PROJECT))

    def test_empty_inputs_are_negative(self):
        self.assertFalse(lib.one_way_door([], "", PROJECT))
        self.assertFalse(lib.one_way_door(None, None, PROJECT))

    def test_doublestar_prefix_is_not_a_catch_all(self):
        # Regression guard: "**/auth/**" must NOT match an unrelated file just because
        # its trailing segment is a bare wildcard.
        self.assertFalse(lib.one_way_door(["src/components/Button.tsx"], "", PROJECT))


# --------------------------------------------------------------------------- #
# one_way_door — comment-aware keyword scan + trigger string (Change 2)        #
# --------------------------------------------------------------------------- #

OWD_PROJECT = {
    "one_way_door_globs": ["wrangler.jsonc", "**/auth/**", "**/migrations/**"],
    "one_way_door_keywords": ["firebase-admin", "payment", "drop table"],
}


class TestRuleFiles(unittest.TestCase):
    """A change that edits the rules it is reviewed against must never self-merge."""

    P = dict(PROJECT, conventions_path="docs/CONVENTIONS.md")

    def test_root_claude_md_is_held(self):
        self.assertEqual(lib.one_way_door(["CLAUDE.md"], "", self.P), "rules:**/CLAUDE.md")

    def test_nested_agents_md_is_held(self):
        self.assertEqual(lib.one_way_door(["apps/web/AGENTS.md"], "", self.P), "rules:**/AGENTS.md")

    def test_claude_dir_is_held(self):
        self.assertEqual(lib.one_way_door([".claude/settings.json"], "", self.P), "rules:.claude/**")

    def test_conventions_path_is_held(self):
        self.assertEqual(lib.one_way_door(["docs/CONVENTIONS.md"], "", self.P), "rules:docs/CONVENTIONS.md")

    def test_ordinary_docs_are_not_held(self):
        self.assertIsNone(lib.one_way_door(["docs/README.md", "src/app.ts"], "+ok\n", self.P))

    def test_no_conventions_path_still_guards_fixed_set(self):
        self.assertEqual(lib.one_way_door(["CLAUDE.md"], "", PROJECT), "rules:**/CLAUDE.md")


class TestOneWayDoorCommentAware(unittest.TestCase):
    def test_keyword_only_in_line_comment_returns_none(self):
        # (a) The only "firebase-admin" sits in a // comment — the exact false positive
        # that wrongly escalated cards #34/#35. It must NOT fire.
        diff = "\n".join([
            "diff --git a/src/db.ts b/src/db.ts",
            "+++ b/src/db.ts",
            "@@ -1,2 +1,3 @@",
            "+// no firebase-admin here, we use the client SDK",
            "+const db = getFirestore();",
        ])
        self.assertIsNone(lib.one_way_door(["src/db.ts"], diff, OWD_PROJECT))

    def test_keyword_in_added_code_fires_with_keyword_reason(self):
        # (b) Same term in a real added code line DOES fire, with a "keyword:" reason.
        diff = "\n".join([
            "+++ b/src/server.ts",
            '+import admin from "firebase-admin";',
        ])
        self.assertEqual(
            lib.one_way_door(["src/server.ts"], diff, OWD_PROJECT),
            "keyword:firebase-admin",
        )

    def test_glob_fires_with_glob_reason_regardless_of_content(self):
        # (c) A changed wrangler.jsonc fires on the glob no matter how clean the body is.
        self.assertEqual(
            lib.one_way_door(["wrangler.jsonc"], "+const clean = true;", OWD_PROJECT),
            "glob:wrangler.jsonc",
        )

    def test_clean_diff_and_files_returns_none(self):
        # (d) No glob hit and no keyword in added code → None.
        diff = "\n".join([
            "+++ b/src/ui/Button.tsx",
            "+const label = 'Save';",
            "-const label = 'OK';",
        ])
        self.assertIsNone(lib.one_way_door(["src/ui/Button.tsx"], diff, OWD_PROJECT))

    def test_block_comment_lines_do_not_fire(self):
        # Whole-line /* */ and " * " continuation comment lines are stripped too.
        diff = "\n".join([
            "+++ b/src/x.ts",
            "+/* firebase-admin is intentionally NOT used here */",
            "+ * firebase-admin still avoided",
            "+const ok = 1;",
        ])
        self.assertIsNone(lib.one_way_door(["src/x.ts"], diff, OWD_PROJECT))

    def test_keyword_survives_when_code_follows_closed_block_comment(self):
        # Conservative: real code after a closed /* */ on the same line is still scanned.
        diff = "+/* set up */ const p = new PaymentClient();"
        self.assertEqual(
            lib.one_way_door(["src/pay.ts"], diff, OWD_PROJECT),
            "keyword:payment",
        )

    def test_keyword_in_url_is_not_dropped_as_a_comment(self):
        # Conservative: the "//" in a URL scheme must not be mistaken for a line comment,
        # so a keyword living in a real URL still fires.
        diff = '+const u = "https://api.example.com/payment/capture";'
        self.assertEqual(
            lib.one_way_door(["src/pay.ts"], diff, OWD_PROJECT),
            "keyword:payment",
        )


# --------------------------------------------------------------------------- #
# load_env (KEY=VALUE parser)                                                  #
# --------------------------------------------------------------------------- #

class TestLoadEnv(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(prefix="kai_env_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_basic_parse(self):
        path = self._write("TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=987654\n")
        env = lib.load_env(path)
        self.assertEqual(env["TELEGRAM_BOT_TOKEN"], "123:abc")
        self.assertEqual(env["TELEGRAM_CHAT_ID"], "987654")

    def test_comments_and_blank_lines_ignored(self):
        path = self._write("# a comment\n\n  \nKEY=value\n")
        env = lib.load_env(path)
        self.assertEqual(env, {"KEY": "value"})

    def test_value_may_contain_equals(self):
        path = self._write("URL=https://x/y?a=1&b=2\n")
        env = lib.load_env(path)
        self.assertEqual(env["URL"], "https://x/y?a=1&b=2")

    def test_surrounding_quotes_stripped(self):
        path = self._write('A="quoted"\nB=\'single\'\n')
        env = lib.load_env(path)
        self.assertEqual(env["A"], "quoted")
        self.assertEqual(env["B"], "single")

    def test_missing_file_returns_empty(self):
        env = lib.load_env("/no/such/kai/env/file")
        self.assertEqual(env, {})


# --------------------------------------------------------------------------- #
# extract_last_json                                                            #
# --------------------------------------------------------------------------- #

class TestExtractLastJson(unittest.TestCase):
    def test_extracts_last_fenced_block(self):
        text = (
            "Here is a first block:\n"
            "```json\n{\"approve\": false}\n```\n"
            "and after more thought:\n"
            "```json\n{\"approve\": true, \"one_way_door\": false}\n```\n"
            "done."
        )
        obj = lib.extract_last_json(text)
        self.assertEqual(obj, {"approve": True, "one_way_door": False})

    def test_balanced_object_when_no_fence(self):
        text = 'preamble {"done": true, "summary": "built the thing"} trailing prose'
        obj = lib.extract_last_json(text)
        self.assertEqual(obj, {"done": True, "summary": "built the thing"})

    def test_braces_inside_strings_do_not_confuse_scan(self):
        text = 'noise {"note": "use a { brace } in prose", "done": true} end'
        obj = lib.extract_last_json(text)
        self.assertEqual(obj, {"note": "use a { brace } in prose", "done": True})

    def test_no_json_returns_none(self):
        self.assertIsNone(lib.extract_last_json("just some prose, no json here"))
        self.assertIsNone(lib.extract_last_json(""))
        self.assertIsNone(lib.extract_last_json(None))

    def test_fenced_takes_precedence_over_earlier_bare_object(self):
        text = '{"stale": true}\n```json\n{"fresh": true}\n```'
        obj = lib.extract_last_json(text)
        self.assertEqual(obj, {"fresh": True})


# --------------------------------------------------------------------------- #
# _find_project_item_id (pure — no I/O, no gh, no network)                     #
# --------------------------------------------------------------------------- #

class TestFindProjectItemId(unittest.TestCase):
    # Shape mirrors `gh project item-list <n> --owner <o> --format json`.
    ITEMS = {
        "items": [
            {"id": "PVTI_aaa", "content": {"type": "Issue", "number": 11}},
            {"id": "PVTI_bbb", "content": {"type": "Issue", "number": 12}},
            {"id": "PVTI_ccc", "content": {"type": "PullRequest", "number": 13}},
            {"id": "PVTI_ddd"},  # draft item with no linked content
        ]
    }

    def test_returns_matching_item_id(self):
        self.assertEqual(lib._find_project_item_id(self.ITEMS, 12), "PVTI_bbb")

    def test_returns_first_field_only_match(self):
        self.assertEqual(lib._find_project_item_id(self.ITEMS, 11), "PVTI_aaa")

    def test_absent_issue_returns_none(self):
        self.assertIsNone(lib._find_project_item_id(self.ITEMS, 99))

    def test_empty_or_malformed_object_returns_none(self):
        self.assertIsNone(lib._find_project_item_id({}, 12))
        self.assertIsNone(lib._find_project_item_id({"items": []}, 12))
        self.assertIsNone(lib._find_project_item_id(None, 12))

    def test_item_without_content_is_skipped(self):
        # The draft item (no content) must not raise or match.
        self.assertIsNone(lib._find_project_item_id({"items": [{"id": "PVTI_ddd"}]}, 12))


# --------------------------------------------------------------------------- #
# load_project safety assertion                                                #
# --------------------------------------------------------------------------- #

class TestLoadProject(unittest.TestCase):
    def _write_json(self, text):
        fd, path = tempfile.mkstemp(prefix="kai_proj_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_rejects_merge_target_equal_to_prod(self):
        path = self._write_json('{"prod_branch": "main", "merge_target": "main"}')
        with self.assertRaises(AssertionError):
            lib.load_project(path)

    def test_accepts_distinct_branches(self):
        path = self._write_json('{"prod_branch": "main", "merge_target": "dev"}')
        proj = lib.load_project(path)
        self.assertEqual(proj["merge_target"], "dev")


if __name__ == "__main__":
    unittest.main()
