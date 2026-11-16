"""TOOL-MERGE: the per-action parameter table (bug T8). A tool listed in ACTION_PARAMS refuses a parameter that the
chosen action does not use, when the call gives it a value other than its default, with INVALID_ARGUMENT naming the
parameter and what the action takes. The table must cover each listed tool exactly: every action of its enum, every
parameter used by some action, and no parameter that does not exist."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client  # noqa: E402

import test_service  # noqa: E402
from geck_mcp.action_params import ACTION_PARAMS, unused_arguments  # noqa: E402
from geck_mcp.server import make_server  # noqa: E402


def enum_of(schema):
    for option in [schema, *schema.get('anyOf', [])]:
        if 'enum' in option:
            return set(option['enum'])
    return set()


class TableTests(unittest.TestCase):
    def setUp(self):
        fixture = test_service.ServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        project = fixture.p.model_copy(update={'toolsets': ['game', 'editor', 'naming', 'legacy']})
        server = make_server(project, fixture.service)
        self.raw = {t.name: t.parameters for t in server._tool_manager.list_tools()}

    def test_the_table_covers_each_tool_exactly(self):
        problems = []
        for tool, entry in ACTION_PARAMS.items():
            if tool not in self.raw:
                problems.append('%s: no such tool' % tool)
                continue
            props = self.raw[tool].get('properties', {})
            by = entry.get('by', 'action')
            if by not in props:
                problems.append('%s: no parameter %s' % (tool, by))
                continue
            actions = set(entry['actions'])
            if actions != enum_of(props[by]):
                problems.append('%s: actions %s, enum %s' % (tool, sorted(actions), sorted(enum_of(props[by]))))
            listed = set(entry.get('common', ())) | {by}
            for params in entry['actions'].values():
                listed |= set(params)
            if listed - set(props):
                problems.append('%s: not parameters: %s' % (tool, sorted(listed - set(props))))
            if set(props) - listed:
                problems.append('%s: used by no action: %s' % (tool, sorted(set(props) - listed)))
            required = set(self.raw[tool].get('required', ())) - {by} - set(entry.get('common', ()))
            if required:
                problems.append('%s: required but not common: %s' % (tool, sorted(required)))
        self.assertEqual(problems, [])

    def test_unused_arguments(self):
        schema = {'properties': {'action': {'default': 'a', 'enum': ['a', 'b']}, 'x': {'default': None},
                                 'y': {'default': 3}}}
        table = {'t': {'actions': {'a': {'x'}, 'b': set()}, 'common': {'y'}}}
        self.assertEqual(unused_arguments('t', {'x': 1}, schema, table), ('a', []))
        self.assertEqual(unused_arguments('t', {'action': 'b', 'x': 1}, schema, table), ('b', ['x']))
        self.assertEqual(unused_arguments('t', {'action': 'b', 'x': None}, schema, table), ('b', []))
        self.assertEqual(unused_arguments('t', {'action': 'b', 'y': 9}, schema, table), ('b', []))
        self.assertEqual(unused_arguments('other', {'z': 1}, schema, table), (None, []))


class RefusalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_service.ServiceTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        self.fixture.tearDown()

    async def test_an_unused_parameter_is_refused_before_the_tool_runs(self):
        async with Client(make_server(self.fixture.p, self.fixture.service)) as client:
            answer = (await client.call_tool('fnv_place', {'action': 'list', 'base': 'player'})).structured_content
            self.assertEqual(answer['error']['code'], 'INVALID_ARGUMENT')
            self.assertIn('base', answer['error']['message'])
            self.assertIn('list', answer['error']['message'])
            # a used parameter, or an unused one at its default, reaches the tool (its own answer, not the check)
            for arguments in ({'action': 'check', 'ref': 'player'}, {'action': 'list', 'snap': False}):
                answer = (await client.call_tool('fnv_place', arguments)).structured_content
                self.assertNotEqual((answer.get('error') or {}).get('code'), 'INVALID_ARGUMENT', arguments)


if __name__ == '__main__':
    unittest.main()
