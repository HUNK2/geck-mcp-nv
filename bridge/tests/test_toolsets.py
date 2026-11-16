"""TOOL-MERGE step 1: toolsets chosen at server start.

The project key `toolsets` (or `run_mcp.py --toolsets game,naming`) picks which families are registered: game
(fnv_*), editor (geck_*), naming (fnv_name_*, fnv_probe, fnv_atlas) and legacy (the old runtime and console tools).
Default: game, editor and naming; legacy stays off unless named (or legacy_tools / --legacy). fnv_guide is always
registered, and its find still names a hidden tool with the toolset that turns it on."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from geck_mcp import server as server_module  # noqa: E402
from geck_mcp.config import Project  # noqa: E402
from geck_mcp.fnv_guide import GuideTools, tool_table  # noqa: E402
from geck_mcp.server import LEGACY_TOOLS, make_server  # noqa: E402
from geck_mcp.toolsets import ALWAYS, DEFAULT_TOOLSETS, toolset_of  # noqa: E402
import test_service  # noqa: E402


class ToolsetOfTests(unittest.TestCase):
    def test_families(self):
        self.assertEqual(toolset_of('fnv_ref'), 'game')
        self.assertEqual(toolset_of('fnv_func'), 'game')
        self.assertEqual(toolset_of('geck_plugin_save'), 'editor')
        self.assertEqual(toolset_of('fnv_name_dossier'), 'naming')
        self.assertEqual(toolset_of('fnv_probe'), 'naming')
        self.assertEqual(toolset_of('fnv_atlas'), 'naming')
        for name in LEGACY_TOOLS:
            self.assertEqual(toolset_of(name), 'legacy', name)

    def test_project_key(self):
        p = test_service.ServiceTests()
        p.setUp()
        self.addCleanup(p.tearDown)
        self.assertEqual(p.p.active_toolsets(), set(DEFAULT_TOOLSETS))
        self.assertNotIn('legacy', DEFAULT_TOOLSETS)
        self.assertEqual(p.p.model_copy(update={'toolsets': ['game']}).active_toolsets(), {'game'})
        self.assertEqual(p.p.model_copy(update={'toolsets': ['game'], 'legacy_tools': True}).active_toolsets(),
                         {'game', 'legacy'})
        data = {**p.p.model_dump(mode='json'), 'toolsets': ['game', 'nonsense']}
        with self.assertRaises(ValidationError):
            Project.model_validate(data)


class ToolsetServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_service.ServiceTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        self.fixture.tearDown()

    async def names(self, **update):
        project = self.fixture.p.model_copy(update=update)
        async with Client(make_server(project, self.fixture.service)) as client:
            listing = await client.list_tools()
        return {t.name for t in (listing.tools if hasattr(listing, 'tools') else listing)}

    async def test_default_is_every_family_but_legacy(self):
        names = await self.names()
        self.assertEqual({toolset_of(n) for n in names}, set(DEFAULT_TOOLSETS))

    async def test_game_only(self):
        names = await self.names(toolsets=['game'])
        self.assertEqual({toolset_of(n) for n in names - ALWAYS}, {'game'})
        self.assertIn('fnv_ref', names)
        self.assertNotIn('fnv_probe', names)
        self.assertFalse(any(n.startswith('geck_') for n in names))

    async def test_editor_only_keeps_the_guide(self):
        names = await self.names(toolsets=['editor'])
        self.assertEqual({toolset_of(n) for n in names - ALWAYS}, {'editor'})
        self.assertIn('fnv_guide', names)

    async def test_each_set_adds_its_tools(self):
        every = await self.names(toolsets=['game', 'editor', 'naming', 'legacy'])
        parts = set()
        for name in ('game', 'editor', 'naming', 'legacy'):
            parts |= await self.names(toolsets=[name])
        self.assertEqual(parts, every)
        self.assertEqual(sorted(set(LEGACY_TOOLS) - every), [])

    def test_command_line_flag(self):
        seen = {}

        def fake_make(project, service):
            seen['sets'] = project.active_toolsets()
            return mock.Mock()
        with mock.patch.object(server_module.Project, 'load', return_value=self.fixture.p), \
                mock.patch.object(server_module, 'Service', return_value=mock.Mock()), \
                mock.patch.object(server_module, 'make_server', side_effect=fake_make):
            server_module.main(['--project', 'x.json', '--toolsets', 'game,naming'])
            self.assertEqual(seen['sets'], {'game', 'naming'})
            server_module.main(['--project', 'x.json', '--toolsets', 'game', '--legacy'])
            self.assertEqual(seen['sets'], {'game', 'legacy'})
            server_module.main(['--project', 'x.json'])
            self.assertEqual(seen['sets'], set(DEFAULT_TOOLSETS))
            with self.assertRaises(SystemExit):
                server_module.main(['--project', 'x.json', '--toolsets', 'game,bogus'])


class GuideNamesHiddenToolsTests(unittest.TestCase):
    def test_every_row_has_a_toolset(self):
        table = tool_table()
        self.assertEqual([n for n, t in table.items() if t.get('toolset') != toolset_of(n)], [])

    def test_hidden_tools_say_how_to_turn_them_on(self):
        table = tool_table({'game'})
        self.assertIn('toolsets', table['fnv_probe']['description'])
        self.assertIn('naming', table['fnv_probe']['description'])
        self.assertIn('geck_plugin_save', table)                       # editor off: find still knows it
        self.assertIn('editor', table['geck_plugin_save']['description'])
        self.assertNotIn('toolsets', table['fnv_ref']['description'])
        rows = GuideTools(toolsets={'game'}).find('probe install functions', limit=50)['rows']
        hit = next(r for r in rows if r['name'] == 'fnv_probe')
        self.assertIn('naming', hit['description'])
        self.assertEqual(hit['toolset'], 'naming')

    def test_editor_on_lists_no_geck_rows(self):
        self.assertNotIn('geck_plugin_save', tool_table())


if __name__ == '__main__':
    unittest.main()
