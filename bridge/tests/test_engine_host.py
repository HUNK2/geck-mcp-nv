"""The shared engine test host: where Zig is found (S0.1) and the seam builds of the companion (S0.2)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine_host  # noqa: E402


def write_project(folder: Path, name: str, zig: str | None) -> None:
    fnv = {'engine_control': 'full'}
    if zig is not None:
        fnv['zig'] = zig
    (folder / name).write_text(json.dumps({'schema_version': 1, 'fnv': fnv}), encoding='utf-8')


class ZigPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.zig = self.root / 'zig.exe'
        self.zig.write_bytes(b'')
        # FNV_MCP_ZIG wins over the old name these tests set; a machine (or CI) that has it set must not leak in.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('FNV_MCP_ZIG', None)

    def test_new_env_name_wins(self):
        with mock.patch.dict(os.environ, {'FNV_MCP_ZIG': str(self.zig), 'GECK_MCP_ZIG': str(self.root / 'old.exe')}):
            self.assertEqual(engine_host.zig_path(self.root), str(self.zig))

    def test_zig_path_prefers_env(self):
        write_project(self.root, 'a.windows.json', str(self.root / 'other.exe'))
        with mock.patch.dict(os.environ, {'GECK_MCP_ZIG': str(self.zig)}):
            self.assertEqual(engine_host.zig_path(self.root), str(self.zig))

    def test_zig_path_reads_project_file(self):
        write_project(self.root, 'a.windows.json', None)
        write_project(self.root, 'b.windows.json', str(self.root / 'missing.exe'))
        write_project(self.root, 'c.windows.json', str(self.zig))
        with mock.patch.dict(os.environ, {'GECK_MCP_ZIG': ''}):
            self.assertEqual(Path(engine_host.zig_path(self.root)), self.zig)

    def test_zig_path_expands_home(self):
        write_project(self.root, 'a.windows.json', '~/zig.exe')
        with mock.patch.dict(os.environ, {'GECK_MCP_ZIG': '', 'USERPROFILE': str(self.root), 'HOME': str(self.root)}):
            self.assertEqual(Path(engine_host.zig_path(self.root)), self.zig)

    def test_defines_string_is_one_define(self):
        from geck_mcp import fnv_link
        self.assertEqual(fnv_link.check_defines('FNVMCP_TEST_SEAM'), ['FNVMCP_TEST_SEAM'])
        self.assertEqual(fnv_link.check_defines(['B', 'A', 'A']), ['A', 'B'])
        for bad in ('éX', '1X', 'A-B', ''):
            with self.assertRaises(fnv_link.LinkError, msg=bad):
                fnv_link.check_defines([bad])

    def test_zig_path_none_without_any(self):
        write_project(self.root, 'a.windows.json', str(self.root / 'missing.exe'))
        (self.root / 'broken.json').write_text('{not json', encoding='utf-8')
        with mock.patch.dict(os.environ, {'GECK_MCP_ZIG': ''}):
            self.assertIsNone(engine_host.zig_path(self.root))

    def test_engine_tests_need_windows(self):
        with mock.patch.dict(os.environ, {'GECK_MCP_ZIG': str(self.zig)}):
            self.assertIsNone(engine_host.engine_zig('posix', self.root))
            self.assertEqual(engine_host.engine_zig('nt', self.root), str(self.zig))

    def test_needs_zig_is_a_skip_decorator(self):
        @engine_host.needs_zig
        class Probe(unittest.TestCase):
            def test_x(self):
                pass
        self.assertTrue(issubclass(Probe, unittest.TestCase))


class ImportAloneTests(unittest.TestCase):
    def test_test_modules_import_without_discover(self):
        # `python -m unittest tests.test_x` from bridge does not put tests/ on sys.path; every module that imports
        # engine_host must add its own folder.
        import subprocess
        bridge = Path(__file__).resolve().parents[1]
        users = sorted(p.stem for p in (bridge / 'tests').glob('test_*.py')
                       if 'from engine_host import' in p.read_text(encoding='utf-8'))
        self.assertGreater(len(users), 5)
        broken = []
        for name in users:
            r = subprocess.run([sys.executable, '-c', 'import tests.%s' % name], cwd=bridge, capture_output=True,
                               text=True, timeout=120, env=dict(os.environ, PYTHONPATH=''))
            if r.returncode:
                broken.append((name, r.stderr.strip().splitlines()[-1:]))
        self.assertEqual(broken, [])


SEAM_DEFINE = 'FNVMCP_TEST_SEAM'


@engine_host.needs_zig
class SeamBuildTests(unittest.TestCase):
    """S0.2: a seam build is a separate build; the product DLL is the same bytes with or without the seam lines."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name)
        cls.product = engine_host.build(cls.state)
        cls.seam = engine_host.build(cls.state, defines=(SEAM_DEFINE,))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_seam_build_has_own_identity(self):
        self.assertNotEqual(self.product['build_id'], self.seam['build_id'])
        self.assertNotEqual(Path(self.product['dll']).parent, Path(self.seam['dll']).parent)
        self.assertEqual(self.seam['defines'], [SEAM_DEFINE])
        self.assertNotIn('defines', self.product)
        self.assertNotEqual(engine_host.pe_code_digest(self.product['dll']),
                            engine_host.pe_code_digest(self.seam['dll']))

    def test_product_build_id_ignores_empty_defines(self):
        again = engine_host.build(self.state, defines=())
        self.assertEqual(again['build_id'], self.product['build_id'])

    def test_product_build_unchanged_by_seam_source(self):
        # Copy the native tree, drop every seam line, compile the product DLL from the copy with the same command,
        # and compare bytes with the real tree's product DLL.
        import re
        import shutil
        from geck_mcp import fnv_link
        copy = self.state / 'noseam' / 'native'
        shutil.copytree(fnv_link.NATIVE, copy)
        removed = 0
        for path in (copy / 'engine').glob('*.c'):
            text = path.read_text(encoding='utf-8')
            new = re.sub(r'(?s)#ifdef FNVMCP_TEST_SEAM.*?#endif /\* FNVMCP_TEST_SEAM \*/\n', '', text)
            new = re.sub(r'(?m)^[ \t]*SEAM\(\d+\);[^\n]*\n', '', new)
            removed += text != new
            path.write_text(new, encoding='utf-8', newline='')
        self.assertGreater(removed, 0, 'no seam lines found to strip')
        out = self.state / 'noseam' / fnv_link.DLL_NAME  # the DLL name is in the export table
        fnv_link.compile_target(engine_host.zig_path(), copy, 'engine/fnvmcp_engine.c', out, ['-shared'])
        self.assertEqual(engine_host.pe_code_digest(out), engine_host.pe_code_digest(self.product['dll']))

    def test_install_refuses_seam_build(self):
        from geck_mcp import fnv_link
        with self.assertRaises(fnv_link.LinkError) as cm:
            fnv_link.install(self.state / 'mo2', 'Dev', self.seam, self.state)
        self.assertEqual(cm.exception.code, 'TEST_BUILD')
        self.assertFalse((self.state / 'mo2' / 'mods').exists())

    def test_fake_host_reports_seam_address_only_in_seam_build(self):
        for receipt, present in ((self.product, False), (self.seam, True)):
            host = engine_host.StopHost(receipt, Path(receipt['dll']).parent, self.tmp.name, 30)
            try:
                self.assertEqual(bool(host.t.get('seam')), present, receipt['build_id'])
                self.assertTrue(host.link.ping())
            finally:
                host.close()


if __name__ == '__main__':
    unittest.main()
