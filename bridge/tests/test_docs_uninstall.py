"""Release plan Phase 7 (M10): the setup guide says how to remove everything the setup and the tools add."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp import fnv_link, fnv_plugin_build  # noqa: E402

DOC = Path(__file__).resolve().parents[1] / 'docs' / 'FNV-MCP-SETUP.md'


class UninstallSectionTests(unittest.TestCase):
    def section(self):
        text = DOC.read_text(encoding='utf-8')
        match = re.search(r'^## Uninstall\n(.*?)(?=^## |\Z)', text, re.S | re.M)
        self.assertIsNotNone(match, 'no "## Uninstall" section in FNV-MCP-SETUP.md')
        return match.group(1)

    def test_names_every_place(self):
        section = self.section()
        for needle in (fnv_link.MOD_NAME, 'modlist.txt', fnv_plugin_build.mod_name('').strip(), 'plugins.txt',
                       'fnv-mcp-install.json', 'gameFilesAdded', 'nvse_loader.exe',
                       r'%LOCALAPPDATA%\fnvmcp', r'%TEMP%\geck-mcp', 'bridge\\state', 'claude mcp remove'):
            self.assertIn(needle, section, needle)


if __name__ == '__main__':
    unittest.main()
