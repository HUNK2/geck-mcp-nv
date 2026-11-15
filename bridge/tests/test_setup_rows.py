"""Release plan Task 4.4 (audit M12): fnv_setup_check covers what a stranger trips on.

New rows: the fnv block (first; without it the other rows say "not set", never None), the symbol database, the
proof folder (optional), capstone, the GECK helper (only when GECK settings are set) and a large-address-aware game
exe. Every row keeps the shape: check, state, detail, fix."""
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp import fnv_setup  # noqa: E402


def fnv_settings(folder, **kw):
    base = dict(mo2_root=None, mo2_profile=None, mo2_executable='NVSE', zig=None, engine_map=Path(folder) / 'none.json',
                decomp_root=None, clean_exe=None, engine_control='read', symdb=None)
    base.update(kw)
    return SimpleNamespace(**base)


def project(folder, fnv, **kw):
    return SimpleNamespace(game_root=Path(folder) / 'game', fnv=fnv, state_dir=Path(folder) / 'state', plugin=None,
                           helper=None, **kw)


def pe(path, large_address_aware):
    """A minimal PE header: e_lfanew at 0x3C, 'PE\\0\\0', then the file header with Characteristics."""
    data = bytearray(0x200)
    data[0:2] = b'MZ'
    struct.pack_into('<I', data, 0x3C, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<H', data, 0x80 + 4 + 18, 0x0102 | (0x0020 if large_address_aware else 0))
    Path(path).write_bytes(bytes(data))


def run(p):
    with mock.patch.dict(os.environ, {'GECK_MCP_ZIG': '', 'FNVSYM_ROOT': ''}):
        return fnv_setup.setup_check(p)


def row(report, name):
    return next(c for c in report['checks'] if c['check'] == name)


class SetupRowTests(unittest.TestCase):
    def test_without_an_fnv_block_it_is_the_first_row_and_nothing_says_none(self):
        with tempfile.TemporaryDirectory() as folder:
            report = run(project(folder, None))
        self.assertEqual(report['checks'][0]['check'], 'fnv block')
        self.assertEqual(report['checks'][0]['state'], 'missing')
        self.assertIn('example-fnv.json', report['checks'][0]['fix'])
        for c in report['checks']:
            self.assertNotIn('None', c['detail'], c)
            self.assertEqual(set(c), {'check', 'state', 'detail', 'fix'})

    def test_with_an_fnv_block_the_first_row_is_ok(self):
        with tempfile.TemporaryDirectory() as folder:
            report = run(project(folder, fnv_settings(folder)))
        self.assertEqual(report['checks'][0], {'check': 'fnv block', 'state': 'ok', 'detail': 'present', 'fix': None})
        for c in report['checks']:
            self.assertNotIn('None', c['detail'], c)

    def test_the_new_rows_are_there_and_optional_where_they_should_be(self):
        with tempfile.TemporaryDirectory() as folder:
            report = run(project(folder, fnv_settings(folder)))
        self.assertEqual(row(report, 'symbol database')['state'], 'optional')
        self.assertEqual(row(report, 'proof folder')['state'], 'optional')
        self.assertIn('FNVSYM_ROOT', row(report, 'proof folder')['fix'])
        self.assertIn(row(report, 'capstone')['state'], ('ok', 'optional'))
        self.assertNotIn('GECK helper', [c['check'] for c in report['checks']])

    def test_the_proof_folder_and_symbol_database_when_present(self):
        with tempfile.TemporaryDirectory() as folder:
            symdb = Path(folder) / 'symdb.sqlite'
            symdb.write_bytes(b'')
            p = project(folder, fnv_settings(folder, symdb=symdb))
            with mock.patch.dict(os.environ, {'GECK_MCP_ZIG': '', 'FNVSYM_ROOT': folder}):
                report = fnv_setup.setup_check(p)
        self.assertEqual(row(report, 'symbol database')['state'], 'ok')
        self.assertEqual(row(report, 'proof folder')['state'], 'ok')

    def test_the_helper_row_only_with_geck_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            p = project(folder, fnv_settings(folder))
            p.plugin, p.helper = 'Mine.esp', Path(folder) / 'Probe.Mcp.exe'
            report = run(p)
            self.assertEqual(row(report, 'GECK helper')['state'], 'missing')
            self.assertIn('build_helper.py', row(report, 'GECK helper')['fix'])
            p.helper.write_bytes(b'MZ')
            self.assertEqual(row(run(p), 'GECK helper')['state'], 'ok')

    def test_large_address_aware(self):
        with tempfile.TemporaryDirectory() as folder:
            game = Path(folder) / 'game'
            game.mkdir()
            p = project(folder, fnv_settings(folder))
            pe(game / 'FalloutNV.exe', False)
            self.assertEqual(row(run(p), 'large address aware')['state'], 'optional')
            pe(game / 'FalloutNV.exe', True)
            self.assertEqual(row(run(p), 'large address aware')['state'], 'ok')

    def test_large_address_aware_reads_the_header(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / 'a.exe'
            pe(exe, True)
            self.assertTrue(fnv_setup.large_address_aware(exe))
            pe(exe, False)
            self.assertFalse(fnv_setup.large_address_aware(exe))
            exe.write_bytes(b'not a pe')
            self.assertIsNone(fnv_setup.large_address_aware(exe))


if __name__ == '__main__':
    unittest.main()
