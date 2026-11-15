"""Release plan Task 5.1 (decision D7): one name and one version everywhere, and the environment variables answer
under the one new prefix (FNV_MCP_) and under their old names."""
import os
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geck_mcp  # noqa: E402
from engine_map import generate  # noqa: E402
from geck_mcp import envvars, fnv_link  # noqa: E402

BRIDGE = Path(__file__).resolve().parents[1]
NAME, VERSION = 'fnv-mcp', '0.5.0b1'


class VersionTests(unittest.TestCase):
    def test_one_name_and_one_version(self):
        project = tomllib.loads((BRIDGE / 'pyproject.toml').read_text(encoding='utf-8'))['project']
        self.assertEqual((project['name'], project['version']), (NAME, VERSION))
        self.assertEqual((geck_mcp.NAME, geck_mcp.__version__), (NAME, VERSION))
        self.assertEqual(generate.GENERATOR, '%s %s engine_map 1' % (NAME, VERSION))

    def test_the_server_announces_them(self):
        from geck_mcp.config import Project
        from geck_mcp.server import make_server
        server = make_server(Project.load(BRIDGE / 'projects' / 'example-fnv.json'))
        self.assertEqual(server.name, NAME)
        self.assertEqual(server.version, VERSION)

    def test_the_changelog_starts_with_this_version(self):
        text = (BRIDGE.parent / 'CHANGELOG.md').read_text(encoding='utf-8')
        first = next(line for line in text.splitlines() if line.startswith('## '))
        self.assertIn(VERSION, first)
        self.assertIn('fnv-mcp', first)


class DependencyTests(unittest.TestCase):
    """Task 5.2: the declared dependencies are pinned the same in the lock file, which has the Windows-only pywin32."""

    def test_declared_dependencies_match_the_lock(self):
        lock = {}
        for line in (BRIDGE / 'requirements.lock.txt').read_text(encoding='utf-8').splitlines():
            if '==' in line and not line.startswith('#'):
                name, version = line.split('==')
                lock[name.strip().lower()] = version.strip()
        project = tomllib.loads((BRIDGE / 'pyproject.toml').read_text(encoding='utf-8'))['project']
        declared = project['dependencies'] + project['optional-dependencies']['naming']
        self.assertIn('pywin32', lock)
        for spec in project['dependencies']:
            name, version = spec.split('==')
            self.assertEqual(lock.get(name.lower()), version, spec)
        self.assertEqual({d.split('==')[0] for d in declared} >= {'mcp', 'pydantic', 'anyio', 'capstone'}, True)


class EnvironmentTests(unittest.TestCase):
    def test_the_new_name_wins_and_the_old_one_still_works(self):
        for new, old in envvars.RENAMED.items():
            with self.subTest(new=new):
                with mock.patch.dict(os.environ, {old: 'old'}, clear=True):
                    self.assertEqual(envvars.get(new), 'old')
                with mock.patch.dict(os.environ, {old: 'old', new: 'new'}, clear=True):
                    self.assertEqual(envvars.get(new), 'new')
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertIsNone(envvars.get(new))

    def test_every_new_name_has_the_one_prefix(self):
        self.assertTrue(all(n.startswith('FNV_MCP_') for n in envvars.RENAMED))

    def test_zig_and_pipe_read_the_new_names(self):
        with tempfile_exe() as zig, mock.patch.dict(os.environ, {'FNV_MCP_ZIG': zig}, clear=True):
            self.assertEqual(str(fnv_link.find_zig(None)), zig)
        with mock.patch.dict(os.environ, {'FNV_MCP_PIPE': 'p1'}, clear=True):
            self.assertTrue(fnv_link.EngineLink().pipe.endswith('\\p1'))


class tempfile_exe:
    def __enter__(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / 'zig.exe'
        path.write_bytes(b'MZ')
        return str(path)

    def __exit__(self, *exc):
        self.tmp.cleanup()


if __name__ == '__main__':
    unittest.main()
