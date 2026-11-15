"""fnv_code and fnv_plugin_build: the function's line range, relocation maths when the blob is placed, an offline
checkout/edit/build of a real decomp function (when the decomp and its compiler are on this machine), and a plugin
built from the scaffold with the standalone runtime (when Zig is set)."""
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp.fnv_code import DIR32, REL32, CodeTools  # noqa: E402
from geck_mcp.fnv_conv import ConvError  # noqa: E402
from geck_mcp.fnv_plugin_build import PluginTools  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/: engine_host
from engine_host import needs_zig, zig_path  # noqa: E402,F401

DECOMP = Path(r'C:\DecompModGithub\fnv_decomp')
SOURCE = '''// header
// @fnv 0x00401000
int A(int x)
{
    return x + 1;
}

// @fnv 0x00401020
int B(int y)
{
    return y * 2;
}
'''


class RangeAndRelocTests(unittest.TestCase):
    def test_range_covers_the_function_only(self):
        self.assertEqual(CodeTools._range(SOURCE, 0x00401000), (2, 6))
        self.assertEqual(CodeTools._range(SOURCE, 0x00401020), (8, 12))
        self.assertEqual(CodeTools._range(SOURCE, 0x00409999), (None, None))

    def test_relocations_are_bound_at_the_placed_address(self):
        code = bytearray(16)
        link = {'code': code.hex(), 'relocs': [
            {'offset': 1, 'kind': REL32, 'absolute': 0x00401000},       # call an original function
            {'offset': 6, 'kind': DIR32, 'absolute': 0x011F6238},       # a global
            {'offset': 11, 'kind': DIR32, 'blob': 0x0C}]}               # data copied into the blob
        out = CodeTools._final(link, 0x10000000)
        self.assertEqual(struct.unpack_from('<i', out, 1)[0], 0x00401000 - (0x10000000 + 1 + 4))
        self.assertEqual(struct.unpack_from('<I', out, 6)[0], 0x011F6238)
        self.assertEqual(struct.unpack_from('<I', out, 11)[0], 0x1000000C)


def live_for(tmp, resolve):
    engine = SimpleNamespace(resolve=resolve)
    project = SimpleNamespace(state_dir=tmp, fnv=SimpleNamespace(decomp_root=str(DECOMP), mo2_root=tmp,
                                                                 mo2_profile='Dev'))
    return SimpleNamespace(project=project, engine=lambda: engine, game=SimpleNamespace(process=lambda: None),
                           journal=SimpleNamespace(add=lambda e: e))


@unittest.skipUnless((DECOMP / 'build' / 'units.json').is_file() and os.environ.get('FNV_MSVC9_ROOT'),
                     'needs the decomp checkout and its compiler (FNV_MSVC9_ROOT)')
class OfflineBuildTests(unittest.TestCase):
    def test_checkout_edit_build_links_every_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = CodeTools(live_for(tmp, lambda n: 0x00643C20))
            out = tools.checkout('AIFormulas::CalculateSkillValue')
            self.assertIn('float result = base;', out['source'])
            tools.edit('AIFormulas::CalculateSkillValue', 'float result = base;', 'float result = base + 10.0f;')
            built = tools.build('AIFormulas::CalculateSkillValue')
            self.assertIsNone(built['unresolved'])
            self.assertIsNone(built['refused'])
            self.assertGreater(built['size'], built['originalSize'])
            link = json.loads((Path(tmp) / 'fnv' / 'code' / '00643C20' / 'link.json').read_text())
            self.assertTrue(all('absolute' in r or 'blob' in r for r in link['relocs']))
            with self.assertRaises(ConvError):
                tools.edit('AIFormulas::CalculateSkillValue', 'not in the function', 'x')
            # the decomp checkout itself is untouched
            self.assertIn('float result = base;', (DECOMP / 'src/fallout_shared/aiformulas.cpp').read_text())


@needs_zig
class PluginBuildTests(unittest.TestCase):
    def test_scaffold_builds_as_a_standalone_plugin(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = PluginTools(live_for(tmp, lambda n: 0x00401000), zig=zig_path())
            tools.scaffold('my plugin')
            out = tools.build('my plugin')
            self.assertTrue(Path(out['dll']).is_file())
            self.assertEqual(out['hooks'], [])
            plan = (Path(tmp) / 'fnv' / 'plugins' / 'my_plugin' / 'fnvmcp_plan.h').read_text()
            self.assertIn('#define FNVMCP_PLUGIN_NAME "my_plugin"', plan)
            with self.assertRaises(ConvError) as cm:
                tools.scaffold('my plugin')
            self.assertEqual(cm.exception.code, 'EXISTS')


class PluginSharingNoteTests(unittest.TestCase):
    """Release plan Phase 7 (B6): like fnv_code, fnv_plugin_build says a DLL built from decompiled code is for
    local use only."""

    def test_install_and_description_say_local_use(self):
        from geck_mcp import fnv_plugin_build
        self.assertIn('local use', fnv_plugin_build.SHARING_NOTE.lower())
        with tempfile.TemporaryDirectory() as tmp:
            live = live_for(tmp, lambda n: 0x00401000)
            live.game = SimpleNamespace(process=lambda fresh=False: None)
            tools = PluginTools(live, zig='zig')
            folder = Path(tmp) / 'fnv' / 'plugins' / 'my_plugin'
            folder.mkdir(parents=True)
            (folder / 'my_plugin.dll').write_bytes(b'MZ')
            out = tools.install('my plugin')
            self.assertEqual(out.get('note'), fnv_plugin_build.SHARING_NOTE)
        from geck_mcp import fnv_live_tools
        import inspect
        self.assertIn(fnv_plugin_build.SHARING_NOTE.split('.')[0], ' '.join(inspect.getsource(fnv_live_tools).split()))


class CompanionHashTests(unittest.TestCase):
    def test_every_engine_source_is_in_the_build_hash(self):
        # A C file left out of the hash let a change reuse a stale build (seen with w5_code.c).
        from geck_mcp import fnv_link
        names = set(fnv_link.source_hashes())
        engine = {'engine/%s' % p.name for p in (fnv_link.NATIVE / 'engine').glob('*.c')}
        self.assertTrue(engine <= names, engine - names)
        self.assertIn('sdk/fnvmcp_module.h', names)


if __name__ == '__main__':
    unittest.main()
