"""Offline tests for the phase 2 tools: reverse references, profile edits, hook-set helpers, world helpers."""
import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from geck_mcp import fnv_records
from geck_mcp.fnv_control import ControlTools, _stack_words
from geck_mcp.fnv_world import _f32, address_space


def sub(tag, data):
    return tag.encode() + struct.pack('<H', len(data)) + data


def record(tag, form_id, *subs):
    body = b''.join(subs)
    return tag.encode() + struct.pack('<IIIIHH', len(body), 0, form_id, 0, 15, 0) + body


def plugin(records, masters=('FalloutNV.esm',)):
    head = b''.join(sub('MAST', m.encode() + b'\0') + sub('DATA', b'\0' * 8) for m in masters)
    tes4 = b'TES4' + struct.pack('<IIIIHH', len(head), 0, 0, 0, 15, 0) + head
    body = b''.join(records)
    group = b'GRUP' + struct.pack('<I', 24 + len(body)) + b'TEST' + struct.pack('<i', 0) + b'\0' * 8
    return tes4 + group + body


class ReferenceScan(unittest.TestCase):
    def test_form_fields_conditions_and_lists(self):
        ctda = bytearray(28)
        struct.pack_into('<H', ctda, 8, 0x48)          # GetIsID: parameter 1 is a form
        struct.pack_into('<I', ctda, 12, 0x00104E84)
        struct.pack_into('<I', ctda, 16, 0x0000000F)   # parameter 2 is not a form for this function
        ctda_int = bytearray(28)
        struct.pack_into('<H', ctda_int, 8, 0x0E)      # GetActorValue: an actor value index, never a form
        struct.pack_into('<I', ctda_int, 12, 0x0000000F)
        lvlo = struct.pack('<HHIhH', 1, 0, 0x0000000F, 3, 0)
        data = plugin([
            record('REFR', 0x01000800, sub('NAME', struct.pack('<I', 0x00104E84))),
            record('INFO', 0x01000801, sub('CTDA', bytes(ctda)), sub('CTDA', bytes(ctda_int))),
            record('LVLI', 0x01000802, sub('LVLO', lvlo)),
            record('MISC', 0x01000803, sub('DATA', struct.pack('<I', 0x00104E84))),  # DATA is never a form
        ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'Test.esp'
            path.write_bytes(data)
            known = {0x00104E84, 0x0000000F}
            rows = fnv_records.scan_references(path, lambda raw: raw in known, {0x48: [True, False], 0x0E: [False]})
        found = {(t, tag, to) for _, t, _, tag, to in rows}
        self.assertEqual(found, {('REFR', 'NAME', 0x00104E84), ('INFO', 'CTDA', 0x00104E84), ('LVLI', 'LVLO', 0x0000000F)})

    def test_how_labels(self):
        self.assertEqual(fnv_records._how('FLST', 'LNAM'), 'form list entry')
        self.assertEqual(fnv_records._how('INFO', 'CTDA'), 'dialogue condition')
        self.assertEqual(fnv_records._how('ACHR', 'NAME'), 'placed reference of it')


class ProfileSet(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        profile = root / 'mo2' / 'profiles' / 'Dev'
        profile.mkdir(parents=True)
        (root / 'mo2' / 'mods' / 'ModA').mkdir(parents=True)
        (profile / 'modlist.txt').write_text('# header\n+ModA\n', encoding='utf-8')
        (profile / 'plugins.txt').write_text('# header\nFalloutNV.esm\nModA.esp\n', encoding='utf-8')
        (profile / 'loadorder.txt').write_text('# header\nFalloutNV.esm\nModA.esp\n', encoding='utf-8')
        (root / 'game' / 'Data').mkdir(parents=True)
        self.profile = profile
        project = SimpleNamespace(fnv=SimpleNamespace(mo2_root=str(root / 'mo2'), mo2_profile='Dev'),
                                  state_dir=str(root / 'state'), game_root=str(root / 'game'))
        self.tools = fnv_records.RecordsTools(SimpleNamespace(project=project))

    def tearDown(self):
        self._tmp.cleanup()

    def test_disable_and_enable_with_backup(self):
        out = self.tools.profile_set(mods={'ModA': False}, plugins={'ModA.esp': False})
        self.assertEqual(len(out['changed']), 2)
        self.assertIn('-ModA', (self.profile / 'modlist.txt').read_text(encoding='utf-8').splitlines())
        self.assertNotIn('ModA.esp', (self.profile / 'plugins.txt').read_text(encoding='utf-8').splitlines())
        self.assertTrue(Path(out['backup'], 'modlist.txt').exists())
        again = self.tools.profile_set(plugins={'ModA.esp': True})
        self.assertEqual(again['changed'][0]['now'], 'active')

    def test_dry_run_errors_and_running_game(self):
        out = self.tools.profile_set(mods={'Missing': True}, plugins={'FalloutNV.esm': False}, dry_run=True)
        self.assertEqual(len(out['errors']), 2)
        self.assertIsNone(out['backup'])
        game = SimpleNamespace(process=lambda: [1234])
        with self.assertRaises(fnv_records.DataError):
            self.tools.profile_set(mods={'ModA': False}, game=game)


class Helpers(unittest.TestCase):
    def test_stack_words_by_convention(self):
        layout = [{'where': 'stack', 'index': 0, 'kind': 'pointer'}, {'where': 'stack', 'index': 1, 'kind': 'double'}]
        self.assertEqual(_stack_words(layout), 3)
        self.assertIsNone(_stack_words([{'where': None}]))

    def test_tracepoint_conditions(self):
        call = {'this': {'ptr': '0x00001000'}, 'args': [{'value': 5}, {'value': '0x0000ABCD'}], 'caller': {'address': '0x00401000'}}
        self.assertTrue(ControlTools._matches(call, [(2, 5, {'on': 'param0'}), (3, 0xABCD, {'on': 'param1'})]))
        self.assertFalse(ControlTools._matches(call, [(1, 0x2000, {'on': 'this'})]))

    def test_vats_percent_in_single_precision(self):
        self.assertEqual(int(_f32(_f32(0.95) * 100.0)), 95)  # the menu shows 95, not 94
        self.assertEqual(int(_f32(_f32(0.4907) * 100.0)), 49)

    def test_address_space_of_this_process(self):
        space = address_space(os.getpid())
        self.assertIsNotNone(space)
        self.assertEqual(space['limit'], 2048)
        self.assertGreater(space['free'] + space['used'], 0)


if __name__ == '__main__':
    unittest.main()
