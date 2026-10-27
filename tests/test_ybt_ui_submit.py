from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODULE_PATH = ROOT / "ybt_ui_submit.py"
SPEC = importlib.util.spec_from_file_location("ybt_ui_submit", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
ui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ui)


class UiSubmitParserTests(unittest.TestCase):
    def test_extracts_display_name_after_uid(self) -> None:
        text = "\u3010Uid123456\u3011ExampleUser"
        self.assertEqual(ui._extract_browser_username(text), "ExampleUser")

    def test_rejects_unrecognised_user_link_text(self) -> None:
        self.assertEqual(ui._extract_browser_username("ExampleUser"), "")

    def test_parses_successful_submit_response(self) -> None:
        self.assertEqual(ui._parse_ui_submit_response("\ufeffok!143361388\r\n"), 143361388)

    def test_rejects_empty_or_error_submit_response(self) -> None:
        self.assertIsNone(ui._parse_ui_submit_response("\ufeff"))
        self.assertIsNone(ui._parse_ui_submit_response("Error!"))
        self.assertIsNone(ui._parse_ui_submit_response("ok!"))


if __name__ == "__main__":
    unittest.main()
