"""Offline tests: the .fos save reader/diff (fnv_save) and the pure parts of the engine AI tools (fnv_engine_ai)."""
import struct
import tempfile
import unittest
from pathlib import Path

from geck_mcp import fnv_save
from geck_mcp.fnv_engine_ai import (ENTRY_POINTS, FORMULA_ARGS, EngineAITools)


# ------------------------------------------------------------------------------------------- save writer (test only)
def sp(data):
    return data + b'|'


def s_str(text):
    raw = text.encode('latin-1')
    return sp(struct.pack('<H', len(raw))) + (sp(raw) if raw else b'')


def s_count(n):
    if n < 0x40:
        return sp(bytes([n << 2]))
    return sp(struct.pack('<H', (n << 2) | 1))


def s_form(index):
    return sp(bytes([(index >> 16) & 0xFF, (index >> 8) & 0xFF, index & 0xFF]))


def quest_body(stage_done, objective_state, variable):
    body = sp(bytes([0x31]))  # quest flags (0x2)
    body += s_count(1) + sp(bytes([10])) + sp(bytes([stage_done])) + s_count(1) + sp(bytes([0])) + sp(bytes([0]))
    body += s_count(1) + sp(struct.pack('<I', 3)) + sp(struct.pack('<d', variable)) + sp(b'\0') + sp(b'\0')
    body += s_count(1) + sp(struct.pack('<I', 10)) + sp(struct.pack('<I', objective_state))
    return body


def change(index, flags, type_index, body):
    return bytes([(index >> 16) & 0xFF, (index >> 8) & 0xFF, index & 0xFF]) + struct.pack('<IBB', flags, type_index, 27) + \
        bytes([len(body)]) + body


def make_save(path, quest_stage_done=1, objective_state=1, variable=2.0, global_value=0.5, ref_x=100.0):
    header = sp(struct.pack('<I', 0x30)) + sp(b'ENGLISH'.ljust(64, b'\0')) + sp(struct.pack('<I', 1)) + \
        sp(struct.pack('<I', 1)) + sp(struct.pack('<I', 7)) + s_str('Courier') + s_str('Drifter') + \
        sp(struct.pack('<I', 3)) + s_str('Goodsprings') + s_str('000.01.00')
    plugins = sp(bytes([2])) + s_str('FalloutNV.esm') + s_str('Test.esp')
    ids = [0x01000ABC, 0x00000038, 0x01000DEF]  # quest, GameHour global, a reference
    globals1 = []
    loc = sp(struct.pack('<I', 0xFF000010)) + s_form(0) + sp(struct.pack('<i', -17)) + sp(struct.pack('<i', 0)) + \
        s_form(0) + sp(struct.pack('<3f', 1.0, 2.0, 3.0))
    globals1.append((1, loc))
    globals1.append((3, s_count(1) + s_form(2) + sp(struct.pack('<f', global_value))))
    # QUST is compact type 9, REFR is 0 (moved 0x2 -> initial data "reference location", 27 bytes + '|')
    ref_body = bytes([0, 0, 0]) + struct.pack('<6f', ref_x, 5.0, 6.0, 0.0, 0.0, 1.5) + b'|'
    changes = [change(1, 0xE0000002, 9, quest_body(quest_stage_done, objective_state, variable)),
               change(3, 0x00000002, 0, ref_body)]
    out = bytearray(b'FO3SAVEGAME' + struct.pack('<I', len(header)) + header + b'\x10\x20\x30' + bytes([27]) +
                    struct.pack('<I', len(plugins)) + plugins)
    section = len(out)
    out += b'\0' * 110
    g1 = len(out)
    for t, body in globals1:
        out += struct.pack('<II', t, len(body)) + body
    cf = len(out)
    for c in changes:
        out += c
    g2 = len(out)
    ida = len(out)
    out += struct.pack('<I', len(ids)) + struct.pack('<%dI' % len(ids), *ids)
    out += struct.pack('<I', 0)
    hist = len(out)
    out += struct.pack('<I', 0)
    struct.pack_into('<8I', out, section, ida, hist, g1, cf, g2, len(globals1), 0, len(changes))
    path.write_bytes(bytes(out))
    return path


