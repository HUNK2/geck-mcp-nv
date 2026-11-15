"""Release plan Phase 7 (M1): one shape for a position. Every tool takes `position` (and fnv_raycast `start`/`end`)
as [x, y, z]; fnv_move's null keeps that axis. fnv_move and fnv_raycast still accept the old {x, y, z} object."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client  # noqa: E402

import test_service  # noqa: E402
from geck_mcp import fnv_live_tools  # noqa: E402
from geck_mcp.server import make_server  # noqa: E402


def kinds(schema):
    return sorted(s.get('type') for s in schema.get('anyOf', [schema]) if s.get('type') != 'null')


class XyzTests(unittest.TestCase):
    def test_conversion(self):
        xyz = fnv_live_tools.xyz_dict
        self.assertEqual(xyz([1, None, 3.5]), {'x': 1.0, 'z': 3.5})
        self.assertEqual(xyz({'z': 2}), {'z': 2.0})
        self.assertIsNone(xyz(None))
        with self.assertRaises(ValueError):
            xyz({'w': 1})


class PositionSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_service.ServiceTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        self.fixture.tearDown()

    async def test_every_position_is_an_array_first(self):
        async with Client(make_server(self.fixture.p, self.fixture.service)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            for tool in tools.values():
                props = tool.input_schema.get('properties', {})
                for name in ('position', 'start', 'end'):
                    if name in props and tool.name not in ('fnv_launch', 'fnv_restart', 'fnv_probe', 'fnv_do',
                                                           'fnv_session', 'fnv_systems', 'fnv_bisect',
                                                           'fnv_dialogue_choose'):
                        self.assertIn('array', kinds(props[name]), '%s.%s' % (tool.name, name))
            # the old object still passes the schema: the read gate answers, not the argument check
            for arguments in ({'position': [100, None, 5]}, {'position': {'x': 100}},
                              {'position': [1, 2, 3], 'rotation': [None, None, 90]}):
                answer = (await client.call_tool('fnv_move', arguments)).structured_content
                self.assertNotEqual(answer['error']['code'], 'INVALID_ARGUMENTS', arguments)
            answer = (await client.call_tool('fnv_raycast', {'start': [0, 0, 0], 'end': {'x': 1, 'y': 0, 'z': 0}}))
            self.assertNotEqual(answer.structured_content['error']['code'], 'INVALID_ARGUMENTS')


if __name__ == '__main__':
    unittest.main()
