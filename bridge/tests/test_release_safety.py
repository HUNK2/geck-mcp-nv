"""Release plan Phase 1 (audit B1, B2, H5): the safety fixes, through the real in-process server.

The server runs with fnv.engine_control = "read" and no pipe (test_stab_gate.GateFixture)."""
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client  # noqa: E402

from geck_mcp.server import make_server  # noqa: E402
from test_stab_gate import GateFixture  # noqa: E402

BRIDGE = Path(__file__).resolve().parents[1]


def add_dev_plugin(case):
    """Put CoolWorld.esp into the temporary dev profile, so tools that need a load-order plugin get past that."""
    mo2 = Path(case.project.fnv.mo2_root)
    mod = mo2 / 'mods' / 'TestMod'
    mod.mkdir(parents=True)
    shutil.copy2(BRIDGE / 'tests' / 'fixtures' / 'plugins' / 'CoolWorld.esp', mod / 'CoolWorld.esp')
    profile = mo2 / 'profiles' / 'Dev'
    (profile / 'modlist.txt').write_text('+TestMod\n', encoding='utf-8')
    (profile / 'plugins.txt').write_text('*CoolWorld.esp\n', encoding='utf-8')
    (profile / 'loadorder.txt').write_text('CoolWorld.esp\n', encoding='utf-8')


