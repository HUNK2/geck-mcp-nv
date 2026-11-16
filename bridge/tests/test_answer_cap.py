"""Release plan Task 6.3 (audit M2): one cap on answer size. An answer above 40,000 characters is cut at a row
boundary, gets truncated: true and a sentence naming the paging argument. fnv_class_vtable pages its slots and
fnv_func_find (callers and callees too) has a lower default limit."""
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client  # noqa: E402

from geck_mcp import server as server_module  # noqa: E402
from geck_mcp.config import Project  # noqa: E402

BRIDGE = Path(__file__).resolve().parents[1]
CAP = 40000


def project(folder):
    path = Path(folder) / 'p.json'
    path.write_text(json.dumps({'project_id': 'cap', 'game_root': str(folder), 'state_dir': 'state'}),
                    encoding='utf-8')
    return Project.load(path)


def envelope(rows):
    return {'ok': True, 'operationId': 'o', 'operation': 'x', 'projectId': 'cap', 'outcome': 'unchanged',
            'data': {'count': len(rows), 'results': rows, 'note': 'n'}, 'error': None}


def size(result):
    return len(json.dumps(result.structured_content))


class CapTests(unittest.IsolatedAsyncioTestCase):
    def test_the_cap_is_40000(self):
        self.assertEqual(server_module.ANSWER_CAP, CAP)

    async def build(self, tmp, with_offset):
        server = server_module.make_server(project(tmp))
        rows = [{'i': i, 'text': 'x' * 200} for i in range(1000)]

        if with_offset:
            @server.tool()
            def fnv_big_fake(limit: int = 1000, offset: int = 0) -> server_module.Envelope:
                """A test tool with a large answer."""
                return envelope(rows[offset:offset + limit])
        else:
            @server.tool()
            def fnv_big_fake() -> server_module.Envelope:
                """A test tool with a large answer and no paging."""
                return envelope(rows)
        return server

    async def test_a_large_answer_is_cut_at_a_row_and_names_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with Client(await self.build(tmp, True)) as client:
                result = await client.call_tool('fnv_big_fake', {})
        data = result.structured_content['data']
        self.assertLessEqual(size(result), CAP)
        self.assertTrue(data['truncated'])
        kept = data['results']
        self.assertEqual([r['i'] for r in kept], list(range(len(kept))))   # whole rows, from the start
        self.assertIn('offset=%d' % len(kept), data['truncatedNote'])
        self.assertIn('1000', data['truncatedNote'])
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)

    async def test_without_offset_the_note_names_limit_or_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with Client(await self.build(tmp, False)) as client:
                result = await client.call_tool('fnv_big_fake', {})
        data = result.structured_content['data']
        self.assertTrue(data['truncated'])
        self.assertLessEqual(size(result), CAP)
        self.assertIn('narrower', data['truncatedNote'])

    async def test_a_small_answer_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = server_module.make_server(project(tmp))

            @server.tool()
            def fnv_small_fake() -> server_module.Envelope:
                """A test tool with a small answer."""
                return envelope([{'i': 1}])
            async with Client(server) as client:
                result = await client.call_tool('fnv_small_fake', {})
        self.assertNotIn('truncated', result.structured_content['data'])


class DefaultTests(unittest.TestCase):
    def test_lower_defaults(self):
        from geck_mcp import fnv_tools
        source = inspect.getsource(fnv_tools.register)
        self.assertIn("status: list[Status] | None = None, limit: Limit = 25,", source)    # fnv_func_find, all modes
        self.assertIn('limit: Limit = 80, offset: Offset = 0', source)


class PlayerCharacterVtableTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_player_vtable_fits_and_says_how_to_get_the_rest(self):
        example = BRIDGE / 'projects' / 'example-fnv.json'
        p = Project.load(example)
        if not (BRIDGE / 'state' / 'engine_map.json').is_file():
            self.skipTest('needs the engine map in bridge/state')
        async with Client(server_module.make_server(p)) as client:
            result = await client.call_tool('fnv_class_vtable', {'class_name': 'PlayerCharacter'})
            data = result.structured_content['data']
            self.assertLessEqual(size(result), CAP)
            self.assertGreater(data['total'], len(data['slots']))
            self.assertIn('offset=%d' % len(data['slots']), data['more'])
            rest = await client.call_tool('fnv_class_vtable', {'class_name': 'PlayerCharacter',
                                                                'offset': len(data['slots'])})
            self.assertEqual(rest.structured_content['data']['slots'][0]['slot'], len(data['slots']))


if __name__ == '__main__':
    unittest.main()
