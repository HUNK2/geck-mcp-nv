"""Release plan Phase 7 (L7): the engine companion DLL is built with ASLR and DEP, and the build cache reuses a
built file only when it still has the digest its last build recorded (a changed file is rebuilt, not installed)."""
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine_host import needs_zig, zig_path  # noqa: E402
from geck_mcp import fnv_link  # noqa: E402

DYNAMIC_BASE, NX_COMPAT = 0x40, 0x100


def dll_characteristics(path):
    data = Path(path).read_bytes()
    pe = struct.unpack_from('<I', data, 0x3C)[0]
    return struct.unpack_from('<H', data, pe + 24 + 70)[0]


@needs_zig
class EngineBuildTrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.receipt = fnv_link.build(self.tmp.name, zig_path())

    def test_aslr_and_dep(self):
        flags = dll_characteristics(self.receipt['dll'])
        self.assertTrue(flags & DYNAMIC_BASE, 'no ASLR (DYNAMIC_BASE)')
        self.assertTrue(flags & NX_COMPAT, 'no DEP (NX_COMPAT)')

    def test_an_unchanged_build_is_reused(self):
        dll = Path(self.receipt['dll'])
        before = dll.stat().st_mtime_ns
        again = fnv_link.build(self.tmp.name, zig_path())
        self.assertEqual(again['sha256'], self.receipt['sha256'])
        self.assertEqual(dll.stat().st_mtime_ns, before)

    def test_a_changed_dll_is_rebuilt(self):
        dll = Path(self.receipt['dll'])
        dll.write_bytes(dll.read_bytes() + b'tampered')
        tampered = fnv_link.file_digest(dll)
        again = fnv_link.build(self.tmp.name, zig_path())
        self.assertNotEqual(again['sha256'], tampered)
        self.assertEqual(again['sha256'], fnv_link.file_digest(dll))
        self.assertFalse(dll.read_bytes().endswith(b'tampered'))

    def test_a_dll_without_a_receipt_is_rebuilt(self):
        dll = Path(self.receipt['dll'])
        (dll.parent / 'build.json').unlink()
        dll.write_bytes(dll.read_bytes() + b'planted')
        again = fnv_link.build(self.tmp.name, zig_path())
        self.assertFalse(dll.read_bytes().endswith(b'planted'))
        self.assertEqual(again['sha256'], fnv_link.file_digest(dll))


if __name__ == '__main__':
    unittest.main()
