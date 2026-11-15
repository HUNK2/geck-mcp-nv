"""Release plan Task 4.2: every Markdown file in the repository links only to files that exist, and holds no control
character other than a newline (a TAB or a form feed inside a path broke a command once)."""
import re
import subprocess
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r'\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
FENCE = re.compile(r'^\s*(```|~~~)')


def markdown_files():
    out = subprocess.run(['git', 'ls-files', '*.md'], cwd=str(ROOT), capture_output=True, text=True, check=True)
    return [ROOT / line for line in out.stdout.splitlines() if line and (ROOT / line).is_file()]


def links(text):
    """(line number, target) of each relative link outside code blocks and inline code."""
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        for match in LINK.finditer(re.sub(r'`[^`]*`', '', line)):
            target = match.group(1)
            if re.match(r'^[a-z][a-z0-9+.-]*:', target, re.I) or target.startswith('#'):
                continue                      # a URL, mailto: or an anchor in the same file
            yield number, target


class MarkdownTests(unittest.TestCase):
    def test_relative_links_point_at_files_that_exist(self):
        broken = []
        for path in markdown_files():
            for number, target in links(path.read_text(encoding='utf-8', errors='replace')):
                file = unquote(target.split('#', 1)[0])
                if file and not (path.parent / file).exists():
                    broken.append('%s:%d -> %s' % (path.relative_to(ROOT).as_posix(), number, target))
        self.assertEqual(broken, [])

    def test_no_control_characters(self):
        bad = []
        for path in markdown_files():
            text = path.read_text(encoding='utf-8', errors='replace').replace('\r\n', '\n')
            for number, line in enumerate(text.split('\n'), 1):
                found = sorted({'0x%02X' % ord(c) for c in line if (ord(c) < 32 and c != '\t') or ord(c) == 0x7F})
                if '\t' in line and not line.startswith('\t'):
                    found.append('TAB')
                if found:
                    bad.append('%s:%d %s' % (path.relative_to(ROOT).as_posix(), number, ','.join(found)))
        self.assertEqual(bad, [])


if __name__ == '__main__':
    unittest.main()
