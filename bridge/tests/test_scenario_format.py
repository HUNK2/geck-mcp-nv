"""Release plan Phase 7 (M5): scenario files are a YAML subset. Each rule docs/FNV-LIVE-TOOLS.md states under
"Scenario file format" is checked here, so the text and the parser cannot drift apart."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp import fnv_scenario  # noqa: E402
from geck_mcp.fnv_scenario import ScenarioError, parse_yaml  # noqa: E402

DOC = Path(__file__).resolve().parents[1] / 'docs' / 'FNV-LIVE-TOOLS.md'


class ScenarioFormatTests(unittest.TestCase):
    def test_the_documented_section_exists(self):
        text = DOC.read_text(encoding='utf-8')
        self.assertIn('### Scenario file format', text)

    def test_mappings_lists_and_flow(self):
        data = parse_yaml('name: x\nsteps:\n  - run: a\n  - {wait: 1}\n  - [1, 2]\nlist:\n- a\n- b\n')
        self.assertEqual(data, {'name': 'x', 'steps': [{'run': 'a'}, {'wait': 1}, [1, 2]], 'list': ['a', 'b']})

    def test_scalars(self):
        data = parse_yaml("a: 00138562\nb: 12\nc: 1.5\nd: 1E5\ne: 0x1F\nf: yes\ng: Off\nh: ~\ni: 'it''s'\n"
                          'j: "tab\\tx"\n')
        self.assertEqual(data, {'a': '00138562', 'b': 12, 'c': 1.5, 'd': '1E5', 'e': '0x1F', 'f': True,
                                'g': False, 'h': None, 'i': "it's", 'j': 'tab\tx'})

    def test_comments(self):
        data = parse_yaml("a: x # note\nb: x#y\nc: 'x # y'\n# whole line\n")
        self.assertEqual(data, {'a': 'x', 'b': 'x#y', 'c': 'x # y'})

    def test_block_scalars_keep_lines_only(self):
        data = parse_yaml('a: |\n  one\n    indented\n\n  three # cut\nb: >\n  x\n  y\n')
        self.assertEqual(data, {'a': 'one\nindented\nthree', 'b': 'x y'})

    def test_json_whole_file(self):
        self.assertEqual(parse_yaml('{"name": "x", "steps": []}'), {'name': 'x', 'steps': []})

    def test_refused(self):
        for text in ('a:\n\tb: 1\n', 'a: "C:\\Mods"\n', 'a: [1, 2\n', 'a: |-\n  x\n'):
            with self.assertRaises(ScenarioError, msg=text):
                parse_yaml(text)

    def test_no_anchors(self):
        self.assertEqual(parse_yaml('a: &x 1\nb: *x\nc: !!str 1\n'), {'a': '&x 1', 'b': '*x', 'c': '!!str 1'})


if __name__ == '__main__':
    unittest.main()
