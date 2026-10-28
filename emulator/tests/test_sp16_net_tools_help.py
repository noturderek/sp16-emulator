"""emulator/net/*.py host tools: every one answers --help with exit 0 and a usage line.

argparse formats each help string with `%`, so a help or description text that contains a bare `%`
(e.g. "2 % fast") raises instead of printing help. This module runs each tool and also scans the
source of each argparse call for help strings that would break that formatting.
"""
import ast
import subprocess
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
NET = TOOLS / "net"

# The string kwargs argparse runs through `%` before printing: a bare `%` in any of them is a crash.
FORMATTED_KWARGS = ("help", "description", "epilog", "usage", "metavar")


def net_tools():
    return sorted(NET.glob("*.py"))


def formatted_strings(tree):
    """Every string constant passed as an argparse-formatted kwarg, with its source line."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg in FORMATTED_KWARGS and isinstance(keyword.value, ast.Constant) \
                    and isinstance(keyword.value.value, str):
                found.append((keyword.arg, keyword.value.value, keyword.value.lineno))
    return found


class HelpExitsZeroTests(unittest.TestCase):
    def test_every_net_tool_prints_help_and_exits_zero(self):
        tools = net_tools()
        self.assertTrue(tools, f"no tools found under {NET}")
        for tool in tools:
            with self.subTest(tool=tool.name):
                result = subprocess.run([sys.executable, str(tool), "--help"], capture_output=True, text=True,
                                        timeout=60)
                self.assertEqual(result.returncode, 0, f"{tool.name} --help failed:\n{result.stderr}")
                self.assertEqual(result.stderr, "", f"{tool.name} --help wrote to stderr")

    def test_tools_with_a_main_print_a_usage_line(self):
        for tool in net_tools():
            source = tool.read_text()
            if '__name__ == "__main__"' not in source:
                continue
            with self.subTest(tool=tool.name):
                result = subprocess.run([sys.executable, str(tool), "--help"], capture_output=True, text=True,
                                        timeout=60)
                self.assertIn("usage:", result.stdout, f"{tool.name} --help printed no usage line")


class HelpStringFormattingTests(unittest.TestCase):
    def test_no_formatted_kwarg_breaks_percent_formatting(self):
        # argparse applies `string % params` to these; params is a dict of every action field, so a dict
        # stands in for it. A bare `%` raises ValueError ("unsupported format character") before printing.
        params = dict.fromkeys(("prog", "default", "type", "choices", "dest", "const", "nargs", "metavar",
                                "required"), "")
        checked = 0
        for tool in net_tools():
            tree = ast.parse(tool.read_text())
            for kwarg, text, lineno in formatted_strings(tree):
                checked += 1
                with self.subTest(tool=tool.name, line=lineno):
                    try:
                        text % params
                    except (ValueError, TypeError) as error:
                        self.fail(f"{tool.name}:{lineno} {kwarg}= string does not survive argparse formatting "
                                  f"({error}); escape a literal percent sign as %%%%: {text!r}")
        self.assertGreater(checked, 0, "found no argparse help strings to check")


if __name__ == "__main__":
    unittest.main()
