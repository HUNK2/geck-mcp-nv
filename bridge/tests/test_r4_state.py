"""PLAN-3 exit gate round 4 (docs/audit4/state.md): M2 (undo order targets keyed by the state they restore, not by
the undo line's text) and M3 (fnv_script_hot_swap journals its intent before the swap)."""
import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeLive  # noqa: E402
from geck_mcp import fnv_link  # noqa: E402
from geck_mcp.fnv_live import LiveError  # noqa: E402

REF = 'FF000900'


def ref_entry(*lines, ref=REF, kind='world_edit'):
    return {'kind': kind, 'ref': ref, 'undo': {'op': 'console_ref', 'ref': ref, 'lines': list(lines)}}


class OrderByStateTests(unittest.TestCase):
    """M2: undos that restore one value through different lines must meet."""

    def refused(self, older, newer):
        with FakeLive() as fake:
            live = fake.live
            first = live.journal.add(dict(older))
            live.journal.add(dict(newer))
            r = live.undo(journal_id=first['id'])['results'][0]
            return r.get('reason') == 'undo the later change first'

    def assertMeet(self, a, b):
        self.assertTrue(self.refused(a, b), 'older %r must wait for newer %r' % (a['undo'], b['undo']))
        self.assertTrue(self.refused(b, a), 'older %r must wait for newer %r' % (b['undo'], a['undo']))

    def assertFree(self, a, b):
        self.assertFalse(self.refused(a, b))
        self.assertFalse(self.refused(b, a))

    def test_enable_and_disable_lines_meet(self):
        self.assertMeet(ref_entry('Enable'), ref_entry('Disable'))

    def test_setscale_is_keyed_by_the_scale_not_its_old_value(self):
        self.assertMeet(ref_entry('SetScale 1.000000'), ref_entry('SetScale 2.000000'))

    def test_ref_enabled_meets_enable_and_disable_lines(self):
        enabled = {'kind': 'actor', 'ref': REF, 'undo': {'op': 'ref_enabled', 'ref': REF, 'on': True}}
        self.assertMeet(enabled, ref_entry('Disable'))
        self.assertMeet(enabled, ref_entry('Enable'))

    def test_movetocell_meets_setpos(self):
        move = ref_entry('MoveToCell 0001A2B3 10 20 30', 'SetAngle Z 0', kind='move')
        self.assertMeet(move, ref_entry('SetPos X 1.0', 'SetPos Y 2.0', 'SetPos Z 3.0', kind='actor'))
        self.assertMeet(move, ref_entry('SetPos Y 2.0'))
        self.assertMeet(move, ref_entry('SetAngle Z 90.0'))

    def test_setav_and_forceav_meet_on_the_actor_value(self):
        self.assertMeet(ref_entry('SetAV Health 100', kind='actor'), ref_entry('ForceAV Health 50', kind='actor'))
        self.assertFree(ref_entry('SetAV Health 100', kind='actor'), ref_entry('ForceAV Strength 5', kind='actor'))

    def test_actor_flag_meets_its_console_line(self):
        flag = {'kind': 'actor', 'ref': REF, 'undo': {'op': 'actor_flag', 'ref': REF, 'flag': 'unconscious', 'on': False}}
        self.assertMeet(flag, ref_entry('SetUnconscious 0', kind='actor'))
        self.assertFree(flag, ref_entry('SetRestrained 0', kind='actor'))

    def test_item_form_ids_are_normalised(self):
        a = {'kind': 'actor', 'undo': {'op': 'item_count', 'ref': '14', 'item': '0x15169', 'count': 1}}
        b = {'kind': 'actor', 'undo': {'op': 'item_count', 'ref': '00000014', 'item': '00015169', 'count': 2}}
        self.assertMeet(a, b)

    def test_one_setting_through_two_commands_meets(self):
        numeric = {'kind': 'setting', 'undo': {'op': 'console', 'line': 'SetNumericGameSetting "fJumpHeightMin" 64.0'}}
        gs = {'kind': 'setting', 'undo': {'op': 'console', 'line': 'SetGS fJumpHeightMin 100.0'}}
        other = {'kind': 'setting', 'undo': {'op': 'console', 'line': 'SetNumericGameSetting "fOther" 1.0'}}
        self.assertMeet(numeric, gs)
        self.assertFree(numeric, other)

    # what U1 already covered keeps working
    def test_u1_cases_still_hold(self):
        self.assertFree(ref_entry('SetPos X 1'), ref_entry('SetPos Y 1'))
        self.assertFree(ref_entry('SetAngle X 1'), ref_entry('SetPos X 1'))
        self.assertFree(ref_entry('SetScale 1.0'), ref_entry('Disable'))
        self.assertFree(ref_entry('SetScale 1.0'), ref_entry('SetScale 1.0', ref='FF000901'))
        self.assertFalse(self.refused(ref_entry('Disable', 'MarkForDelete'), ref_entry('Enable')))
        player = {'kind': 'input', 'undo': {'op': 'console', 'line': 'player.SetAngle Z 90.000'}}
        self.assertTrue(self.refused(player, ref_entry('SetAngle Z 10.000', ref='00000014')))
        var_a = {'kind': 'script_set', 'undo': {'op': 'console', 'line': 'set QuestA.done to 0'}}
        var_b = {'kind': 'script_set', 'undo': {'op': 'console', 'line': 'set QuestA.other to 0'}}
        self.assertFree(var_a, var_b)
        self.assertMeet(var_a, dict(var_a))

    def test_disable_then_enable_then_revert_leaves_the_ref_enabled(self):
        """The audit's case 1: world_edit disable (undo Enable) then enable (undo Disable); fnv_undo the first by
        id is refused, so revert_all runs Disable then Enable and the ref ends enabled, as it was."""
        with FakeLive() as fake:
            live = fake.live
            state = {'disabled': 0}

            def console(line, ref=0):
                if line in ('Enable', 'Disable'):
                    state['disabled'] = int(line == 'Disable')
                return {'line': line, 'accepted': True, 'compileError': False, 'output': []}
            live.console = console
            live.resolve = lambda spec: {'ptr': 0x1000, 'formId': REF, 'expr': REF}
            first = live.journal.add(ref_entry('Enable'))       # the disable
            state['disabled'] = 1
            live.journal.add(ref_entry('Disable'))              # the enable
            state['disabled'] = 0
            r = live.undo(journal_id=first['id'])['results'][0]
            self.assertFalse(r['undone'])
            live.revert_all()
            self.assertEqual(state['disabled'], 0)


