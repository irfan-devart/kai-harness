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
        # No file globs hit, but the diff text mentions a keyword (case-insensitive).
        files = ["src/checkout/Cart.tsx"]
        diff = "+ // handle PAYMENT capture flow\n+ const x = 1"
        self.assertTrue(lib.one_way_door(files, diff, PROJECT))

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
