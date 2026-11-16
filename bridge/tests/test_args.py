"""PLAN-3 S5.2: arguments are checked inside the envelope (T2, T5, T6, G12, G16)."""
import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeLive  # noqa: E402
from geck_mcp.fnv_live import LiveError  # noqa: E402

TOOLS = Path(__file__).resolve().parents[1] / 'geck_mcp' / 'fnv_live_tools.py'


class ParserTests(unittest.TestCase):
    def test_hex_int_names_parameter(self):
        from geck_mcp import fnv_args
        self.assertEqual(fnv_args.hex_int('start', '0x0087F9F0'), 0x0087F9F0)
        self.assertEqual(fnv_args.hex_int('start', '87f9f0'), 0x87F9F0)
        with self.assertRaises(ValueError) as caught:
            fnv_args.hex_int('start', 'zz')
        self.assertEqual(str(caught.exception), "start: expected a hex number such as 0x0087F9F0, got 'zz'")

    def test_form_id_forms(self):
        from geck_mcp import fnv_args
        for text in ('0x14', '14', '00000014'):
            self.assertEqual(fnv_args.form_id(text), 0x14, text)
        self.assertEqual(fnv_args.form_id('FF000001'), 0xFF000001)
        for bad in ('Beef', 'zz', '0x123456789', ''):
            with self.assertRaises(ValueError, msg=bad):
                fnv_args.form_id(bad)

    def test_record_tools_take_short_form_ids(self):
        from geck_mcp.fnv_data import DataTools
        entries = [{'index': 0, 'name': 'FalloutNV.esm', 'fileId': 1}]
        for text in ('0x14', '14', '00000014'):
            self.assertEqual(tuple(DataTools._resolve(None, entries, text)), ('falloutnv.esm', 0x14), text)

    def test_ranged(self):
        from geck_mcp import fnv_args
        self.assertEqual(fnv_args.ranged('value', 255, 'u8'), 255)
        self.assertEqual(fnv_args.ranged('value', '0x10', 'u16'), 16)
        with self.assertRaises(ValueError) as caught:
            fnv_args.ranged('value', 300, 'u8')
        self.assertIn('u8 holds 0 to 255', str(caught.exception))


class SinkRefTests(unittest.TestCase):
    def test_events_ref_names_parameter(self):
        from geck_mcp.fnv_sampling import SinkTools
        with FakeLive() as fake:
            r = fake.live.run('fnv_events', lambda: SinkTools(fake.live).read(0, 'zz'))
            self.assertEqual(r['error']['code'], 'INVALID_ARGUMENT')
            self.assertIn('ref:', r['error']['message'])


class MemWriteTests(unittest.TestCase):
    def test_mem_write_u8_range_is_invalid_argument(self):
        with FakeLive() as fake:
            live = fake.live
            fake.link.put(0x01000000, b'\x05')
            r = live.run('fnv_mem_write', lambda: live.mem_write('0x01000000', 300, as_type='u8', cls='X'), 'act')
            self.assertEqual(r['error']['code'], 'INVALID_ARGUMENT')
            self.assertIn('u8 holds 0 to 255', r['error']['message'])
            self.assertEqual(r['outcome'], 'failed_before_change')
            self.assertEqual(live.journal.entries(), [])

    def test_mangled_type_is_named(self):
        with FakeLive() as fake:
            with self.assertRaises(ValueError) as caught:
                fake.live._encode('?$SettingT@VGameSettingCollection@@', 1)
            self.assertIn('SettingT<GameSettingCollection>', str(caught.exception))
            self.assertNotIn('?$', str(caught.exception))


class EngineGetTests(unittest.TestCase):
    def test_engine_get_arg_count_checked(self):
        from geck_mcp.fnv_control import ControlTools
        with FakeLive() as fake:
            live = fake.live
            fn = {'name': 'Actor::GetLevel', 'status': 'proven', 'address': '0x0087F9F0'}
            engine = SimpleNamespace(
                resolve=lambda f: 0x0087F9F0, summary=lambda a: fn, walk=lambda *a: ([], None),
                functions={0x0087F9F0: {'signature': 'public: short __thiscall Actor::GetLevel(void)'}})
            live._engine = engine
            control = ControlTools(SimpleNamespace(live=live, deep=None))
            called = []
            control._call = lambda *a: called.append(a) or {'value': 1, 'raw': 1}
            control._purity_of = lambda *a: {'pure': True}    # machine-code check: tests/test_r4_contract.py
            with self.assertRaises(LiveError) as caught:
                control.engine_get('Actor::GetLevel', this='player', args=[1, 2, 3])
            self.assertEqual(caught.exception.code, 'BAD_ARGUMENTS')
            self.assertIn('0', str(caught.exception))
            self.assertEqual(called, [])
            control.engine_get('Actor::GetLevel', this='player', args=[])
            self.assertEqual(len(called), 1)