class SaveReader(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_header_plugins_globals_and_quest(self):
        save = fnv_save.Save(make_save(self.tmp / 'a.fos'))
        self.assertEqual(save.header['playerName'], 'Courier')
        self.assertEqual(save.header['level'], 3)
        self.assertEqual(save.plugins, ['FalloutNV.esm', 'Test.esp'])
        self.assertEqual(save.player_location()['gridX'], -17)
        self.assertEqual(save.global_variables(), {'00000038': 0.5})
        quest = save.decode(save.changes[0])
        self.assertTrue(quest['complete'])
        self.assertTrue(quest['quest']['running'])
        self.assertFalse(quest['quest']['completed'])
        self.assertEqual(quest['quest']['stages'][0], {'stage': 10, 'done': True, 'logEntries': [{'logEntry': 0, 'date': None}]})
        self.assertEqual(quest['quest']['objectives'], [{'index': 10, 'state': 1}])
        self.assertEqual(quest['quest']['scriptVariables'], {3: 2.0})
        ref = save.decode(save.changes[1])
        self.assertEqual(ref['initial']['kind'], 'reference location')
        self.assertEqual(ref['initial']['position']['x'], 100.0)
        self.assertEqual(save.plugin_of(0x01000ABC), 'Test.esp')

    def test_diff_reports_quest_stage_global_and_position(self):
        a = make_save(self.tmp / 'a.fos')
        b = make_save(self.tmp / 'b.fos', quest_stage_done=0, objective_state=2, global_value=0.75, ref_x=250.0)
        d = fnv_save.diff(a, b)
        self.assertEqual(d['counts'], {'added': 0, 'removed': 0, 'changed': 2})
        fields = {f['field']: (f['before'], f['after']) for e in d['changed'] for f in e.get('fields', [])}
        self.assertEqual(fields['quest.stages[0].done'], (True, False))
        self.assertEqual(fields['quest.objectives[0].state'], (1, 2))
        self.assertEqual(fields['initial.position.x'], (100.0, 250.0))
        self.assertEqual((d['globalVariables'][0]['before'], d['globalVariables'][0]['after']), (0.5, 0.75))
        self.assertEqual(fnv_save.diff(a, a)['counts'], {'added': 0, 'removed': 0, 'changed': 0})

    def test_rejects_non_saves(self):
        p = self.tmp / 'x.fos'
        p.write_bytes(b'not a save')
        with self.assertRaises(fnv_save.SaveFormatError):
            fnv_save.Save(p)

    def test_buffer_count_and_created_form_ids(self):
        b = fnv_save.Buffer(s_count(5) + s_count(300) + sp(bytes([0x80, 0x00, 0x05])), [0x11])
        self.assertEqual(b.count(), 5)
        self.assertEqual(b.count(), 300)
        self.assertEqual(b.form(), 0xFF000005)

    def test_flag_names_by_type(self):
        self.assertEqual(fnv_save.flag_names('QUST', 0xE0000002), ['quest flags', 'objectives', 'script variables', 'stages'])
        self.assertIn('life state', fnv_save.flag_names('ACHR', 0x400))
        self.assertIn('item data', fnv_save.flag_names('REFR', 0x400))


class EngineAI(unittest.TestCase):
    def test_tables_have_engine_sizes(self):
        self.assertEqual(len(FORMULA_ARGS), 25)  # the detection formula takes 25 stack arguments
        self.assertEqual(len(ENTRY_POINTS), 74)  # ms_kEntryPointTable has 74 entries
        self.assertEqual(ENTRY_POINTS[9], ('Adjust Experience Points', 1))

    def test_hostility_rule(self):
        cases = [(3, 3, False, False, 'frenzied'), (2, 0, False, False, 'so hostile'), (1, 0, False, False, 'not hostile'),
                 (1, 1, False, False, 'so hostile'), (0, 1, False, False, 'never starts'),
                 (1, 0, True, False, 'already in combat'), (1, 0, False, True, 'crime')]
        for aggr, rel, forced, angry, expect in cases:
            with self.subTest(aggr=aggr, rel=rel, forced=forced, angry=angry):
                self.assertIn(expect, EngineAITools._hostility_rule(aggr, rel, forced, angry, False, False, False))

    def test_detection_explanation_mentions_cone_and_range(self):
        out = {'observer': {'highProcess': True},
               'committed': {'observerDetectsTarget': {'state': 'lost', 'score': -12, 'lineOfSightInViewCone': False}},
               'settings': {'fSneakSeenMin': 0.0, 'fSneakNoticedMin': -20.0, 'fDetectionViewCone': 190.0,
                            'fSneakMaxDistance': 2500.0, 'fSneakExteriorDistanceMult': 2.0},
               'recomputed': {'level': -12, 'formula': {'inputs': {'distance': 6000.0, 'outdoors': True,
                                                                   'targetSneaking': True, 'sneakSkill': 40}}}}
        why = ' '.join(EngineAITools._explain_detection(out))
        self.assertIn('view cone', why)
        self.assertIn('beyond the detection range 5000', why)
        self.assertIn('Sneak skill 40', why)


if __name__ == '__main__':
    unittest.main()
