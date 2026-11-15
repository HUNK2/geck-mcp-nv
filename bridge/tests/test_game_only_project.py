"""Release plan Task 4.3 (audit H8): a project file for the game alone, with no GECK settings, is valid.

- Project.load accepts projects/example-fnv.json (no plugin, helper, records or spec).
- A geck_* tool on such a project answers NOT_CONFIGURED and names the missing keys; the fnv_* tools work.
- An invalid project file ends run_mcp.py with one line on stderr and exit code 2, no traceback.
- fnv_plugin_build plans prologues from fnv.clean_exe, then FNV_CLEAN_EXE, then ~/.fnvdecomp/FalloutNV.exe."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client  # noqa: E402

from geck_mcp import fnv_plugin_build  # noqa: E402
from geck_mcp.config import Project  # noqa: E402
from geck_mcp.server import make_server  # noqa: E402

BRIDGE = Path(__file__).resolve().parents[1]
EXAMPLE = BRIDGE / 'projects' / 'example-fnv.json'


def game_only(folder):
    path = Path(folder) / 'game.json'
    path.write_text(json.dumps({'project_id': 'gameonly', 'game_root': str(folder), 'state_dir': 'state',
                                'fnv': {'engine_control': 'read'}}), encoding='utf-8')
    return Project.load(path)


class LoadTests(unittest.TestCase):
    def test_the_example_loads(self):
        project = Project.load(EXAMPLE)
        self.assertIsNone(project.plugin)
        self.assertIsNone(project.helper)
        self.assertEqual(project.fnv.engine_control, 'read')
        self.assertEqual(project.launcher, 'native')

    def test_a_plugin_still_needs_records_or_a_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'p.json'
            path.write_text(json.dumps({'project_id': 'p', 'game_root': tmp, 'state_dir': 's', 'helper': 'h.exe',
                                        'plugin': 'A.esp'}), encoding='utf-8')
            with self.assertRaises(ValueError):
                Project.load(path)


class NotConfiguredTests(unittest.IsolatedAsyncioTestCase):
    async def test_geck_tools_name_the_missing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = game_only(tmp)
            async with Client(make_server(project)) as client:
                for tool, args in (('geck_status', {}), ('geck_plugin_build', {'request_key': 'k' * 16}),
                                   ('geck_plugin_inspect', {})):
                    with self.subTest(tool=tool):
                        result = await client.call_tool(tool, args)
                        error = (result.structured_content or {}).get('error') or {}
                        self.assertEqual(error.get('code'), 'NOT_CONFIGURED')
                        for key in ('plugin', 'helper', 'spec'):
                            self.assertIn(key, error.get('message', ''))

    async def test_fnv_tools_and_the_capabilities_still_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = game_only(tmp)
            async with Client(make_server(project)) as client:
                result = await client.call_tool('fnv_guide', {'topic': 'crash'})
                self.assertNotEqual(((result.structured_content or {}).get('error') or {}).get('code'),
                                    'NOT_CONFIGURED')
                result = await client.call_tool('geck_capabilities', {})
                self.assertFalse(result.is_error)


class ReadmeCountTests(unittest.TestCase):
    """Task 4.2: the README's tool counts are the server's, not typed numbers that drift."""

    def test_the_readme_counts_match_the_server(self):
        import re
        from geck_mcp.server import LEGACY_TOOLS, server_capabilities
        text = (BRIDGE.parent / 'README.md').read_text(encoding='utf-8')
        block = re.search(r'<!-- counts -->(.*?)<!-- /counts -->', text, re.S).group(1)
        numbers = [int(n) for n in re.findall(r'\d+', block)]
        caps = server_capabilities(make_server(Project.load(EXAMPLE)))
        self.assertEqual(numbers, [caps['fnv']['toolCount'], caps['toolCount'], caps['allTools'], len(LEGACY_TOOLS)])


class InvalidProjectTests(unittest.TestCase):
    def run_mcp(self, project, *extra):
        env = {k: v for k, v in os.environ.items() if k != 'GECK_MCP_RELOAD'}
        env['GECK_SUPERVISOR_LOG'] = ''
        return subprocess.run([sys.executable, 'run_mcp.py', '--project', str(project), *extra], cwd=str(BRIDGE),
                              env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)

    def check(self, out):
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertNotIn('Traceback', out.stderr)
        self.assertEqual(len(out.stderr.strip().splitlines()), 1, out.stderr)

    def test_a_bad_key_exits_with_code_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.json'
            path.write_text(json.dumps({'project_id': 'bad', 'game_root': tmp, 'state_dir': 's', 'nonsense': 1}),
                            encoding='utf-8')
            for extra in ((), ('--no-reload',)):
                with self.subTest(extra=extra):
                    out = self.run_mcp(path, *extra)
                    self.check(out)
                    self.assertIn('nonsense', out.stderr)

    def test_a_missing_file_and_broken_json_exit_with_code_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.check(self.run_mcp(Path(tmp) / 'missing.json'))
            broken = Path(tmp) / 'broken.json'
            broken.write_text('{"project_id": ', encoding='utf-8')
            self.check(self.run_mcp(broken))


class CleanExeTests(unittest.TestCase):
    def tools(self, fnv):
        live = SimpleNamespace(project=SimpleNamespace(fnv=fnv, state_dir='.'))
        return fnv_plugin_build.PluginTools(live)

    def test_the_order_is_project_then_environment_then_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            own, env = Path(tmp) / 'own.exe', Path(tmp) / 'env.exe'
            own.write_bytes(b'MZ')
            env.write_bytes(b'MZ')
            with mock.patch.dict(os.environ, {'FNV_CLEAN_EXE': str(env)}):
                self.assertEqual(self.tools(SimpleNamespace(clean_exe=own))._image_path(), own)
                self.assertEqual(self.tools(SimpleNamespace(clean_exe=None))._image_path(), env)
                self.assertEqual(self.tools(None)._image_path(), env)
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(self.tools(None)._image_path(), fnv_plugin_build.IMAGE)


if __name__ == '__main__':
    unittest.main()
