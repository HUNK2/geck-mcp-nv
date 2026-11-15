"""PLAN-3 exit gate, round-4 audit (docs/audit4/contract.md): H1 fnv_engine_get, M1 fnv_perk_entries."""
import os
import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeLive  # noqa: E402
from geck_mcp.fnv_deep import HookError  # noqa: E402
from geck_mcp.fnv_live_tools import FNV_LIVE_TOOLS, gated_kind  # noqa: E402

try:
    import capstone  # noqa: F401
    HAVE_CAPSTONE = True
except ImportError:
    HAVE_CAPSTONE = False
needs_capstone = unittest.skipUnless(HAVE_CAPSTONE, 'capstone (the naming extra) is not installed')

BRIDGE = Path(__file__).resolve().parents[1]

# Small x86 bodies for a fake clean exe (fnv_oracle.Purity decodes them with capstone).
GET_LEVEL = 0x00500000      # mov eax, dword ptr [ecx+0x10] ; ret                -> pure
GET_SPEED = 0x00500100      # fld dword ptr [ecx+0x10] ; ret                      -> pure, float result
GET_ROW = 0x00500200        # mov eax,[esp+8] ; mov edx,[ecx] ; mov [eax],edx ; ret 8   -> writes the out pointer
GET_SUNRISE = 0x00500300    # mov dword ptr [0x011CA9E8], 0 ; ret                -> writes a global
GET_VIA = 0x00500400        # call GET_ROW ; ret                                  -> writes through a callee
CODE = {
    GET_LEVEL: bytes.fromhex('8b4110c3'),
    GET_SPEED: bytes.fromhex('d94110') + b'\xc3',
    GET_ROW: bytes.fromhex('8b442408' '8b11' '8910' 'c20800'),
    GET_SUNRISE: bytes.fromhex('c705e8a91c0100000000') + b'\xc3',
    GET_VIA: b'\xe8' + struct.pack('<i', GET_ROW - (GET_VIA + 5)) + b'\xc3',
}
FUNCTIONS = {
    GET_LEVEL: ('Actor::GetLevel', 'public: short __thiscall Actor::GetLevel(void)'),
    GET_SPEED: ('Actor::GetSpeed', 'public: float __thiscall Actor::GetSpeed(void)'),
    GET_ROW: ('NiMatrix3::GetRow', 'public: void __thiscall NiMatrix3::GetRow(unsigned int, class NiPoint3 &)'),
    GET_SUNRISE: ('Sky::GetSunriseBegin', 'public: float __thiscall Sky::GetSunriseBegin(void)'),
    GET_VIA: ('Thing::GetRowCopy', 'public: void __thiscall Thing::GetRowCopy(unsigned int, class NiPoint3 &)'),
}


class FakeImage:
    def read(self, address, length):
        for start, code in CODE.items():
            if start <= address < start + len(code):
                return code[address - start:address - start + length]
        return None


def fake_engine():
    by_name = {name: a for a, (name, _) in FUNCTIONS.items()}
    return SimpleNamespace(
        resolve=lambda f: by_name.get(f) or int(f, 16),
        summary=lambda a: {'name': FUNCTIONS[a][0], 'status': 'proven', 'address': '0x%08X' % a},
        walk=lambda *a: ([], None),       # no named callees: the old name rule sees nothing
        functions={a: {'signature': sig, 'size': len(CODE[a])} for a, (_, sig) in FUNCTIONS.items()})


class EngineGetPurityTests(unittest.TestCase):
    """H1: fnv_engine_get (read) ran proven "getters" that write memory, and called with no fault guard."""

    def setUp(self):
        from geck_mcp.fnv_control import ControlTools
        self.fake = FakeLive(engine_control='read')
        self.addCleanup(self.fake.close)
        self.live = self.fake.live
        self.live._engine = fake_engine()
        self.image = FakeImage()

        def image(required=True):
            if self.image is None:
                raise HookError('CLEAN_EXE_MISSING', 'Set fnv.clean_exe')
            return self.image
        agent = SimpleNamespace(live=self.live, deep=SimpleNamespace(_image=image),
                                describe_pointers=lambda pointers: {})
        self.control = ControlTools(agent)

    def get(self, function, this='0x01000000', args=None):
        self.assertEqual(gated_kind('fnv_engine_get', None, FNV_LIVE_TOOLS['fnv_engine_get'][0]), 'read')
        return self.live.run('fnv_engine_get', lambda: self.control.engine_get(function, this, args), 'read')

    def calls(self):
        return [args for name, args in self.fake.link.ops if name == 'call']

    def assert_refused(self, reply):
        self.assertFalse(reply['ok'], reply)
        self.assertEqual(reply['error']['code'], 'MAY_WRITE', reply['error'])
        text = reply['error']['message']
        self.assertIn('engine_control', text)
        self.assertIn('full', text)
        self.assertIn('fnv_engine_call', text)
        self.assertEqual(self.calls(), [], 'nothing may be called')

    @needs_capstone
    def test_out_pointer_getter_is_refused(self):
        # the audit's case: NiMatrix3::GetRow copies 12 bytes to the caller's pointer
        self.assert_refused(self.get('NiMatrix3::GetRow', args=[0, '0x011DEA3C']))

    @needs_capstone
    def test_global_writing_getter_is_refused(self):
        self.assert_refused(self.get('Sky::GetSunriseBegin'))

    @needs_capstone
    def test_getter_writing_through_an_unnamed_callee_is_refused(self):
        self.assert_refused(self.get('Thing::GetRowCopy', args=[0, '0x011DEA3C']))

    @needs_capstone
    def test_refusal_says_why(self):
        reply = self.get('NiMatrix3::GetRow', args=[0, '0x011DEA3C'])
        self.assertIn('0x00500206', reply['error']['message'])     # the store instruction
        self.assertFalse(reply['data']['purity']['pure'])

    def test_no_clean_exe_is_refused(self):
        self.image = None
        reply = self.get('Actor::GetLevel')
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['error']['code'], 'CLEAN_EXE_MISSING')
        self.assertIn('fnv_engine_call', reply['error']['message'])
        self.assertEqual(self.calls(), [])

    @needs_capstone
    def test_pure_getter_runs_guarded(self):
        reply = self.get('Actor::GetLevel')
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['outcome'], 'unchanged')
        self.assertEqual(reply['data']['result'], 1)
        self.assertEqual(self.calls(), [['500000', 'ig', '1000000', '0']])

    @needs_capstone
    def test_float_getter_runs_guarded(self):
        reply = self.get('Actor::GetSpeed')
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(self.calls()[0][1], 'fg')

    @needs_capstone
    def test_fault_comes_back_as_an_error(self):
        # a bad `this` (0x10): the guarded call returns the fault instead of crashing the game
        original = self.fake.link._op

        def op(name, args):
            if name == 'call':
                self.fake.link.calls.append(args)
                return {'ok': False, 'fault': {'code': '0xc0000005', 'address': '0x00500000', 'access': 'read',
                                               'target': '0x00000020'}, 'scriptErrors': 0}
            return original(name, args)
        self.fake.link._op = op
        reply = self.get('Actor::GetLevel', this='0x00000010')
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['error']['code'], 'CALL_FAULTED')
        self.assertEqual(self.calls()[0][1], 'ig')