class PackageTests(GateFixture, unittest.IsolatedAsyncioTestCase):
    """B1: fnv_author package can no longer delete or leave its folder."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        add_dev_plugin(self)
        self.state = Path(self.project.state_dir)

    async def package(self, client, **extra):
        code, result = await self.call(client, 'fnv_author', {'action': 'package', 'plugin': 'CoolWorld.esp',
                                                              **extra})
        sc = result.structured_content or {}
        return code, (sc.get('data') or sc)

    async def test_package_refuses_a_name_outside_packages(self):
        decoy = self.state / 'decoy'
        decoy.mkdir(parents=True)
        (decoy / 'keep.txt').write_text('mine', encoding='utf-8')
        async with Client(make_server(self.project, self.fixture.service)) as client:
            for name in ['..\\..\\decoy', str(decoy), 'a/b', 'x.']:
                with self.subTest(name=name):
                    code, _ = await self.package(client, name=name)
                    self.assertEqual(code, 'INVALID_ARGUMENT')
                    self.assertTrue((decoy / 'keep.txt').exists())
        self.assertEqual(sorted(p.name for p in decoy.iterdir()), ['keep.txt'])

    async def test_package_never_deletes_an_existing_folder(self):
        async with Client(make_server(self.project, self.fixture.service)) as client:
            code, first = await self.package(client, name='rel')
            self.assertIsNone(code, first)
            mine = Path(first['folder']) / 'mine.txt'
            mine.write_text('mine', encoding='utf-8')
            code, second = await self.package(client, name='rel')
            self.assertIsNone(code, second)
        self.assertTrue(mine.exists())
        self.assertNotEqual(Path(first['folder']), Path(second['folder']))
        self.assertTrue(Path(second['archive']).exists())

    async def test_two_packages_in_one_second_get_two_folders(self):
        folders = set()
        with mock.patch('time.strftime', return_value='20261115-120000'):
            async with Client(make_server(self.project, self.fixture.service)) as client:
                for _ in range(3):
                    code, out = await self.package(client, name='rel')
                    self.assertIsNone(code, out)
                    folders.add(out['folder'])
        self.assertEqual(len(folders), 3)

    async def test_default_name_is_the_plugin_stem(self):
        async with Client(make_server(self.project, self.fixture.service)) as client:
            code, out = await self.package(client)
        self.assertIsNone(code, out)
        self.assertEqual(Path(out['folder']), (self.state / 'fnv' / 'packages' / 'CoolWorld').resolve())
        self.assertEqual(Path(out['archive']).name, 'CoolWorld.zip')

    async def test_dotted_name_keeps_its_archive_name(self):
        async with Client(make_server(self.project, self.fixture.service)) as client:
            code, out = await self.package(client, name='my.mod')
        self.assertIsNone(code, out)
        self.assertEqual(Path(out['archive']).name, 'my.mod.zip')


class CompilerPathTests(GateFixture, unittest.IsolatedAsyncioTestCase):
    """B2 (Task 1.3): no tool runs a program whose path an agent passes; the compiler comes from the project file."""

    async def test_no_tool_takes_a_compiler_path(self):
        async with Client(make_server(self.project, self.fixture.service)) as client:
            listing = await client.list_tools()
        tools = listing.tools if hasattr(listing, 'tools') else listing
        found = sorted((t.name, prop) for t in tools if t.name.startswith(('geck_', 'fnv_'))
                       for prop in (t.input_schema or {}).get('properties', {}) if prop in ('zig', 'zig_path'))
        self.assertEqual(found, [])

    def test_setup_and_preflight_use_the_project_zig(self):
        from geck_mcp import fnv_link
        missing = fnv_link.LinkError('ZIG_MISSING', 'none set')
        with mock.patch.object(fnv_link, 'find_zig', side_effect=missing) as find:
            built = self.fixture.service.console_setup('build', self.fixture.hash, 'zig-missing-1')
            self.assertEqual(built['error']['code'], 'ZIG_MISSING', built)
            self.assertTrue(find.called)
        from geck_mcp import runtime_companion
        with mock.patch.object(runtime_companion, 'preflight', return_value={}) as preflight:
            with mock.patch.object(fnv_link, 'find_zig', side_effect=missing):
                self.fixture.service.runtime_preflight({})
            self.assertIsNone(preflight.call_args.args[2])
            with mock.patch.object(fnv_link, 'find_zig', return_value=Path('C:/project/zig.exe')):
                self.fixture.service.runtime_preflight({})
            self.assertEqual(preflight.call_args.args[2], Path('C:/project/zig.exe'))


def filetime(unix):
    """A Windows FILETIME integer (100 ns since 1601) for a Unix time, as game_processes() reports start times."""
    return int((unix + 11644473600) * 10_000_000)


class QuitOwnershipTests(GateFixture, unittest.IsolatedAsyncioTestCase):
    """B2 (Task 1.4): under read, fnv_quit and fnv_restart stop only the game fnv_launch started."""
    PID = 4242

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.launched = time.time() - 600
        self.steps = []
        patches = [mock.patch('geck_mcp.fnv_game.terminate', side_effect=lambda pid: self.steps.append('terminate')),
                   mock.patch('geck_mcp.fnv_game.post_close',
                              side_effect=lambda pid: self.steps.append('post_close') or 1),
                   mock.patch('geck_mcp.fnv_game.Game._exited', return_value=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def running(self, started):
        patch = mock.patch('geck_mcp.fnv_game.game_processes', return_value=[(self.PID, filetime(started))])
        patch.start()
        self.addCleanup(patch.stop)

    def session(self, pid):
        folder = Path(self.project.state_dir) / 'fnv'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'session.json').write_text(json.dumps({'pid': pid, 'launchedAt': self.launched, 'ready': True}),
                                             encoding='utf-8')

    async def quit(self, tool='fnv_quit', args=None):
        async with Client(make_server(self.project, self.fixture.service)) as client:
            return await self.call(client, tool, args or {'force': True, 'timeout_seconds': 5})

    async def test_quit_refuses_a_game_it_did_not_launch(self):
        self.session(None)
        self.running(self.launched + 2)
        code, result = await self.quit()
        self.assertEqual(code, 'NOT_OUR_GAME')
        self.assertIn('not started by fnv_launch', result.structured_content['error']['message'])
        self.assertEqual(self.steps, [])

    async def test_restart_refuses_a_game_it_did_not_launch(self):
        self.session(1)
        self.running(self.launched + 2)
        code, _ = await self.quit('fnv_restart', {'start': 'menu', 'timeout_seconds': 20})
        self.assertEqual(code, 'NOT_OUR_GAME')
        self.assertEqual(self.steps, [])

    async def test_quit_refuses_a_reused_pid(self):
        self.session(self.PID)
        self.running(self.launched - 3600)
        code, _ = await self.quit()
        self.assertEqual(code, 'NOT_OUR_GAME')
        self.assertEqual(self.steps, [])

    async def test_quit_stops_its_own_game(self):
        self.session(self.PID)
        self.running(self.launched + 2)
        code, result = await self.quit()
        self.assertIsNone(code, result.structured_content)
        self.assertEqual(result.structured_content['data']['wasRunning'], True)

    async def test_full_may_quit_any_game(self):
        self.project = self.project.model_copy(
            update={'fnv': self.project.fnv.model_copy(update={'engine_control': 'full'})})
        self.session(None)
        self.running(self.launched + 2)
        code, result = await self.quit()
        self.assertIsNone(code, result.structured_content)
        self.assertEqual(result.structured_content['data']['wasRunning'], True)

    async def test_status_still_answers_for_a_foreign_game(self):
        self.session(None)
        self.running(self.launched + 2)
        async with Client(make_server(self.project, self.fixture.service)) as client:
            code, result = await self.call(client, 'fnv_status', {})
        self.assertIsNone(code, result.structured_content)
        self.assertTrue(result.structured_content['ok'])


UNC = '\\\\fnvmcp-test-host\\share\\x'


class PathArgumentTests(GateFixture, unittest.IsolatedAsyncioTestCase):
    maxDiff = None
    """H5 (Task 1.5): every path and name argument of the plan's table goes through safe_paths. The level is "full"
    here so that the gate does not hide the check (the check comes before any game or pipe call)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.project = self.project.model_copy(
            update={'fnv': self.project.fnv.model_copy(update={'engine_control': 'full'})})
        self.root = Path(self.fixture.tmp.name)
        self.state = Path(self.project.state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        # a file outside every root: the temp folder's parent is not a root, so make a sibling folder of the test
        self.outside_dir = Path(tempfile.mkdtemp(prefix='fnvmcp-outside-'))
        self.addCleanup(shutil.rmtree, self.outside_dir, True)
        self.outside = self.outside_dir / 'secret.log'
        self.outside.write_text('Exception: secret\n', encoding='utf-8')
        plugin = self.state / 'Built.esp'
        shutil.copy2(BRIDGE / 'tests' / 'fixtures' / 'plugins' / 'CoolWorld.esp', plugin)
        self.plugin = plugin
        self.cases = [
            ('fnv_profile', {'action': 'sample', 'name': '..\\..\\out'}),
            ('fnv_photo', {'action': 'compare', 'baseline': UNC + '.png'}),
            ('fnv_photo', {'action': 'compare', 'baseline': str(self.outside)}),
            ('fnv_install', {'plugin_path': str(plugin), 'mod_name': '..'}),
            ('fnv_install', {'plugin_path': str(plugin), 'mod_name': ' . '}),
            ('fnv_install', {'plugin_path': UNC + '.esp'}),
            ('fnv_profile_set', {'plugins': {'a\nb.esp': True}}),
            ('fnv_profile_set', {'mods': {'..\\..\\x': True}}),
            ('fnv_profile_set', {'mods': {'a/b': False}}),
            ('fnv_crash_latest', {'path': UNC + '.log'}),
            ('fnv_crash_latest', {'path': str(self.outside)}),
            ('fnv_crash_explain', {'path': UNC + '.log'}),
            ('fnv_crash_explain', {'path': str(self.outside)}),
            ('fnv_save_inspect', {'save': UNC + '.fos'}),
            ('fnv_save_inspect', {'save': str(self.outside_dir / 'x.fos')}),
            ('fnv_test_run', {'path': UNC + '.yaml'}),
            ('fnv_test_run', {'path': str(self.outside)}),
            ('fnv_atlas', {'action': 'import_frida', 'path': UNC + '.json'}),
            ('fnv_asset', {'action': 'watch', 'path': UNC}),
            ('fnv_asset', {'action': 'watch', 'path': str(self.outside_dir)}),
            ('fnv_plugin_build', {'action': 'build', 'name': 'x', 'source': '#include "../../secret.h"\nint x;'}),
            ('fnv_plugin_build', {'action': 'build', 'name': 'x', 'source': UNC + '.c'}),
            ('fnv_native', {'action': 'build', 'name': 'x', 'source': '#include "C:/secret.h"\nint x;'}),
            ('fnv_native', {'action': 'build', 'name': 'x', 'source': UNC + '.c'}),
        ]

    async def test_path_arguments_are_checked(self):
        wrong = []
        async with Client(make_server(self.project, self.fixture.service)) as client:
            for tool, args in self.cases:
                code, result = await self.call(client, tool, args)
                if code != 'INVALID_ARGUMENT':
                    text = ''.join(getattr(c, 'text', '') for c in result.content)[:160]
                    wrong.append((tool, args, code, text))
        self.assertEqual(wrong, [])
        self.assertEqual(sorted(p.name for p in self.outside_dir.iterdir()), ['secret.log'])

    async def test_image_cutout_writes_only_inside_the_project(self):
        png = self.outside_dir / 'shot.png'
        from PIL import Image
        Image.new('RGB', (8, 8), (0, 255, 0)).save(png)
        async with Client(make_server(self.project, self.fixture.service)) as client:
            for path in (str(png), UNC + '.png'):
                result = await client.call_tool('geck_image_cutout', {'path': path})
                sc = result.structured_content or {}
                self.assertFalse(sc.get('ok', True), sc)
        self.assertEqual(sorted(p.name for p in self.outside_dir.iterdir()), ['secret.log', 'shot.png'])

    async def test_paths_inside_the_roots_still_work(self):
        async with Client(make_server(self.project, self.fixture.service)) as client:
            log = self.state / 'CrashLogger.log'
            log.write_text('nothing\n', encoding='utf-8')
            code, _ = await self.call(client, 'fnv_crash_latest', {'path': str(log).replace('\\', '/')})
            self.assertNotEqual(code, 'INVALID_ARGUMENT')
            code, _ = await self.call(client, 'fnv_profile_set', {'plugins': {'Cool World.esp': True},
                                                                  'dry_run': True})
            self.assertNotEqual(code, 'INVALID_ARGUMENT')


if __name__ == '__main__':
    unittest.main()
