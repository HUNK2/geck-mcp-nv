"""Release plan Phase 7 (M13): call every tool once with minimal arguments and check the answer's shape.

The project is the service fixture's: no `fnv` block, so engine control is `read` and every act or control tool
must refuse with a code; no game is started and none is needed. Game process discovery is patched to "none
running", so a game the user has open is never touched. The GECK side runs over the fixture's fake helper.

What is checked for every tool: the call returns (within its time limit), its structured answer is an envelope
(`ok` is a bool), and a failure carries an error code and a message. A bare Python exception that reaches the
agent as FNV_ERROR / BRIDGE_ERROR with no recovery is a bug."""
import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client  # noqa: E402

import test_service  # noqa: E402
from geck_mcp import fnv_game  # noqa: E402
from geck_mcp.server import make_server  # noqa: E402

PER_CALL_SECONDS = 60
UNCODED = ('FNV_ERROR', 'BRIDGE_ERROR', 'INTERNAL_ERROR', 'TOOL_ERROR')
NOT_AN_OPERATION = {'geck_capabilities'}   # describes the server; answers a plain report, not an envelope


def minimal(schema, root=None):
    """The smallest arguments that satisfy the schema's required fields."""
    root = root or schema
    if '$ref' in schema:
        name = schema['$ref'].rsplit('/', 1)[-1]
        return minimal(root.get('$defs', {})[name], root)
    for key in ('anyOf', 'oneOf'):
        if key in schema:
            options = [s for s in schema[key] if s.get('type') != 'null'] or schema[key]
            return minimal(options[0], root)
    if 'enum' in schema:
        return schema['enum'][0]
    if 'const' in schema:
        return schema['const']
    kind = schema.get('type')
    if kind == 'object':
        props = schema.get('properties', {})
        return {name: minimal(props.get(name, {}), root) for name in schema.get('required', [])}
    return {'string': 'x', 'integer': 1, 'number': 1, 'boolean': False, 'array': [], 'null': None}.get(kind, 'x')


class EveryToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_service.ServiceTests()
        self.fixture.setUp()
        patch = mock.patch.object(fnv_game, 'game_processes', lambda: [])
        patch.start()
        self.addCleanup(patch.stop)

    async def asyncTearDown(self):
        self.fixture.tearDown()

    async def test_every_tool_answers_an_envelope(self):
        bad, timings = {}, {}
        async with Client(make_server(self.fixture.p, self.fixture.service)) as client:
            listing = await client.list_tools()
            tools = listing.tools if hasattr(listing, 'tools') else listing
            self.assertGreater(len(tools), 150)
            for tool in sorted(tools, key=lambda t: t.name):
                arguments = minimal(tool.input_schema)
                started = time.monotonic()
                try:
                    result = await asyncio.wait_for(client.call_tool(tool.name, arguments), PER_CALL_SECONDS)
                except asyncio.TimeoutError:
                    bad[tool.name] = 'no answer in %d s' % PER_CALL_SECONDS
                    continue
                except Exception as e:  # noqa: BLE001 - the classification is the test
                    bad[tool.name] = 'raised %s: %s' % (type(e).__name__, str(e)[:200])
                    continue
                finally:
                    timings[tool.name] = round(time.monotonic() - started, 2)
                answer = result.structured_content
                if answer is None:
                    text = ''.join(getattr(c, 'text', '') for c in result.content)
                    try:
                        answer = json.loads(text)
                    except ValueError:
                        bad[tool.name] = 'not an envelope: %s' % text[:200]
                        continue
                if tool.name in NOT_AN_OPERATION:
                    if result.is_error:
                        bad[tool.name] = 'failed: %s' % str(answer)[:200]
                elif not isinstance(answer.get('ok'), bool):
                    bad[tool.name] = 'no ok flag: %s' % str(answer)[:200]
                elif not answer['ok']:
                    error = answer.get('error') or {}
                    if not error.get('code') or not error.get('message'):
                        bad[tool.name] = 'failure without code or message: %s' % str(error)[:200]
                    elif error['code'] in UNCODED:
                        bad[tool.name] = '%s: %s' % (error['code'], str(error.get('message'))[:200])
        slow = {name: s for name, s in timings.items() if s > 10}
        self.assertEqual(bad, {}, 'slow calls: %s' % slow)


class TitleTests(unittest.IsolatedAsyncioTestCase):
    """Release plan Phase 7 (L6): every tool has a display title, and no two share one."""

    async def test_every_tool_has_a_title(self):
        fixture = test_service.ServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        async with Client(make_server(fixture.p, fixture.service)) as client:
            listing = await client.list_tools()
        tools = listing.tools if hasattr(listing, 'tools') else listing
        titles = {tool.name: tool.title for tool in tools}
        self.assertEqual([name for name, title in titles.items() if not title], [])
        self.assertEqual(len(set(titles.values())), len(titles))
        self.assertEqual(titles['fnv_class_vtable'], 'FNV: class vtable')
        self.assertEqual(titles['geck_plugin_save'], 'GECK: plugin save')


class EscapedErrorTests(unittest.IsolatedAsyncioTestCase):
    """A refusal that escapes a tool keeps its code and sentence; a crash stays TOOL_ERROR with no detail."""

    async def test_codes(self):
        from geck_mcp.server import BridgeServer
        from geck_mcp.transport import BridgeError
        server = BridgeServer('t')

        @server.tool()
        def refuses_value() -> dict:
            raise ValueError('operation not found in this project')

        @server.tool()
        def refuses_code() -> dict:
            raise BridgeError('NO_SPEC', 'no spec', uncertain=True)

        @server.tool()
        def crashes() -> dict:
            raise TypeError('secret detail')

        async with Client(server) as client:
            value = (await client.call_tool('refuses_value', {})).structured_content
            coded = (await client.call_tool('refuses_code', {})).structured_content
            crash = (await client.call_tool('crashes', {})).structured_content
        self.assertEqual((value['error']['code'], value['error']['message']),
                         ('INVALID_ARGUMENT', 'operation not found in this project'))
        self.assertTrue(value['error']['retrySafe'])
        self.assertEqual((coded['error']['code'], coded['outcome'], coded['error']['retrySafe']),
                         ('NO_SPEC', 'outcome_unknown', False))
        self.assertEqual(crash['error']['code'], 'TOOL_ERROR')
        self.assertNotIn('secret detail', json.dumps(crash))


if __name__ == '__main__':
    unittest.main()