class EnvelopeThroughServerTests(unittest.IsolatedAsyncioTestCase):
    """Bad arguments to a running server come back in the envelope (T2: before, fnv_input's key_name raised before
    invoke and the SDK answered TOOL_ERROR)."""

    async def asyncSetUp(self):
        import test_service
        from unittest import mock
        from geck_mcp import fnv_link
        from geck_mcp.config import FnvSettings
        self.fixture = test_service.ServiceTests()
        self.fixture.setUp()
        root = Path(self.fixture.tmp.name)
        (root / 'mo2/profiles/Dev').mkdir(parents=True)
        fnv = FnvSettings(mo2_root=root / 'mo2', mo2_profile='Dev', engine_control='full',
                          engine_pipe='fnvmcp-args-test-none')
        self.project = self.fixture.p.model_copy(update={'fnv': fnv})

        def no_pipe(link, *args, **kwargs):
            raise fnv_link.LinkError('COMPANION_UNREACHABLE', 'no game in this test')
        for p in (mock.patch.object(fnv_link.EngineLink, 'connect', no_pipe),
                  mock.patch('geck_mcp.fnv_game.game_processes', return_value=[])):
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        self.fixture.tearDown()

    async def call(self, tool, args):
        from mcp import Client
        from geck_mcp.server import make_server
        async with Client(make_server(self.project, self.fixture.service)) as client:
            result = await client.call_tool(tool, args)
        return result.structured_content or {}

    async def test_input_bad_key_in_envelope(self):
        sc = await self.call('fnv_input', {'action': 'tap', 'key_name': 'NoSuchKey'})
        self.assertIn('operationId', sc)
        self.assertEqual(sc['error']['code'], 'INVALID_ARGUMENT')
        self.assertIn('NoSuchKey', sc['error']['message'])

    async def test_probe_bad_start_names_parameter(self):
        sc = await self.call('fnv_probe', {'action': 'plan', 'start': 'zz'})
        self.assertEqual(sc['error']['code'], 'INVALID_ARGUMENT')
        self.assertIn('start:', sc['error']['message'])


def _is_invoke_return(st):
    return (isinstance(st, ast.Return) and isinstance(st.value, ast.Await) and isinstance(st.value.value, ast.Call)
            and getattr(st.value.value.func, 'id', None) in ('invoke', 'offline_invoke'))   # fnv_crash explain, latest


def _harmless(st):
    """Statements allowed before the invoke: the docstring, a nested def, an import, an assignment without calls or
    subscripts (kind / args dicts), an `if` that only returns an invoke."""
    if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
        return True
    if isinstance(st, (ast.FunctionDef, ast.Import, ast.ImportFrom)):
        return True
    if isinstance(st, ast.Assign):
        return not any(isinstance(n, (ast.Call, ast.Subscript, ast.Await)) for n in ast.walk(st.value))
    if isinstance(st, ast.If):
        return not st.orelse and all(_is_invoke_return(s) for s in st.body)
    return False


class NoWorkBeforeInvokeTests(unittest.TestCase):
    def test_no_work_before_invoke(self):
        source = TOOLS.read_text(encoding='utf-8')
        wrong = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.AsyncFunctionDef) or not any(
                    isinstance(d, ast.Call) and getattr(d.func, 'attr', None) == 'tool' for d in node.decorator_list):
                continue
            last = max([i for i, st in enumerate(node.body) if _is_invoke_return(st)], default=None)
            if last is None:
                wrong.append((node.name, 'no return await invoke(...)'))
                continue
            wrong += [(node.name, st.lineno) for st in node.body[:last] if not _harmless(st)]
        self.assertEqual(wrong, [])


if __name__ == '__main__':
    unittest.main()
