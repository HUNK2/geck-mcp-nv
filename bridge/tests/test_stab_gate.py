"""PLAN-3 S1.5, bug T1: every action that changes the running game is gated by fnv.engine_control (decision D2).

The real server is built in-process with engine_control = "read". Every (tool, action) in
fnv_live_tools.MUTATING_ACTIONS must answer CONTROL_DISABLED before any pipe op; their read actions must not."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client  # noqa: E402

from geck_mcp import fnv_link  # noqa: E402
from geck_mcp.config import FnvSettings  # noqa: E402
from geck_mcp.fnv_live_tools import (FNV_LIVE_TOOLS, FULL_ONLY_ACTIONS, MUTATING_ACTIONS,  # noqa: E402
                                     READ_LEVEL_CONTROL, needs_full)
from geck_mcp.server import make_server  # noqa: E402
import test_service  # noqa: E402

STEPS = [{'wait': 1}]
# (tool, action label) -> minimal valid arguments. The label is what MUTATING_ACTIONS lists ('*' = every call).
MUTATING_CASES = {
    ('fnv_probe', 'install'): {'action': 'install'},
    ('fnv_probe', 'remove'): {'action': 'remove'},
    ('fnv_probe', 'clear'): {'action': 'clear'},
    ('fnv_probe', 'config'): {'action': 'config'},
    ('fnv_probe', 'harvest'): {'action': 'harvest'},
    ('fnv_name_oracle', 'run'): {'action': 'run', 'function': 'Actor::GetLevel'},
    ('fnv_name_oracle', 'calibrate'): {'action': 'calibrate'},
    ('fnv_name_oracle', 'sweep'): {'action': 'sweep'},
    ('fnv_name_test', 'test'): {'action': 'test'},
    ('fnv_name_test', 'battery'): {'action': 'battery'},
    ('fnv_field_watch', '*'): {'action': 'find', 'ref': 'player'},
    ('fnv_atlas', 'run'): {'action': 'run'},
    ('fnv_atlas', 'import_frida'): {'action': 'import_frida'},
    ('fnv_hooks', 'remove'): {'action': 'remove', 'hook_id': 'all'},
    ('fnv_trace', 'start'): {'action': 'start', 'function': 'Actor::GetLevel'},
    ('fnv_trace', 'stop'): {'action': 'stop', 'function': 'Actor::GetLevel'},
    ('fnv_script_profile', '*'): {'action': 'read'},   # read installs the hook too
    ('fnv_script_trace', '*'): {'action': 'read', 'script': 'MyScript'},   # read installs the hook too
    ('fnv_damage_log', '*'): {'action': 'read'},   # read installs the hook too
    ('fnv_frame_profile', '*'): {},
    ('fnv_crash', 'capture_on'): {'action': 'capture_on'},
    ('fnv_crash', 'capture_off'): {'action': 'capture_off'},
    ('fnv_memory', 'alloc_trace'): {'action': 'alloc_trace'},
    ('fnv_memory', 'refcount'): {'action': 'refcount', 'pointer': '0x01000000'},
    ('fnv_events', 'subscribe'): {'subscribe': ['onhit']},
    ('fnv_events', 'unsubscribe'): {'unsubscribe': ['all']},
    ('fnv_watch', '*'): {'items': ['player.x'], 'seconds': 0.1},   # expressions can change the game (W1)
    ('fnv_warnings', 'capture'): {'action': 'capture', 'mode': 'start'},    # hooks the engine's Warning (W3)
    ('fnv_window', 'release_mouse'): {'action': 'release_mouse'},           # writes game memory (W3)
    ('fnv_window', 'capture_mouse'): {'action': 'capture_mouse'},
    ('fnv_window', 'unpause'): {'action': 'unpause'},
    ('fnv_snapshot', '*'): {'name': 'x', 'items': [{'console': 'player.kill'}]},
    ('fnv_systems', 'formula'): {'action': 'formula', 'name': 'x'},
    ('fnv_systems', 'sweep'): {'action': 'sweep', 'name': 'x'},
    ('fnv_systems', 'barter'): {'action': 'barter', 'args': ['0x00015169']},   # round 4: perk hooks + engine calls
    ('fnv_leveled_roll', 'seed'): {'form': '0x00000014', 'seed': 1},
    ('fnv_test', '*'): {'yaml': 'name: x\nsteps:\n  - wait: 1\n'},
    # TOOL-MERGE tier 1: merged tools list their changing actions; their reads are in READ_CASES
    ('fnv_time', 'calendar_set'): {'set_hour': 12},
    ('fnv_time', 'pause'): {'action': 'pause'},
    ('fnv_time', 'resume'): {'action': 'resume'},
    ('fnv_time', 'force_resume'): {'action': 'force_resume'},
    ('fnv_time', 'hold'): {'action': 'hold'},
    ('fnv_time', 'release'): {'action': 'release'},
    ('fnv_time', 'step'): {'action': 'step', 'frames': 1},
    ('fnv_time', 'fixed'): {'action': 'fixed', 'fps': 60},
    ('fnv_time', 'speed'): {'action': 'speed', 'x': 1},
    ('fnv_undo', '*'): {'all': True},
    ('fnv_ui_do', '*'): {'action': 'close_all'},
    ('fnv_place', 'at'): {'action': 'at', 'base': '0x0000000F', 'export': False},
    ('fnv_place', 'array'): {'action': 'array', 'base': '0x0000000F'},
    ('fnv_place', 'nudge'): {'action': 'nudge', 'ref': 'player', 'offset': [1, 0, 0]},
    ('fnv_place', 'rotate'): {'action': 'rotate', 'ref': 'player', 'rotation': [0, 0, 1]},
    ('fnv_place', 'scale'): {'action': 'scale', 'ref': 'player', 'scale': 1},
    ('fnv_place', 'snap'): {'action': 'snap', 'ref': 'player'},
    ('fnv_place', 'remove'): {'action': 'remove'},
    ('fnv_session', 'start'): {'action': 'start'},
    ('fnv_session', 'end'): {'action': 'end'},
    ('fnv_crawl', '*'): {'action': 'cells'},
    ('fnv_bisect', '*'): {'level': 'mods', 'steps': STEPS},
    ('fnv_replay', 'record'): {'action': 'record', 'name': 'x'},
    ('fnv_replay', 'stop'): {'action': 'stop'},
    ('fnv_replay', 'play'): {'action': 'play', 'name': 'x'},
    # hooks ApplyPerkModifiers and ApplyFunction until stop; evaluate hooks, calls, unhooks (round 4 M1)
    ('fnv_perk_entries', 'start'): {'action': 'start'},
    ('fnv_perk_entries', 'stop'): {'action': 'stop'},
    ('fnv_perk_entries', 'evaluate'): {'action': 'evaluate', 'entry_point': 9, 'value': 100},
}
READ_CASES = {
    'fnv_probe': {'action': 'status'},
    'fnv_name_oracle': {'action': 'purity', 'function': 'Actor::GetLevel'},
    'fnv_name_test': {'action': 'panels'},
    'fnv_atlas': {'action': 'actions'},
    'fnv_hooks': {'action': 'list'},
    'fnv_trace': {'action': 'list'},
    'fnv_crash': {'action': 'capture_status'},
    'fnv_time': {'action': 'status'},
    'fnv_place': {'action': 'list'},
    'fnv_memory': {'action': 'pools'},
    'fnv_events': {},
    'fnv_systems': {'action': 'formulas'},
    'fnv_leveled_roll': {'form': '0x00000014'},
    'fnv_session': {'action': 'status'},
    'fnv_replay': {'action': 'list'},
    'fnv_warnings': {'action': 'capture', 'mode': 'status'},
    'fnv_window': {'action': 'status'},
    'fnv_perk_entries': {'action': 'read'},
    'fnv_engine_get': {'function': 'Actor::GetLevel', 'this': 'player'},   # pure getters only (round 4 H1)
}

# Release plan Task 1.2 (audit B2): calls that put agent-chosen code or files into the game or its mod profile need
# "full" although their kind is control. (tool, action label) -> minimal valid arguments, like MUTATING_CASES.
FULL_ONLY_CASES = {
    ('fnv_plugin_build', 'build'): {'action': 'build', 'name': 'x', 'source': 'int x;'},
    ('fnv_plugin_build', 'install'): {'action': 'install', 'name': 'x'},
    ('fnv_install', '*'): {'plugin_path': 'C:/nope/Nope.esp'},
    ('fnv_profile_set', 'apply'): {'plugins': {'Nope.esp': True}},
}
# control calls that stay allowed under read
READ_LEVEL_CASES = {
    ('fnv_plugin_build', 'scaffold'): {'action': 'scaffold', 'name': 'x'},
    ('fnv_profile_set', 'dry_run'): {'plugins': {'Nope.esp': True}, 'dry_run': True},
    ('fnv_author', 'scaffold'): {'action': 'scaffold', 'kind': 'item', 'name': 'x'},
}


def expanded(table=MUTATING_ACTIONS):
    out = set()
    for tool, rule in table.items():
        out |= {(tool, '*')} if rule == '*' else {(tool, a) for a in rule}
    return out


class GateFixture:
    """The in-process server with engine_control = "read" and no pipe. Mix into IsolatedAsyncioTestCase."""

    async def asyncSetUp(self):
        self.fixture = test_service.ServiceTests()
        self.fixture.setUp()
        root = Path(self.fixture.tmp.name)
        (root / 'mo2/profiles/Dev').mkdir(parents=True)
        fnv = FnvSettings(mo2_root=root / 'mo2', mo2_profile='Dev', engine_control='read',
                          engine_pipe='fnvmcp-gate-test-none')
        self.project = self.fixture.p.model_copy(update={'fnv': fnv})
        self.pipe_ops = []

        def no_pipe(link, *args, **kwargs):
            self.pipe_ops.append(args[:1])
            raise fnv_link.LinkError('COMPANION_UNREACHABLE', 'no game in this test')
        patches = [mock.patch.object(fnv_link.EngineLink, 'connect', no_pipe),
                   mock.patch('geck_mcp.fnv_game.game_processes', return_value=[])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        self.fixture.tearDown()

    async def call(self, client, tool, args):
        result = await client.call_tool(tool, args)
        sc = result.structured_content or {}
        return (sc.get('error') or {}).get('code'), result


class GateTests(GateFixture, unittest.IsolatedAsyncioTestCase):
    def test_cases_cover_the_table(self):
        self.assertEqual(set(MUTATING_CASES), expanded())

    async def test_every_mutating_action_is_refused_when_read(self):
        wrong = []
        async with Client(make_server(self.project, self.fixture.service)) as client:
            for (tool, label), args in sorted(MUTATING_CASES.items()):
                self.pipe_ops.clear()
                code, result = await self.call(client, tool, args)
                if code != 'CONTROL_DISABLED' or self.pipe_ops:
                    text = ''.join(getattr(c, 'text', '') for c in result.content)[:200]
                    wrong.append((tool, label, code, len(self.pipe_ops), text))
        self.assertEqual(wrong, [])

    async def test_read_actions_still_work_when_read(self):
        wrong = []
        async with Client(make_server(self.project, self.fixture.service)) as client:
            for tool, args in sorted(READ_CASES.items()):
                code, _ = await self.call(client, tool, args)
                if code == 'CONTROL_DISABLED':
                    wrong.append(tool)
        self.assertEqual(wrong, [])

    async def test_annotations_match_kinds(self):
        async with Client(make_server(self.project, self.fixture.service)) as client:
            listing = await client.list_tools()
            tools = {t.name: t for t in (listing.tools if hasattr(listing, 'tools') else listing)}
        for tool in MUTATING_ACTIONS:
            self.assertFalse(tools[tool].annotations.read_only_hint, tool)
            self.assertNotEqual(FNV_LIVE_TOOLS[tool][0], 'read', tool)


class FullOnlyTests(GateFixture, unittest.IsolatedAsyncioTestCase):
    """Release plan Task 1.2: one rule for what read allows."""

    def test_full_only_cases_cover_the_table(self):
        self.assertEqual(set(FULL_ONLY_CASES), expanded(FULL_ONLY_ACTIONS))

    async def test_every_full_only_action_is_refused_when_read(self):
        wrong = []
        async with Client(make_server(self.project, self.fixture.service)) as client:
            for (tool, label), args in sorted(FULL_ONLY_CASES.items()):
                self.pipe_ops.clear()
                code, result = await self.call(client, tool, args)
                if code != 'CONTROL_DISABLED' or self.pipe_ops:
                    text = ''.join(getattr(c, 'text', '') for c in result.content)[:200]
                    wrong.append((tool, label, code, len(self.pipe_ops), text))
        self.assertEqual(wrong, [])

    async def test_refusal_says_what_to_set(self):
        async with Client(make_server(self.project, self.fixture.service)) as client:
            _, result = await self.call(client, 'fnv_install', FULL_ONLY_CASES[('fnv_install', '*')])
        message = result.structured_content['error']['message']
        self.assertIn('fnv.engine_control = "full"', message)
        self.assertIn('puts code or files into the game', message)

    async def test_read_level_calls_are_not_refused(self):
        wrong = []
        async with Client(make_server(self.project, self.fixture.service)) as client:
            for (tool, label), args in sorted(READ_LEVEL_CASES.items()):
                code, _ = await self.call(client, tool, args)
                if code == 'CONTROL_DISABLED':
                    wrong.append((tool, label))
        self.assertEqual(wrong, [])

    def test_every_control_tool_is_classified(self):
        control = {n for n, (kind, _) in FNV_LIVE_TOOLS.items() if kind == 'control'}
        tables = set(MUTATING_ACTIONS) | set(FULL_ONLY_ACTIONS) | set(READ_LEVEL_CONTROL)
        self.assertEqual(sorted(control - tables), [])
        whole = {n for t in (MUTATING_ACTIONS, FULL_ONLY_ACTIONS) for n, rule in t.items() if rule == '*'}
        self.assertEqual(sorted(whole & set(READ_LEVEL_CONTROL)), [])
        self.assertEqual(sorted(n for n, why in READ_LEVEL_CONTROL.items() if not why.strip()), [])

    def test_needs_full(self):
        self.assertTrue(needs_full('fnv_install', None))
        self.assertTrue(needs_full('fnv_plugin_build', 'install'))
        self.assertTrue(needs_full('fnv_plugin_build', None))     # a listed tool with no action: assume the worst
        self.assertFalse(needs_full('fnv_plugin_build', 'scaffold'))
        self.assertFalse(needs_full('fnv_game', None))

    async def test_no_fnv_block_means_read(self):
        self.project = self.project.model_copy(update={'fnv': None})
        async with Client(make_server(self.project, self.fixture.service)) as client:
            code, _ = await self.call(client, 'fnv_install', FULL_ONLY_CASES[('fnv_install', '*')])
        self.assertEqual(code, 'CONTROL_DISABLED')


if __name__ == '__main__':
    unittest.main()
