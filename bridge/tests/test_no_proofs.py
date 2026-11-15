"""Release plan Task 4.1 (audit B3, decision D2): the tools work without the private fnvsym proof table.

- The tools' own watch hooks (script profile, script trace, damage log) find their functions by address
  (fnv_facts.WATCH_TARGETS), so a map generated without --verified is enough.
- fnv_trace takes allow_unproven like the other hook tools.
- The fnv_name_* tools need FNVSYM_ROOT and say so (NO_PROOF_FOLDER) when it is not set: no machine default.

The map test needs a decomp checkout: set FNV_DECOMP (or FNV_NOPROOF_MAP to a map already generated without
--verified). It is skipped otherwise."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import Client  # noqa: E402

from geck_mcp import fnv_engine  # noqa: E402
from geck_mcp.fnv_agent import AgentTools  # noqa: E402
from geck_mcp.fnv_deep import WATCHES, DeepTools, HookError  # noqa: E402
from geck_mcp.fnv_facts import WATCH_TARGETS  # noqa: E402
from geck_mcp.server import make_server  # noqa: E402
from test_stab_gate import GateFixture  # noqa: E402

BRIDGE = Path(__file__).resolve().parents[1]


class FakeEngine:
    def __init__(self, functions):
        self.functions = functions        # address -> record

    def summary(self, address, with_location=True):
        f = self.functions[address]
        return {'address': '0x%08X' % address, 'name': f['name'], 'status': f['status']}

    def resolve(self, ref):
        for address, f in self.functions.items():
            if f['name'] == ref:
                return address
        raise fnv_engine.EngineMapError('NOT_FOUND', 'no %s' % ref)


def deep_with(engine):
    tools = DeepTools.__new__(DeepTools)
    tools.live = SimpleNamespace(engine=lambda: engine)
    return tools


class WatchTargetTests(unittest.TestCase):
    def test_every_watch_names_a_fixed_target(self):
        self.assertEqual({w['function'] for w in WATCHES.values()}, set(WATCH_TARGETS))

    def test_unproven_names_at_the_target_addresses_are_accepted(self):
        engine = FakeEngine({a: {'name': 'FUN_%08x' % a, 'status': 'unnamed'} for a in WATCH_TARGETS.values()})
        deep = deep_with(engine)
        for name, address in WATCH_TARGETS.items():
            with self.subTest(name=name):
                self.assertEqual(deep._function(name)[0], address)

    def test_a_map_without_the_function_start_is_refused(self):
        deep = deep_with(FakeEngine({}))
        with self.assertRaises(HookError) as cm:
            deep._function('Script::Run')
        self.assertEqual(cm.exception.code, 'ENGINE_MAP_MISMATCH')

    def test_other_names_still_need_proof(self):
        deep = deep_with(FakeEngine({0x00401000: {'name': 'Foo::Bar', 'status': 'decomp'}}))
        with self.assertRaises(HookError) as cm:
            deep._function('Foo::Bar')
        self.assertEqual(cm.exception.code, 'UNPROVEN_FUNCTION')


class ProofFreeMapTests(unittest.TestCase):
    """The real check: a map built from the decomp checkout with no --verified places a function at every target."""

    @classmethod
    def setUpClass(cls):
        prebuilt = os.environ.get('FNV_NOPROOF_MAP')
        decomp = os.environ.get('FNV_DECOMP')
        if prebuilt:
            cls.map = Path(prebuilt)
            cls.tmp = None
            return
        if not decomp or not Path(decomp).is_dir():
            raise unittest.SkipTest('set FNV_DECOMP to a fnv_decomp checkout (or FNV_NOPROOF_MAP) to run this')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.map = Path(cls.tmp.name) / 'engine_map.json'
        subprocess.run([sys.executable, '-m', 'engine_map.generate', '--decomp', decomp, '--out', str(cls.map)],
                       cwd=str(BRIDGE), check=True, capture_output=True, timeout=900)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, 'tmp', None):
            cls.tmp.cleanup()

    def test_the_map_has_no_proofs(self):
        engine = fnv_engine.load(self.map, None, Path(self.map).with_name('none.sqlite'))
        statuses = {f.get('status') for f in engine.functions.values()}
        self.assertNotIn('proven', statuses)

    def test_every_watch_resolves_to_its_address(self):
        engine = fnv_engine.load(self.map, None, Path(self.map).with_name('none.sqlite'))
        deep = deep_with(engine)
        for label, spec in WATCHES.items():
            with self.subTest(watch=label):
                address, record = deep._function(spec['function'])
                self.assertEqual(address, WATCH_TARGETS[spec['function']])
                self.assertEqual(int(str(record['address']), 16), address)


class TraceUnprovenTests(unittest.TestCase):
    def agent(self):
        engine = FakeEngine({0x00401000: {'name': 'Foo::Bar', 'status': 'decomp'}})
        engine.functions[0x00401000]['signature'] = None
        agent = AgentTools.__new__(AgentTools)
        agent.live = SimpleNamespace(engine=lambda: engine)
        return agent

    def test_unproven_is_refused_without_the_flag(self):
        with self.assertRaises(HookError) as cm:
            self.agent()._trace_function('Foo::Bar')
        self.assertEqual(cm.exception.code, 'UNPROVEN_FUNCTION')
        self.assertIn('allow_unproven', str(cm.exception))

    def test_allow_unproven_lets_it_through(self):
        address = self.agent()._trace_function('Foo::Bar', allow_unproven=True)[0]
        self.assertEqual(address, 0x00401000)


class ProofFolderTests(GateFixture, unittest.IsolatedAsyncioTestCase):
    CASES = [
        ('fnv_name_dossier', {'function': 'Actor::GetLevel'}),
        ('fnv_name_oracle', {'action': 'purity', 'function': 'Actor::GetLevel'}),
        ('fnv_name_test', {'action': 'panels'}),
        ('fnv_name_export', {}),
        ('fnv_name_status', {'name_or_address': 'Actor::GetLevel'}),
    ]

    async def test_without_fnvsym_root_the_naming_tools_say_how_to_set_it(self):
        env = {k: v for k, v in os.environ.items() if k != 'FNVSYM_ROOT'}
        wrong = []
        with mock.patch.dict(os.environ, env, clear=True):
            async with Client(make_server(self.project, self.fixture.service)) as client:
                for tool, args in self.CASES:
                    code, result = await self.call(client, tool, args)
                    message = (result.structured_content or {}).get('error', {}).get('message', '')
                    if code != 'NO_PROOF_FOLDER' or 'FNVSYM_ROOT' not in message:
                        wrong.append((tool, code))
        self.assertEqual(wrong, [])

    async def test_with_fnvsym_root_they_get_past_the_check(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {'FNVSYM_ROOT': root}):
            async with Client(make_server(self.project, self.fixture.service)) as client:
                for tool, args in self.CASES:
                    code, _ = await self.call(client, tool, args)
                    self.assertNotEqual(code, 'NO_PROOF_FOLDER', tool)


if __name__ == '__main__':
    unittest.main()
