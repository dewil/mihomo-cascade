import contextlib
import importlib.machinery
import importlib.util
import io
import pathlib
import re
import unittest
from unittest.mock import patch


SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "usr/local/sbin/mihomo-build-config"
)
LOADER = importlib.machinery.SourceFileLoader("mihomo_alias_test", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
builder = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(builder)


class AliasLabelsTests(unittest.TestCase):
    def build(self, payload):
        with patch.object(pathlib.Path, "read_text", return_value=payload):
            return builder._build_alias_regexes(["de", "nl"])

    def test_absent_preserves_defaults(self):
        with patch.object(
            pathlib.Path, "read_text", side_effect=FileNotFoundError
        ):
            actual = builder._build_alias_regexes(["de", "nl"])
        expected = {
            code + suffix: rf"(?i)\b{code.upper()}{suffix}\b"
            for code in ("de", "nl")
            for suffix in ("3", "2", "")
        }
        self.assertEqual(actual, expected)
        self.assertEqual(self.build("{}"), expected)

    def test_override_changes_only_selected_alias(self):
        defaults = self.build("{}")
        actual = self.build('{"de":"DE5"}')
        self.assertEqual(actual, dict(defaults, de=r"(?i)\bDE5\b"))
        for name in ("DE5", "node de5"):
            with self.subTest(name=name):
                self.assertIsNotNone(re.search(actual["de"], name))
        for name in ("DE", "DE2", "DE3", "DE50", "xDE5", "DE5x"):
            with self.subTest(name=name):
                self.assertIsNone(re.search(actual["de"], name))
        self.assertEqual(
            builder._assign_aliases(["DE5", "DE2", "DE3"], actual),
            {"de3": "DE3", "de2": "DE2", "de": "DE5"},
        )

    def test_invalid_mapping_is_sanitized(self):
        payloads = (
            "{",
            "[]",
            "null",
            '"text"',
            '{"zz":"DE5"}',
            '{"de4":"DE5"}',
            '{"de":5}',
            '{"de":null}',
            '{"de":true}',
            '{"de":"DE.*"}',
            '{"de":"DE5|DE2"}',
            '{"de":"DÉ5"}',
            '{"de":"DE５"}',
            '{"de":"DE5\\n"}',
            '{"de":"https://secret.invalid"}',
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as caught:
                        self.build(payload)
                self.assertEqual(caught.exception.code, 3)
                self.assertEqual(
                    stderr.getvalue(), "Invalid alias-labels.json\n"
                )

    def test_main_rejects_before_downloads_or_output(self):
        with (
            patch.object(builder, "_load_iso_codes", return_value=["de"]),
            patch.object(pathlib.Path, "read_text", return_value="{"),
            patch.object(builder, "_read_routing_rules_url") as routing,
            patch.object(builder, "_download_subscription") as download,
            patch.object(builder, "_emit_aliases_yaml") as emit,
            patch.object(builder, "_write_if_changed") as write,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as caught:
                builder.main()
        self.assertEqual(caught.exception.code, 3)
        for operation in (routing, download, emit, write):
            operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
