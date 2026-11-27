import os
import tomllib
import unittest
from unittest.mock import patch

from oncolab.config import ConfigError
from oncolab.env import parse_config_env, parse_env


class Environment(unittest.TestCase):
    def test_no_implicit_forwarding(self):
        self.assertEqual(parse_env([]), {})

    def test_literal_values_and_last_assignment_wins(self):
        self.assertEqual(
            parse_env([
                "MODE=old",
                "EMPTY=",
                "_ARG1=x=y",
                "LITERAL= a \\$HOME $(echo no)\n第二行 ",
                "MODE=cuda",
            ]),
            {
                "MODE": "cuda",
                "EMPTY": "",
                "_ARG1": "x=y",
                "LITERAL": " a \\$HOME $(echo no)\n第二行 ",
            },
        )

    def test_malformed_assignment_does_not_echo_input(self):
        for assignment in ("", "BAD-NAME", "=private", "1VAR=private", "BAD-NAME=private",
                           "BAD NAME=private", "UNICODÉ=private", "NAME=private\0"):
            with self.subTest(assignment=assignment), self.assertRaises(ConfigError) as caught:
                parse_env([assignment])
            self.assertNotIn("private", str(caught.exception))

    def test_named_local_forwarding_and_explicit_unset(self):
        with patch.dict(os.environ, {"MODE": "a = $b", "EMPTY": "", "OTHER": "private"}, clear=True):
            self.assertEqual(parse_env(["MODE", "EMPTY", "MISSING"]), {
                "MODE": "a = $b", "EMPTY": "", "MISSING": None,
            })
            self.assertEqual(parse_env(["MODE=old", "MODE", "MISSING=x", "MISSING", "EMPTY=z"]), {
                "MODE": "a = $b", "MISSING": None, "EMPTY": "z",
            })
            self.assertEqual(parse_env(["MODE", "MODE="]), {"MODE": ""})

    def test_config_values_and_cli_override(self):
        table = tomllib.loads("""
[env]
MODE = "old"
EMPTY = ""
ARG = 'space $HOME x=y'
""")["env"]
        configured = parse_config_env(table)
        merged = configured | parse_env(["MODE=cpu", "EXTRA=added"])
        self.assertEqual(merged, {"MODE": "cpu", "EMPTY": "", "ARG": "space $HOME x=y",
                                  "EXTRA": "added"})
        self.assertEqual(table["MODE"], "old")

    def test_config_requires_valid_names_and_string_values(self):
        for table in ("not a table", {"BAD-NAME": "private"}, {"VAR": 1},
                      {"VAR": False}, {"VAR": ["private"]}, {"VAR": {"a": "private"}},
                      {"VAR": "private\0"}):
            with self.subTest(table=table), self.assertRaises(ConfigError) as caught:
                parse_config_env(table)
            self.assertNotIn("private", str(caught.exception))
