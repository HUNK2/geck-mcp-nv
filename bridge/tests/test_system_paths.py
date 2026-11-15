"""Release plan Phase 7 (L5): Windows' own programs are started by their full path under %SystemRoot%, never by a
bare name that the current folder or PATH could answer with another program."""
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

from geck_mcp import fnv_game  # noqa: E402

BARE = re.compile(r"""\[\s*['"](tasklist|powershell|powershell\.exe|cmd|cmd\.exe|taskkill|explorer\.exe)['"]""",
                  re.I)


class SystemPathTests(unittest.TestCase):
    def test_no_bare_names(self):
        found = []
        for folder in ('geck_mcp', 'tools', 'engine_map'):
            for path in (BRIDGE / folder).rglob('*.py'):
                for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                    if BARE.search(line):
                        found.append('%s:%d' % (path.relative_to(BRIDGE), n))
        self.assertEqual(found, [])

    def test_paths_are_under_system_root(self):
        with mock.patch.dict(os.environ, {'SystemRoot': r'D:\Win'}):
            self.assertEqual(str(fnv_game.system_exe('tasklist.exe')), r'D:\Win\System32\tasklist.exe')
            self.assertEqual(str(fnv_game.system_exe('powershell.exe')),
                             r'D:\Win\System32\WindowsPowerShell\v1.0\powershell.exe')


if __name__ == '__main__':
    unittest.main()