class ItemByFormIdTests(unittest.TestCase):
    """M2: an item named by EditorID is journaled under its form id, so it meets the same item named by form id."""

    def setUp(self):
        self.fake = FakeLive()
        self.addCleanup(self.fake.close)
        live = self.live = self.fake.live
        self.count = 3
        ids = {'stimpak': '00015169', '00015169': '00015169', 'player': '00000014', '00000014': '00000014'}
        live.in_game = lambda: None
        live.changing = lambda reason: None
        live.resolve = lambda spec: {'ptr': 0x1000, 'formId': ids[spec.lower()], 'expr': spec}
        live.values = lambda exprs, ref=0: [self.count for _ in exprs]

        def console(line, ref=0):
            words = line.split()
            if words[0] in ('AddItem', 'RemoveItem'):
                self.count += int(words[2]) * (1 if words[0] == 'AddItem' else -1)
            return {'line': line, 'accepted': True, 'compileError': False, 'output': []}
        live.console = console

    def test_the_editor_id_is_resolved_at_journal_time(self):
        out = self.live.actor_control('player', 'add_item', target='Stimpak', value=2)
        self.assertEqual(self.live.journal.get(out['journalId'])['undo']['item'], '00015169')

    def test_editor_id_and_form_id_adds_meet(self):
        first = self.live.actor_control('player', 'add_item', target='Stimpak', value=2)
        self.live.actor_control('player', 'add_item', target='00015169', value=1)
        r = self.live.undo(journal_id=first['journalId'])['results'][0]
        self.assertEqual(r.get('reason'), 'undo the later change first')
        self.assertEqual(self.count, 6, 'nothing removed by the refused undo')
        self.live.revert_all()
        self.assertEqual(self.count, 3)

    def test_an_unresolvable_editor_id_keeps_its_name(self):
        def resolve(spec):
            if spec.lower() == 'stimpak':
                raise LiveError('REF_NOT_FOUND', 'no')
            return {'ptr': 0x1000, 'formId': '00000014', 'expr': spec}
        self.live.resolve = resolve
        out = self.live.actor_control('player', 'add_item', target='Stimpak', value=1)
        self.assertEqual(self.live.journal.get(out['journalId'])['undo']['item'], 'Stimpak')


