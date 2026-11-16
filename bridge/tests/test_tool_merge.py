"""TOOL-MERGE tiers 1 and 2a: the old tools are gone, each old function is an action of a merged tool, and the
merged tools refuse argument mixes that their old tools could not be given. No game runs: engine_control is read
and there is no pipe, so these check the argument handling that comes before any game call."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client  # noqa: E402

from test_stab_gate import GateFixture  # noqa: E402
from geck_mcp.server import make_server  # noqa: E402

# old tool -> the merged tool that does its job now
TIER_1 = {
    'fnv_crash_latest': 'fnv_crash', 'fnv_crash_explain': 'fnv_crash', 'fnv_crash_history': 'fnv_crash',
    'fnv_crash_context': 'fnv_crash', 'fnv_tracepoint': 'fnv_trace', 'fnv_vars': 'fnv_script_instances',
    'fnv_func_callers': 'fnv_func_find', 'fnv_func_callees': 'fnv_func_find', 'fnv_time_control': 'fnv_time',
    'fnv_diff': 'fnv_snapshot', 'fnv_revert_all': 'fnv_undo', 'fnv_launch': 'fnv_game', 'fnv_quit': 'fnv_game',
    'fnv_restart': 'fnv_game', 'fnv_ui_tiles': 'fnv_menu', 'fnv_menu_control': 'fnv_ui_do', 'fnv_spawn': 'fnv_place',
    'fnv_test_run': 'fnv_test', 'fnv_test_run_all': 'fnv_test', 'fnv_do': 'fnv_test',
}


class MergeTests(GateFixture, unittest.IsolatedAsyncioTestCase):
    async def names(self):
        async with Client(make_server(self.project, self.fixture.service)) as client:
            return {t.name for t in (await client.list_tools()).tools}

    async def test_old_names_gone_new_ones_there(self):
        names = await self.names()
        self.assertEqual(sorted(set(TIER_1) & names), [])
        self.assertEqual(sorted(set(TIER_1.values()) - names), [])

    async def test_refusals(self):
        cases = [
            ('fnv_undo', {'all': True, 'count': 3}, 'all=true'),
            ('fnv_trace', {'function': 'Actor::GetLevel', 'when': [{'on': 'this', 'ref': 'player'}],
                           'filter': {'on': 'this', 'ref': 'player'}}, 'filter'),
            ('fnv_place', {'action': 'at', 'base': '0x0000000F', 'export': False, 'position': [0, 0, 0]}, 'position'),
            ('fnv_place', {'action': 'at', 'base': '0x0000000F', 'count': 2}, 'export=false'),
            ('fnv_place', {'action': 'list', 'export': False}, 'export'),
            ('fnv_game', {'action': 'quit', 'timeout_seconds': 300}, '5 to 120'),
            ('fnv_game', {'action': 'launch', 'timeout_seconds': 10}, '20 to 600'),
            ('fnv_menu', {'action': 'state', 'menu': 'inventory'}, 'menu'),
            ('fnv_func_find', {'query': 'x', 'mode': 'callers', 'offset': 5}, 'offset'),
            ('fnv_crash', {'action': 'context', 'limit': 50}, '1 to 20'),
            ('fnv_crash', {'action': 'history', 'path': 'x.log'}, 'path'),
            ('fnv_time', {'action': 'pause', 'set_hour': 3}, 'set_hour'),
            ('fnv_test', {'action': 'run_all', 'steps': [{'wait': 1}]}, 'steps'),
            ('fnv_snapshot', {'action': 'diff', 'name': 'x'}, 'name'),
        ]
        wrong = []
        # full control: under read the gate answers first (CONTROL_DISABLED); the checks come before any game call
        full = self.project.model_copy(update={'fnv': self.project.fnv.model_copy(update={'engine_control': 'full'})})
        async with Client(make_server(full, self.fixture.service)) as client:
            for tool, args, word in cases:
                code, result = await self.call(client, tool, args)
                message = ((result.structured_content or {}).get('error') or {}).get('message', '')
                if code != 'INVALID_ARGUMENT' or word not in message:
                    wrong.append((tool, args, code, message[:150]))
        self.assertEqual(wrong, [])

    async def test_crash_explain_is_the_default_and_offline(self):
        async with Client(make_server(self.project, self.fixture.service)) as client:
            answer = (await client.call_tool('fnv_crash', {})).structured_content
        self.assertEqual(answer['operation'], 'fnv_crash')
        self.assertEqual(self.pipe_ops, [])                  # no game needed: it reads crash log files


if __name__ == '__main__':
    unittest.main()
