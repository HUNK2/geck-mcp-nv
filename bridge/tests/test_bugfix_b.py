"""Regression tests for the native-area bugs of the bug log that the bridge side can check offline (B4, B5, B8,
B10, B11). The C fixes (B1, B2, B6, B7, B9, B12-B17) run under the engine suites with GECK_MCP_ZIG set and in the
game."""
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp import fnv_code, fnv_flow, fnv_native  # noqa: E402
from geck_mcp.fnv_conv import ConvError  # noqa: E402


class FakeLink:
    def __init__(self, fail_at=()):
        self.patches, self.fail_at = [], set(fail_at)

    def one(self, *op, **kw):
        if op[:2] == ('code', 'patch'):
            address = int(op[2], 16)
            self.patches.append(address)
            if address in self.fail_at:
                return {'ok': False, 'error': 'busy'}
            return {'ok': True, 'old': '00'}
        return {'ok': True}


class UnswapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.orig_work = fnv_code.CodeTools.work
        fnv_code.CodeTools.work = property(lambda s: self.work)

    def tearDown(self):
        fnv_code.CodeTools.work = self.orig_work
        self.tmp.cleanup()

    def make(self, link):
        tools = fnv_code.CodeTools(SimpleNamespace(ready=lambda: None, link=link))
        tools._game_id = lambda: 'g'
        tools.swaps[0x401000] = {'function': 'F', 'blob': 0x900000, 'old': 'e9000000', 'size': 9,
                                 'sites': [{'at': 0x402001, 'kind': 'call', 'old': '11'},
                                           {'at': 0x403001, 'kind': 'call', 'old': '22'}]}
        tools._save_swaps()
        return tools

    def test_partial_restore_keeps_what_is_still_redirected(self):  # B5
        tools = self.make(FakeLink(fail_at={0x403001}))
        with self.assertRaises(ConvError) as caught:
            tools.unswap()
        self.assertEqual(caught.exception.code, 'WRITE_FAILED')
        kept = tools.swaps[0x401000]
        self.assertEqual([s['at'] for s in kept['sites']], [0x403001])
        self.assertIsNone(kept['old'])  # the entry came back, so it is no longer listed as patched

    def test_full_restore_forgets_the_swap(self):
        tools = self.make(FakeLink())
        out = tools.unswap()
        self.assertEqual(out['restored'], ['F'])
        self.assertEqual(tools.swaps, {})


class ExportTests(unittest.TestCase):
    def test_export_refuses_functions_shorter_than_a_jump(self):  # B8
        tools = fnv_code.CodeTools(SimpleNamespace(ready=lambda: None))
        tools._target = lambda f: (0x401000, {}, {'size': 3})
        with self.assertRaises(ConvError) as caught:
            tools.export('Tiny')
        self.assertEqual(caught.exception.code, 'TOO_SHORT')


class ExportLookupTests(unittest.TestCase):
    def test_reads_the_name_count(self):  # B11
        base = 0x10000000
        mem = {}

        def put(addr, data):
            mem[addr] = data
        put(base + 0x3C, struct.pack('<I', 0x80))
        put(base + 0x80 + 0x78, struct.pack('<II', 0x1000, 0x100))
        directory = bytearray(40)
        struct.pack_into('<IIIII', directory, 20, 1, 1, 0x1100, 0x1200, 0x1300)  # functions, names, three tables
        put(base + 0x1000, bytes(directory))
        put(base + 0x1200, struct.pack('<I', 0x1400))
        put(base + 0x1400, b'fnvmcp_flow_slot\0'.ljust(64, b'\0'))
        put(base + 0x1300, struct.pack('<H', 0))
        put(base + 0x1100, struct.pack('<I', 0x2222))

        def reads(pairs):
            return [mem.get(a) for a, _ in pairs]
        tools = fnv_native.NativeTools(SimpleNamespace(reads=reads))
        self.assertEqual(tools._export_in_memory(base, 'fnvmcp_flow_slot'), base + 0x2222)


class FlowFastcallTests(unittest.TestCase):
    def test_fastcall_fills_ecx_edx_and_stack(self):  # B10
        writes = []
        flow = fnv_flow.FlowTools.__new__(fnv_flow.FlowTools)
        flow.native = SimpleNamespace(flow_slot=lambda: 0x5000,
                                      loaded={'fnvmcp_flowcall': {'exports': {'fnvmcp_flow_version': '0x1'}}})
        flow.live = SimpleNamespace(_signature=lambda a: (None, {}, 'int __fastcall F(int a, int b, int c)', None),
                                    link=SimpleNamespace(write=lambda a, b: writes.append((a, b))))
        flow._arm_call({'addr': 0x401000}, (None, [1, 2, 3]))
        body = dict(writes)
        function, ecx, nargs = struct.unpack_from('<III', body[0x5004])
        self.assertEqual((function, ecx, nargs), (0x401000, 1, 1))
        self.assertEqual(struct.unpack_from('<I', body[0x5004], 12)[0], 3)
        self.assertEqual(struct.unpack('<I', body[0x5000 + 4 * 18])[0], 2)


if __name__ == '__main__':
    unittest.main()