class HotSwapIntentTests(unittest.TestCase):
    """M3: fnv_script_hot_swap writes its entry (pending, with both scripts' spans) before the swap."""
    TARGET, HOLDER = 0x9000, 0x9800

    def setUp(self):
        from geck_mcp import fnv_data
        from geck_mcp.fnv_data import DataTools, SCRIPT_DATA, SCRIPT_HEADER, SCRIPT_REFS, SCRIPT_VARS
        self.fake = FakeLive()
        self.addCleanup(self.fake.close)
        self.spans = [(SCRIPT_HEADER, 0x10), (SCRIPT_DATA, 4), (SCRIPT_REFS, 8), (SCRIPT_VARS, 8)]
        self.data_off = SCRIPT_DATA
        live = self.live = self.fake.live
        for base, fill in ((self.TARGET, 0x11), (self.HOLDER, 0x22)):
            for off, n in self.spans:
                self.fake.link.put(base + off, bytes([fill]) * n)
        self.before = self.state()
        live.in_game = lambda: None
        live.changing = lambda reason: None
        live.deep = SimpleNamespace(script_form=lambda s: (self.TARGET, {'formId': '0000ABCD'}, None))
        patcher = mock.patch.object(fnv_data, 'read_script', lambda live, script, bytecode: {'variables': []})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tools = DataTools(self.fake.project, live)
        self.tools.compile_ingame = lambda source, include_bytecode=False: {
            'compiled': True, 'script': '0x%08X' % self.HOLDER, 'variables': [], 'statements': []}

    def state(self):
        return [self.fake.link.get(base + off, n) for base in (self.TARGET, self.HOLDER) for off, n in self.spans]

    def swap_batch(self, fail):
        """Wrap the link: the 8-write swap batch lands `fail`'s way."""
        real = self.fake.link.batch

        def batch(ops, timeout=None):
            if len(ops) == 8 and all(op[0] == 'wr' for op in ops):
                self.fake.link.batch = real      # only the swap itself; the undo's batch goes through
                return fail(real, ops)
            return real(ops, timeout)
        self.fake.link.batch = batch

    def hot_swap_fails(self):
        with self.assertRaises(Exception):
            self.tools.hot_swap('MyScript', 'scn MyScript')
        [entry] = [e for e in self.live.journal.entries() if e['kind'] == 'script_hot_swap']
        return entry

    def test_success_confirms_the_entry(self):
        out = self.tools.hot_swap('MyScript', 'scn MyScript')
        entry = self.live.journal.get(out['journalId'])
        self.assertEqual(entry['state'], 'confirmed')
        self.assertTrue(entry['undo']['spans'])
        self.assertTrue(self.live.undo()['results'][0]['undone'])
        self.assertEqual(self.state(), self.before)

    def test_a_lost_reply_after_the_writes_landed_leaves_an_entry_that_undoes(self):
        def lost(real, ops):
            real(ops)
            raise fnv_link.LinkError('COMPANION_TIMEOUT', 'no reply')
        self.swap_batch(lost)
        entry = self.hot_swap_fails()
        self.assertEqual(entry['state'], 'pending')
        self.assertNotEqual(self.state(), self.before, 'the swap landed')
        r = self.live.undo(journal_id=entry['id'])['results'][0]
        self.assertTrue(r['undone'], r)
        self.assertEqual(self.state(), self.before)

    def test_a_half_failed_swap_is_repaired_by_the_undo(self):
        def half(real, ops):
            return real(ops[:3]) + [{'ok': False, 'error': 'unreadable'}] * 5
        self.swap_batch(half)
        entry = self.hot_swap_fails()
        self.assertEqual(entry['state'], 'pending')
        self.assertTrue(self.live.revert_all()['results'][0]['undone'])
        self.assertEqual(self.state(), self.before)

    def test_a_swap_that_failed_before_any_write_is_harmless_to_undo(self):
        def before_any(real, ops):
            raise fnv_link.LinkError('COMPANION_DISCONNECTED', 'pipe closed before the request was sent')
        self.swap_batch(before_any)
        entry = self.hot_swap_fails()
        self.assertEqual(self.state(), self.before)
        r = self.live.undo(journal_id=entry['id'])['results'][0]
        self.assertTrue(r['undone'], r)
        self.assertEqual(self.state(), self.before, 'the undo wrote back what was already there')

    def test_unreadable_fields_refuse_before_any_entry_or_write(self):
        self.fake.link.memory.pop(self.HOLDER + self.data_off)
        written = []
        real = self.fake.link.batch
        self.fake.link.batch = lambda ops, timeout=None: (written.extend(o for o in ops if o[0] == 'wr'),
                                                            real(ops, timeout))[1]
        with self.assertRaises(Exception) as caught:
            self.tools.hot_swap('MyScript', 'scn MyScript')
        self.assertEqual(getattr(caught.exception, 'code', None), 'UNREADABLE')
        self.assertEqual(written, [])
        self.assertEqual([e for e in self.live.journal.entries() if e['kind'] == 'script_hot_swap'], [])

    def test_a_pending_entry_without_spans_is_still_refused(self):
        """Entries from before the fix (no spans) toggle the code: unknown outcome stays refused."""
        e = self.live.journal.add({'kind': 'script_hot_swap', 'state': 'pending', 'undo': {
            'op': 'swap_script', 'target': '0x%08X' % self.TARGET, 'holder': '0x%08X' % self.HOLDER}})
        r = self.live.undo(journal_id=e['id'])['results'][0]
        self.assertFalse(r['undone'])
        self.assertEqual(r['reason'], 'outcome unknown: the change may not have happened')
        self.assertEqual(self.state(), self.before)


if __name__ == '__main__':
    unittest.main()
