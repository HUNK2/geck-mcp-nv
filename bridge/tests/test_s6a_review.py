"""PLAN-3 S6 batch A review fixes (fresh Opus review of 027a947..9899601)."""
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from geck_mcp.fnv_conv import ConvError  # noqa: E402
from test_stab_s6a import _flow  # noqa: E402


def debugger(case):
    from test_fnv_debug import DebugTestBase
    base = DebugTestBase('setUp')
    base.setUp()
    case.addCleanup(base.doCleanups)
    return base.dbg


class PendingToTests(unittest.TestCase):
    def setUp(self):
        self.dbg = debugger(self)
        self.dbg._persist = lambda: None

    def test_i1_a_reached_pending_site_leaves_the_list(self):
        self.dbg.pending_to = {0x00500010}
        self.dbg.link.stops = [{'seq': 9, 'kind': 'step', 'eip': '0x00500010', 'thread': 7}]
        self.dbg._stops()
        self.assertEqual(self.dbg.pending_to, set())

    def test_i1_target_already_pending_is_still_named(self):
        target = 0x00500010
        self.dbg.pending_to = {target}
        here = {'seq': 1, 'kind': 'bp', 'eip': '0x00500000', 'thread': 7}
        self.dbg._current = lambda: here
        self.dbg._return_address = lambda stop: target
        self.dbg._esp_of = lambda stop: 0x1000
        self.dbg._resume = lambda cur, *a, **kw: {'seq': 2, 'kind': 'bp', 'eip': '0x00600000', 'thread': 7}
        self.dbg._why = lambda stop: 'break 2'
        self.dbg.status_view = lambda stop: {}
        out = self.dbg.step('out')
        self.assertEqual(out['pendingTo'], '0x%08X' % target)


class FlowSlotTests(unittest.TestCase):
    def test_m2_a_failed_trace_zeroes_the_call_slot(self):
        flow, link = _flow(signature='int __cdecl F(void)')
        flow._await_bp = lambda bp_id, wait, **kw: {'seq': 4}
        released = []
        flow._release_quietly = lambda seq, end=None: released.append(seq)
        real = link.one

        def one(*op, **kw):
            if op[:2] == ('trace', 'ranges'):
                raise RuntimeError('link dropped')
            return real(*op, **kw)
        link.one = one
        with self.assertRaises(RuntimeError):
            flow.trace('F', call=(None, [1]))
        writes = [op for op in link.log if op[0] == 'write' and op[1] == 0x10000000]
        self.assertEqual(writes[-1][2], struct.pack('<I', 0), 'the module must not make the call later')
        self.assertEqual(released, [4], 'the bp stop is not left parked')


class ThawGuardTests(unittest.TestCase):
    def setUp(self):
        self.dbg = debugger(self)
        self.dbg._persist = lambda: None
        test = self
        self.frame_reads = [b'\x10\0\0\0', b'\x10\0\0\0']
        self.resumed = []

        class Proc:
            def __init__(self, pid):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

            def threads(self):
                return [10, 20]

            def read(self, a, n):
                return test.frame_reads.pop(0) if test.frame_reads else None

            def resume_all(self, tids):
                test.resumed.extend(tids)
                return list(tids)
        from geck_mcp import fnv_procmem
        p = mock.patch.object(fnv_procmem, 'GameProcess', Proc)
        p.start()
        self.addCleanup(p.stop)

    def test_m3_parked_by_the_debugger_is_not_frozen(self):
        self.dbg.link.ping = lambda timeout=None: {'parked': 1, 'held': False}
        out = self.dbg.thaw()
        self.assertEqual(self.resumed, [])
        self.assertIn('continue', out['reason'])

    def test_m3_held_gate_is_not_frozen(self):
        self.dbg.link.ping = lambda timeout=None: {'parked': 0, 'held': True}
        out = self.dbg.thaw()
        self.assertEqual(self.resumed, [])
        self.assertIn('fnv_time', out['reason'])

    def test_m3_unreadable_counter_resumes_nothing(self):
        self.frame_reads = []
        self.dbg.link.ping = lambda timeout=None: {'parked': 0, 'held': False}
        out = self.dbg.thaw()
        self.assertEqual(self.resumed, [])
        self.assertIn('cannot tell', out['reason'])


class BreakNumberTests(unittest.TestCase):
    def setUp(self):
        self.dbg = debugger(self)
        self.stop = {'seq': 5, 'kind': 'bp', 'id': 0, 'eip': '0x00500000', 'thread': 7}

    def test_m5_a_reused_bp_id_does_not_rename_the_stop(self):
        self.dbg.breaks = {1: {'number': 1, 'id': 0, 'mechanism': 'int3'}}
        self.dbg.link.stops = [self.stop]
        self.dbg._stops()
        self.dbg.breaks = {2: {'number': 2, 'id': 0, 'mechanism': 'int3'}}     # break 1 cleared, break 2 reuses id 0
        self.assertEqual(self.dbg._why(self.stop), 'break 1 (cleared) (trap)')

    def test_m5_a_new_game_starts_a_new_map(self):
        self.dbg.stop_breaks = {5: 1}
        self.dbg._restore()
        self.assertEqual(self.dbg.stop_breaks, {})


class HookNoteTests(unittest.TestCase):
    def test_m6_old_companion_note_has_no_none(self):
        from types import SimpleNamespace
        from geck_mcp.fnv_control import ControlTools
        hook = {'id': 4, 'label': 'act:00500000', 'target': '0x00500000', 'calls': 1, 'action': 3,
                'actionValue': '0x00000007', 'actionHits': 3}
        deep = SimpleNamespace(hook=lambda label: hook, records=lambda h, since, limit=4096: (1, [], 0))
        agent = SimpleNamespace(live=SimpleNamespace(in_game=lambda: None), deep=deep, _traces={},
                                _read_trace=lambda *a: {'calls': [{'seq': 1}]})
        control = ControlTools(agent)
        control._function = lambda f, allow=False: (0x00500000, {'name': 'F'}, '', None)
        out = control.hook_set('F', action='read')
        self.assertNotIn('None', out['resultNote'])


if __name__ == '__main__':
    unittest.main()