ENGINE_MAP = BRIDGE / 'state' / 'engine_map.json'
CLEAN_EXE = os.environ.get('FNV_CLEAN_EXE') or 'C:/Users/Ajang/.fnvdecomp/FalloutNV.exe'


@needs_capstone
@unittest.skipUnless(ENGINE_MAP.is_file() and Path(CLEAN_EXE).is_file(), 'needs the engine map and a clean exe')
class EngineGetRealMapTests(unittest.TestCase):
    """H1 on the real engine map and clean exe: the audit's examples are refused, Actor::GetLevel still runs."""

    @classmethod
    def setUpClass(cls):
        from geck_mcp import fnv_engine
        from geck_mcp.fnv_deep import CleanImage
        cls.engine = fnv_engine.load(ENGINE_MAP)
        cls.image = CleanImage(CLEAN_EXE)

    def setUp(self):
        from geck_mcp.fnv_control import ControlTools
        self.fake = FakeLive(engine_control='read')
        self.addCleanup(self.fake.close)
        self.fake.live._engine = self.engine
        agent = SimpleNamespace(live=self.fake.live, deep=SimpleNamespace(_image=lambda required=True: self.image),
                                describe_pointers=lambda pointers: {})
        self.control = ControlTools(agent)

    def get(self, function, this, args):
        return self.fake.live.run('fnv_engine_get', lambda: self.control.engine_get(function, this, args), 'read')

    def test_audit_examples_are_refused(self):
        cases = [('0x00476930', [0, '0x011DEA3C']),      # NiMatrix3::GetRow
                 ('0x00439F50', [0, '0x011DEA3C']),      # NiMatrix3::GetCol
                 ('0x00595EA0', [])]                     # Sky::GetSunriseBegin (global 0x011CA9E8)
        for function, args in cases:
            reply = self.get(function, '0x01000000', args)
            self.assertEqual((reply.get('error') or {}).get('code'), 'MAY_WRITE', (function, reply.get('error')))
        self.assertEqual([op for op in self.fake.link.ops if op[0] == 'call'], [])

    def test_get_level_runs_guarded(self):
        reply = self.get('Actor::GetLevel', '0x00000010', [])
        self.assertTrue(reply['ok'], reply.get('error'))
        self.assertEqual([args[:2] for name, args in self.fake.link.ops if name == 'call'], [['87f9f0', 'ig']])


class PerkEntriesGateTests(unittest.TestCase):
    """M1: fnv_perk_entries start/stop/evaluate install or remove engine hooks: kind act (D2); read stays read."""

    def test_hook_actions_are_act(self):
        # the registration invokes with the default kind 'read' and its action; the table makes the hook actions act
        for action in ('start', 'stop', 'evaluate'):
            self.assertEqual(gated_kind('fnv_perk_entries', action, 'read'), 'act', action)
        self.assertEqual(gated_kind('fnv_perk_entries', 'read', 'read'), 'read')
        self.assertNotEqual(FNV_LIVE_TOOLS['fnv_perk_entries'][0], 'read')

    def test_start_is_refused_before_any_op_when_read(self):
        with FakeLive(engine_control='read') as fake:
            ran = []
            reply = fake.live.run('fnv_perk_entries', lambda: ran.append('start'),
                                  gated_kind('fnv_perk_entries', 'start', 'read'))
            self.assertEqual(reply['error']['code'], 'CONTROL_DISABLED')
            self.assertEqual((ran, fake.link.ops), ([], []))

    def test_registration_passes_the_action(self):
        import ast
        source = (BRIDGE / 'geck_mcp' / 'fnv_live_tools.py').read_text(encoding='utf-8')
        fn = next(n for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == 'fnv_perk_entries')
        call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, 'id', None) == 'invoke')
        action = next((k.value for k in call.keywords if k.arg == 'action'), None)
        self.assertIsInstance(action, ast.Name)
        self.assertEqual(action.id, 'action')


if __name__ == '__main__':
    unittest.main()
