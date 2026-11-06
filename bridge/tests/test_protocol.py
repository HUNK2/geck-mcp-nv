import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from geck_mcp.server import make_server
from geck_mcp.fnv_tools import FNV_TOOLS
from geck_mcp.fnv_live_tools import FNV_LIVE_TOOLS
import test_service


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_service.ServiceTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        self.fixture.tearDown()

    async def test_discovery_schemas_errors_resources_and_prompt(self):
        async with Client(make_server(self.fixture.p, self.fixture.service)) as client:
            listing = await client.list_tools()
            tools = listing.tools if hasattr(listing, 'tools') else listing
            by_name = {tool.name: tool for tool in tools}
            self.assertEqual(len([n for n in by_name if n.startswith('geck_')]), 44)
            self.assertEqual(set(n for n in by_name if n.startswith('fnv_')), set(FNV_TOOLS) | set(FNV_LIVE_TOOLS))
            for name in FNV_TOOLS:
                self.assertTrue(by_name[name].annotations.read_only_hint, name)
            for name, (kind, _) in FNV_LIVE_TOOLS.items():
                self.assertEqual(by_name[name].annotations.read_only_hint, kind == 'read', name)
                self.assertEqual(by_name[name].annotations.destructive_hint, kind == 'act', name)
            result = await client.call_tool('fnv_func_find', {'query': 'anything'})
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content['error']['code'], 'ENGINE_MAP_UNAVAILABLE')
            self.assertIn('geck_show_windows', by_name)
            self.assertIn('geck_runtime_snapshot', by_name)
            self.assertIn('geck_runtime_compare', by_name)
            for name in ('geck_actor_photo', 'geck_render_capture', 'geck_image_cutout'):
                self.assertIn(name, by_name)
            self.assertIn('RESUMABLE', by_name['geck_actor_photo'].description)
            self.assertIsNotNone(by_name['geck_status'].output_schema)
            result = await client.call_tool('geck_status', {})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content['data']['sessionId'], '100:123')
            result = await client.call_tool('geck_records_find', {'session_id':'100:123','limit':501})
            self.assertTrue(result.is_error)
            result = await client.call_tool('geck_project_attach', {'session_id':'wrong-session'})
            self.assertTrue(result.is_error)
            resource = await client.read_resource('geck://project/manifest')
            self.assertIn('Fixture.esp', str(resource))
            prompt = await client.get_prompt('update_static_model', {'editor_id':'FixtureStatic','model_path':'fixture\\new.nif'})
            self.assertIn('whole-plugin', str(prompt))

    async def test_session_argument_is_optional_everywhere(self):
        # 5 Oct 2026: the Claude desktop device proxy dropped tool arguments named session_id, so every
        # session-taking tool failed with "session_id Field required". None of them may require it now.
        async with Client(make_server(self.fixture.p, self.fixture.service)) as client:
            listing = await client.list_tools()
            tools = listing.tools if hasattr(listing, 'tools') else listing
            for tool in tools:
                schema = tool.input_schema if hasattr(tool, 'input_schema') else tool.inputSchema
                required = schema.get('required', [])
                self.assertNotIn('session_id', required, tool.name)
                self.assertNotIn('editor_session', required, tool.name)
                if 'session_id' in schema.get('properties', {}):
                    self.assertIn('editor_session', schema['properties'], tool.name)
            # What the proxy forwarded: only the other arguments.
            for args in ({}, {'editor_session': '100:123'}, {'session_id': '100:123'},
                         {'editor_session': '100:123', 'session_id': '100:123'}):
                result = await client.call_tool('geck_project_attach', args)
                self.assertFalse(result.is_error, (args, result))
            result = await client.call_tool('geck_project_attach', {'editor_session': '100:123', 'session_id': '9:9'})
            self.assertTrue(result.is_error)
            result = await client.call_tool('geck_project_attach', {'editor_session': 'wrong-session'})
            self.assertTrue(result.is_error)
            result = await client.call_tool('geck_show_windows', {})
            self.assertFalse(result.is_error, result)
            result = await client.call_tool('geck_record_read', {'editor_id': 'FixtureStatic', 'source': 'editor'})
            self.assertFalse(result.is_error, result)

    async def test_real_stdio_process(self):
        path = Path(self.fixture.tmp.name)/'project.json'
        path.write_text(self.fixture.p.model_dump_json())
        root = Path(__file__).resolve().parents[1]
        parameters = StdioServerParameters(command=sys.executable,
                        args=[str(root/'run_mcp.py'),'--project',str(path)], cwd=str(root))
        async with Client(parameters) as client:
            result = await client.call_tool('geck_capabilities', {})
            self.assertFalse(result.is_error)
            self.assertIn('STAT', str(result.structured_content))
            listing = await client.list_tools()
            tools = listing.tools if hasattr(listing, 'tools') else listing
            self.assertEqual(len(tools), 44 + len(FNV_TOOLS) + len(FNV_LIVE_TOOLS))
            result = await client.call_tool('geck_console_readiness', {'expected_plugin_hash': '0'*64})
            self.assertFalse(result.is_error, result)
            self.assertFalse(result.structured_content['data']['ready'])
            self.assertIn('not installed', result.structured_content['data']['reason'])

            result = await client.call_tool('geck_plugin_validate', {'expected_models':{'FixtureStatic':'fixture\\old.nif'}})
            self.assertFalse(result.is_error, result)
            self.assertTrue(result.structured_content['data']['persistenceVerified'])
            result = await client.call_tool('geck_runtime_snapshot', {
                'profile':{'plugin':'Fixture.esp','probes':[{'key':'fuel','kind':'quest_variable',
                    'local_form':'000803','variable':'Fuel'}]},'expected_plugin_hash':'0'*64})
            self.assertFalse(result.structured_content['ok'])
            self.assertEqual(result.structured_content['error']['code'],'RUNTIME_COMPANION_UNAVAILABLE')
            result = await client.call_tool('geck_runtime_compare', {
                'profile':{'plugin':'Fixture.esp','probes':[{'key':'fuel','kind':'quest_variable',
                    'local_form':'000803','variable':'Fuel'}]},'expected_plugin_hash':'0'*64,'baseline':{}})
            self.assertFalse(result.structured_content['ok'])
            self.assertEqual(result.structured_content['error']['code'],'RUNTIME_COMPANION_UNAVAILABLE')

    async def test_legacy_initialize_client(self):
        async with Client(make_server(self.fixture.p, self.fixture.service), mode='legacy') as client:
            listing = await client.list_tools()
            tools = listing.tools if hasattr(listing, 'tools') else listing
            self.assertIn('geck_plugin_load', {tool.name for tool in tools})
            result = await client.call_tool('geck_status', {})
            self.assertFalse(result.is_error)
            self.assertTrue(result.structured_content['ok'])


if __name__ == '__main__':
    unittest.main()
