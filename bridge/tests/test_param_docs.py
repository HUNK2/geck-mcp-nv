"""Release plan Phase 7 (M1): every parameter of a tool with six or more parameters is described in its schema."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client  # noqa: E402

import test_service  # noqa: E402
from geck_mcp.param_docs import PARAM_DOCS  # noqa: E402
from geck_mcp.server import make_server  # noqa: E402


def own_description(schema):
    return bool(schema.get('description')) or any(s.get('description') for s in schema.get('anyOf', []))


class ParamDocTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_service.ServiceTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        self.fixture.tearDown()

    async def listing(self):
        async with Client(make_server(self.fixture.p, self.fixture.service)) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    async def test_tools_with_six_or_more_parameters_are_described(self):
        missing = {}
        for name, tool in (await self.listing()).items():
            props = tool.input_schema.get('properties', {})
            if len(props) >= 6:
                gaps = [p for p, schema in props.items() if not own_description(schema)]
                if gaps:
                    missing[name] = gaps
        self.assertEqual(missing, {})

    async def test_every_entry_is_real_and_needed(self):
        from geck_mcp.fnv_live_tools import register  # noqa: F401 - the listing below is the truth
        server = make_server(self.fixture.p, self.fixture.service)
        raw = {t.name: t.parameters.get('properties', {}) for t in server._tool_manager.list_tools()}
        problems = []
        for tool, params in PARAM_DOCS.items():
            if tool not in raw:
                problems.append('%s: no such tool' % tool)
                continue
            for param, text in params.items():
                if param not in raw[tool]:
                    problems.append('%s.%s: no such parameter' % (tool, param))
                elif own_description(raw[tool][param]):
                    problems.append('%s.%s: already described in its signature' % (tool, param))
                elif not (isinstance(text, str) and 10 <= len(text) <= 300):
                    problems.append('%s.%s: description must be 10 to 300 characters' % (tool, param))
        self.assertEqual(problems, [])


if __name__ == '__main__':
    unittest.main()
