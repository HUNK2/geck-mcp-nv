"""Decision D4 (release plan Task 3.4, TOOL-MERGE tier 3): the old runtime and console tools sit in a `legacy`
toolset that is off by default. Turning it on (project key legacy_tools, or `--legacy`) brings all 15 back."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client  # noqa: E402

from geck_mcp import server as server_module  # noqa: E402
from geck_mcp.fnv_guide import GuideTools, tool_table  # noqa: E402
from geck_mcp.server import LEGACY_TOOLS, make_server  # noqa: E402
import test_service  # noqa: E402


class LegacyToolsetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_service.ServiceTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        self.fixture.tearDown()

    async def names(self, project):
        async with Client(make_server(project, self.fixture.service)) as client:
            listing = await client.list_tools()
        return {t.name for t in (listing.tools if hasattr(listing, 'tools') else listing)}

    def test_the_table_is_the_fifteen_tier_3_tools(self):
        self.assertEqual(len(LEGACY_TOOLS), 15)
        self.assertEqual(sum(n.startswith('geck_runtime_') for n in LEGACY_TOOLS), 9)
        self.assertEqual(sum(n.startswith('geck_console_') for n in LEGACY_TOOLS), 6)

    async def test_off_by_default(self):
        names = await self.names(self.fixture.p)
        self.assertEqual(sorted(set(LEGACY_TOOLS) & names), [])
        self.assertIn('geck_plugin_build', names)          # the plugin builder stays (D4)

    async def test_a_hidden_tool_answers_unknown_tool(self):
        async with Client(make_server(self.fixture.p, self.fixture.service)) as client:
            result = await client.call_tool('geck_console_readiness', {'expected_plugin_hash': '0' * 64})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content['error']['code'], 'UNKNOWN_TOOL')

    async def test_project_key_turns_it_on(self):
        names = await self.names(self.fixture.p.model_copy(update={'legacy_tools': True}))
        self.assertEqual(sorted(set(LEGACY_TOOLS) - names), [])

    def test_command_line_flag_turns_it_on(self):
        seen = {}

        def fake_make(project, service):
            seen['legacy'] = project.legacy_tools
            return mock.Mock()
        with mock.patch.object(server_module.Project, 'load', return_value=self.fixture.p), \
                mock.patch.object(server_module, 'Service', return_value=mock.Mock()), \
                mock.patch.object(server_module, 'make_server', side_effect=fake_make):
            server_module.main(['--project', 'x.json', '--legacy'])
            self.assertTrue(seen['legacy'])
            server_module.main(['--project', 'x.json'])
            self.assertFalse(seen['legacy'])

    def test_guide_find_names_the_toolset(self):
        table = tool_table()
        for name in LEGACY_TOOLS:
            self.assertEqual(table[name].get('toolset'), 'legacy', name)
        rows = GuideTools().find('console command companion', limit=50)['rows']
        hit = next(r for r in rows if r['name'] == 'geck_console_execute')
        self.assertIn('legacy', hit['description'])
        self.assertIn('legacy_tools', hit['description'])


if __name__ == '__main__':
    unittest.main()
