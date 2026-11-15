"""Debugger, script debugger, time control and profiling fixes (BUG-LOG E1-E24, S10), against fake companions and fake
game processes: no game is needed. One test or more per bug that has an offline check."""
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from geck_mcp import fnv_blame, fnv_conv, fnv_debugger, fnv_deep, fnv_flow, fnv_scriptdbg, fnv_time, fnv_watch  # noqa: E402
from geck_mcp.fnv_link import LinkError  # noqa: E402
from geck_mcp.fnv_pred import ProcState, compile_predicate  # noqa: E402
from test_fnv_debug import CALLER, CODE, STACK_TOP, DebugTestBase, FakeLink  # noqa: E402
from test_fnv_flow import FUNC, make_live as make_flow_live  # noqa: E402
from test_fnv_script_debug import (FORM_ID, LINE, PROCESS, PROCESS_BYTE, FakeClock, ScriptDebugTestBase,  # noqa: E402
                                   statement_stop)
from test_fnv_time import TG, TimeTestCase  # noqa: E402


def frozen_process():
    proc = mock.MagicMock()
    proc.__enter__.return_value = proc
    proc.suspend_all.return_value = [1001, 1002]
    proc.resume_all.return_value = [1001, 1002]
    proc.threads.return_value = [1000, 1001, 1002]
    return proc


# ===================================================================================== E1, E6, E7, E8, E12 (debugger)
class DebuggerFixes(DebugTestBase):
    def unreachable(self):
        def ready():
            raise LinkError('COMPANION_TIMEOUT', 'the game is frozen')
        self.live.ready = ready

    def test_e1_freeze_thaw_and_status_work_when_the_companion_cannot_answer(self):
        proc = frozen_process()
        with mock.patch('geck_mcp.fnv_procmem.GameProcess', return_value=proc):
            self.assertEqual(self.run_tool('freeze')['state'], 'frozen')
            self.unreachable()                       # every later call would die in live.ready()
            self.assertEqual(self.run_tool('status')['state'], 'frozen')
            self.assertEqual(self.run_tool('thaw')['state'], 'running')
        proc.resume_all.assert_called_once_with([1001, 1002])

    def test_e1_e6_a_restarted_server_can_still_thaw(self):
        proc = frozen_process()
        with mock.patch('geck_mcp.fnv_procmem.GameProcess', return_value=proc):
            self.run_tool('freeze')
            fresh = fnv_debugger.Debugger(self.live, sleep=lambda s: None, clock=lambda: 0.0)   # nothing in memory
            self.unreachable()
            self.assertEqual(fresh.run('status')['state'], 'frozen')
            out = fresh.run('thaw')
        self.assertEqual(out['resumed'], [1001, 1002])
        self.assertFalse(ProcState(self.live).load('dbg_frozen', 1234, 0))

    def test_e6_thaw_with_nothing_on_record_resumes_a_stalled_game(self):
        proc = frozen_process()
        proc.read.return_value = b'\x10\x00\x00\x00'     # the frame counter stands still (S6.3: only then)
        with mock.patch('geck_mcp.fnv_procmem.GameProcess', return_value=proc):
            out = self.run_tool('thaw')
        proc.resume_all.assert_called_once_with([1000, 1001, 1002])
        self.assertTrue(out['forced'])

    def test_e6_thaw_with_nothing_on_record_leaves_a_running_game(self):
        proc = frozen_process()
        proc.read.side_effect = [b'\x10\x00\x00\x00', b'\x40\x00\x00\x00']    # frames advance
        with mock.patch('geck_mcp.fnv_procmem.GameProcess', return_value=proc):
            out = self.run_tool('thaw')
        proc.resume_all.assert_not_called()
        self.assertFalse(out['forced'])

    def test_e6_a_record_of_another_game_process_is_ignored(self):
        ProcState(self.live).save('dbg_frozen', 999, 0, [1, 2])
        self.assertIsNone(ProcState(self.live).load('dbg_frozen', 1234, 0))

    def test_e7_breaks_survive_a_server_restart_and_orphans_are_listed_and_swept(self):
        self.run_tool('break', op='set', location='src/actor.cpp:7')
        fresh = fnv_debugger.Debugger(self.live, sleep=lambda s: None, clock=lambda: 0.0)
        self.assertEqual(fresh.run('break', op='list')['total'], 1)
        self.link.bps[9] = {'id': 9, 'kind': 'int3'}          # armed by an earlier server, unknown to this one
        listed = self.run_tool('break', op='list')
        self.assertIn({'kind': 'bp', 'id': 9}, listed['orphans'])
        self.run_tool('break', op='clear')
        self.assertIn(('bp', 'clear', 'ff'), self.link.ops)
        self.assertEqual(self.link.bps, {})

    def test_e12_two_conditions_never_share_a_predicate_slot(self):
        first = self.dbg._store_pred('frame > 1')
        second = self.dbg._store_pred('frame > 2')
        self.assertNotEqual(first, second)
        self.assertIn(first, self.link.preds)
        self.assertIn(second, self.link.preds)
        self.dbg.release_pred_slot(first)
        self.assertNotIn(first, self.link.preds)
        self.assertEqual(self.dbg._store_pred('frame > 3'), first)      # a released slot is free again

    def test_e12_the_debugger_lock_is_reentrant(self):
        with self.dbg.lock:
            with self.dbg.lock:
                self.assertEqual(self.run_tool('break', op='list')['total'], 0)

    def prologue_top(self, offset, esp, ebp):
        return {'address': CODE + offset, 'regs': {'esp': esp, 'ebp': ebp}}

    def test_e8_return_address_inside_the_prologue(self):
        stack = self.link.regions[2][1]
        s = 0x200
        struct.pack_into('<I', stack, s, CALLER)                  # [esp] at offset 0: the return address
        self.assertEqual(self.dbg._prologue_caller(self.prologue_top(0, STACK_TOP + s, 0xAAAA)), (CALLER, 0xAAAA))
        struct.pack_into('<I', stack, s, 0xBBBB)                  # after push ebp: [esp] = caller's ebp
        struct.pack_into('<I', stack, s + 4, CALLER)
        self.assertEqual(self.dbg._prologue_caller(self.prologue_top(1, STACK_TOP + s, 0xBBBB)), (CALLER, 0xBBBB))
        e = 0x300                                                 # after mov ebp,esp: the frame is valid
        struct.pack_into('<I', stack, e, 0xCCCC)
        struct.pack_into('<I', stack, e + 4, CALLER + 8)
        self.assertEqual(self.dbg._prologue_caller(self.prologue_top(3, STACK_TOP + s, STACK_TOP + e)),
                         (CALLER + 8, 0xCCCC))

    def test_e8_a_nonstandard_prologue_has_no_known_return_address(self):
        self.link.regions[0][1][0:3] = b'\x53\x56\x57'             # push ebx; push esi; push edi
        self.assertEqual(self.dbg._prologue_caller(self.prologue_top(1, STACK_TOP + 0x200, 0)), (None, None))

    def test_e8_run_to_refuses_an_address_outside_the_exe(self):
        with self.assertRaises(fnv_conv.ConvError) as cm:
            self.dbg._run_to({'seq': 1}, STACK_TOP + 0x100)       # a stack slot: an int3 there corrupts the caller
        self.assertEqual(cm.exception.code, 'BAD_LOCATION')
        self.assertFalse(self.link.sent('resume'))

    def test_e24_main_thread_break_holds_on_the_main_predicate(self):
        self.run_tool('break', op='set', location='Actor::ApplyDamage')
        self.assertEqual(self.link.sent('hookact')[0][3], '37')   # MAIN_SLOT 55
        self.assertEqual(self.link.preds[fnv_debugger.MAIN_SLOT], compile_predicate('main')['hex'])

    def test_e24_main_thread_break_with_a_condition_is_anded_with_main(self):
        self.run_tool('break', op='set', location='Actor::ApplyDamage', when='frame > 5')
        slots = [int(op[2], 16) for op in self.link.sent('pred') if op[1] == 'set']
        self.assertIn(compile_predicate('main and (frame > 5)')['hex'], [self.link.preds[s] for s in slots])

    def test_e2_a_hook_hold_carries_the_bytes_a_skip_pops(self):
        self.run_tool('break', op='set', location='Actor::ApplyDamage')
        self.assertEqual(self.link.sent('hookact')[0][2:], ('hold', '37', '4'))


# ===================================================================================== E4 flow
class FlowFixes(unittest.TestCase):
    def test_e4_a_trace_that_cannot_be_read_does_not_leave_the_thread_parked(self):
        live = make_flow_live()
        link = live.link
        stops = {'now': []}     # the hit parks once the breakpoint is armed (K4: earlier stops are not its hit)
        base_one = link.one

        def one(*op, timeout=None):
            if op[0] == 'stops':
                return {'stops': stops['now']}
            if op[:2] == ('bp', 'set'):
                result = base_one(*op, timeout=timeout)
                stops['now'] = [{'seq': 4, 'kind': 'bp', 'id': int(op[2], 16), 'main': True, 'thread': 1,
                                 'eip': '0x%08X' % int(op[3], 16)}]
                return result
            if op[0] == 'resume' and op[2] == 'trace':
                stops['now'] = [{'seq': 5, 'kind': 'step', 'id': 255, 'main': True, 'thread': 1, 'eip': '0x00401010'}]
                return {'ok': True}
            if op[:2] == ('trace', 'read'):
                raise LinkError('COMPANION_TIMEOUT', 'no answer')
            if op[0] == 'resume':
                link.ops.append(op)
                return {'ok': True}
            return base_one(*op, timeout=timeout)
        link.one = one
        tools = fnv_flow.FlowTools(live, sleep=lambda s: None, clock=lambda: 0.0)
        db = SimpleNamespace(function=lambda a: {'addr': FUNC, 'size': 0x20, 'name': 'Actor::ApplyDamage'},
                             lines_of=lambda a: [{'addr': FUNC, 'line': 7, 'file': 'src/actor.cpp'}],
                             locals_of=lambda a: [], line_at=lambda a: None)
        tools.dbg._symdb = lambda required=False: db
        tools.dbg._resolve = lambda f: (FUNC, FUNC, None)
        with self.assertRaises(LinkError):
            tools.run('trace', function='Actor::ApplyDamage', max_steps=100, when='frame > 0')
        resumed = [op[1:3] for op in link.ops if op[0] == 'resume']
        self.assertIn(('5', 'run'), resumed)          # the step stop the trace ended in is released
        # the condition's predicate slot was taken from the debugger's pool and given back
        self.assertEqual(link.preds, {})

    def test_e12_a_trace_condition_does_not_use_the_old_fixed_slot(self):
        live = make_flow_live()
        tools = fnv_flow.FlowTools(live, sleep=lambda s: None, clock=lambda: 0.0)
        tools.dbg.link.preds[fnv_flow.PRED_SLOT] = b'\x01'     # another tool's condition in the old slot
        slot = tools.dbg._store_pred('frame > 1')
        self.assertNotEqual(slot, fnv_flow.PRED_SLOT)


# ===================================================================================== E5, E6, E7, E16 script debugger
class ScriptDebuggerFixes(ScriptDebugTestBase):
    def new_tool(self):
        dbg = fnv_debugger.Debugger(self.live, sleep=lambda s: None, clock=FakeClock())
        return fnv_scriptdbg.ScriptDebugger(self.live, dbg=dbg)

    def test_e16_hex_literals_are_not_variable_names(self):
        self.assertEqual(fnv_scriptdbg.var_names('arg0.[0x0C]:u32 == 5'), set())
        self.assertEqual(fnv_scriptdbg.var_names('frame >= 0x10'), set())
        self.assertEqual(fnv_scriptdbg.var_names('bossHP > 50 and var1 == 0x1F'), {'bossHP', 'var1'})
        self.assertEqual(fnv_scriptdbg.var_names('x2 < 1.5'), {'x2'})

    def test_e16_a_predicate_when_with_a_hex_address_is_armed_in_the_game(self):
        self.run_tool('break', op='set', script='QuestScript', when='arg1:u16 == 0x1234')
        self.assertTrue(self.tool.eval.holding)

    def test_e5_a_division_by_zero_in_a_condition_is_false(self):
        tree = fnv_scriptdbg.compile_var_when('a / b > 1')
        self.assertFalse(fnv_scriptdbg.eval_var_when(tree, {'a': 4.0, 'b': 0.0}))
        self.assertTrue(fnv_scriptdbg.eval_var_when(tree, {'a': 4.0, 'b': 2.0}))

    def test_e5_settle_gives_up_on_stops_that_are_never_wanted(self):
        tool = self.tool
        resumed = []
        tool.dbg.clock = lambda: 0.0
        tool._wanted = lambda stop: False
        tool._in_save_load = lambda stop: False
        tool.dbg._resume = lambda stop, *a, **k: resumed.append(stop['seq'])
        tool._await_ours = lambda after, wait=True: {'seq': after + 1}
        self.assertIsNone(tool._settle({'seq': 0}, wait=True))
        self.assertEqual(len(resumed), fnv_scriptdbg.STEP_LIMIT)
        self.assertEqual(tool.refused, fnv_scriptdbg.STEP_LIMIT)

    def test_e6_the_script_freeze_survives_a_server_restart(self):
        self.run_tool('freeze')
        tool = self.new_tool()
        self.assertEqual(tool.run('status')['state'], 'frozen')
        out = tool.run('thaw')
        self.assertEqual(out['restored'], 1)
        self.assertEqual(self.link.region(PROCESS_BYTE, 1)[0][PROCESS_BYTE - PROCESS], 1)
        self.assertIsNone(ProcState(self.live).load('sdbg_freeze', 1234, 0))

    def test_e6_thaw_forces_the_byte_on_when_nothing_is_on_record(self):
        self.link.regions[3][1][0x685] = 0
        out = self.run_tool('thaw')
        self.assertTrue(out['forced'])
        self.assertEqual(self.link.region(PROCESS_BYTE, 1)[0][PROCESS_BYTE - PROCESS], 1)

    def test_e6_e7_a_new_game_process_drops_the_old_freeze_and_breaks(self):
        self.run_tool('freeze')
        self.run_tool('break', op='set', script='QuestScript', line=LINE)
        self.assertTrue(self.tool.breaks)
        self.live.game = SimpleNamespace(process=lambda: (777, 5))
        self.link.stops = []
        out = self.run_tool('status')
        self.assertNotEqual(out.get('state'), 'frozen')
        self.assertIsNone(self.tool.saved_scripts)
        self.assertEqual(self.tool.breaks, {})
        self.assertIsNone(self.tool.eval.hook_id)

    def test_e7_a_restarted_server_recognises_its_own_hold_stops(self):
        self.run_tool('break', op='set', script='QuestScript', line=LINE)
        hook_id = self.hook_id()
        tool = self.new_tool()                                   # hook_id is None here: the hook is found by label
        self.link.stops = [statement_stop(1, hook_id)]
        self.assertEqual(tool.run('break', op='list')['total'], 1)
        out = tool.run('status')
        self.assertEqual(out['stop']['line'], LINE)

    def test_e12_the_hold_and_the_debugger_share_one_lock(self):
        self.assertIs(self.tool.lock, self.dbg.lock)
        self.assertIs(self.tool.eval.lock, self.dbg.lock)


# ===================================================================================== E9, E6, E22, S10 time
class TimeFixes(TimeTestCase):
    def with_state(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.live.project = SimpleNamespace(state_dir=Path(tmp.name))

    def restart(self):
        return fnv_time.TimeControl(self.live, sleep=self.comp.sleep, clock=self.comp.clock)

    def test_e6_a_pause_survives_a_server_restart(self):
        self.with_state()
        self.control.run('pause')
        self.assertEqual(self.comp.counter, 1)
        out = self.restart().run('resume')
        self.assertEqual(self.comp.counter, 0)
        self.assertTrue(out['changed'])

    def test_e6_a_pause_of_another_game_process_is_not_ours(self):
        self.with_state()
        self.control.run('pause')
        self.live.game.proc = (5555, 'FalloutNV.exe')            # the game restarted
        self.comp.put_u32(TG, 1)                                  # someone else's pause in the new process
        out = self.restart().run('resume')
        self.assertFalse(out['changed'])
        self.assertEqual(self.comp.counter, 1)

    def test_e6_force_resume_undoes_every_pause_of_a_lost_record(self):
        self.comp.put_u32(TG, 3)
        out = self.control.run('force_resume')
        self.assertEqual(self.comp.counter, 0)
        self.assertTrue(out['changed'])

    def test_e9_a_long_wait_runs_in_rounds_of_at_most_the_companion_limit(self):
        base = self.comp.frame
        out = self.wait.run(seconds=200, timeout_seconds=900)      # about 12000 frames at 60 a second
        limits = [int(op[2], 16) for request in self.comp.requests for op in request if op[0] == 'until']
        self.assertTrue(limits)
        self.assertLessEqual(max(limits), fnv_time.UNTIL_MAX)
        self.assertGreater(len(limits), 1)
        self.assertGreater(self.comp.frame - base, fnv_time.UNTIL_MAX)
        self.assertGreater(out['frames'], fnv_time.UNTIL_MAX)
        self.assertFalse(self.comp.held)

    def test_e9_the_largest_frame_count_no_longer_exceeds_the_companion_limit(self):
        self.wait.run(frames=10000, timeout_seconds=900)
        limits = [int(op[2], 16) for request in self.comp.requests for op in request if op[0] == 'until']
        self.assertEqual(max(limits), fnv_time.UNTIL_MAX)

    def test_e22_a_stepped_run_at_a_fixed_rate_restores_the_previous_step(self):
        out = self.control.run('step', frames=5, fps=30)
        self.assertIn('fixedStepRestoredTo', out)
        self.assertEqual(struct.unpack('<f', self.comp.get(TG + 4, 4))[0], 0.0)

    def test_s10_an_unreadable_predicate_address_fails_before_waiting(self):
        original = self.comp.op

        def op(name, a):
            if name == 'rd' and int(a[0], 16) == 0x10:
                return {'ok': False, 'error': 'unreadable'}
            return original(name, a)
        self.comp.op = op
        with self.assertRaises(fnv_conv.ConvError) as cm:
            self.wait.run(predicate='[0x10]:u8 == 1', max_frames=20)
        self.assertEqual(cm.exception.code, 'MEMORY_UNREADABLE')
        self.assertFalse(any(o[0] == 'until' for request in self.comp.requests for o in request))
        self.assertFalse(self.comp.held)


# ===================================================================================== E21 predicates
class PredicateFixes(unittest.TestCase):
    def test_e21_the_compiler_reports_what_it_reads_and_warns_about_signed_u32_compares(self):
        out = compile_predicate('[0x011A2FE0]:u32 > 0x10000000')
        self.assertEqual(out['reads'], [(0x011A2FE0, 4)])
        self.assertTrue(any('signed' in w for w in out['warnings']))
        self.assertNotIn('warnings', compile_predicate('[0x011A2FE0]:u32 == 5'))

    def test_e21_a_huge_negative_float_is_a_bad_predicate_not_an_overflow(self):
        with self.assertRaises(fnv_conv.ConvError) as cm:
            compile_predicate('-' + '9' * 40 + '.0 > 0')
        self.assertEqual(cm.exception.code, 'BAD_PREDICATE')

    def test_e21_an_absolute_address_plus_a_large_offset_is_folded(self):
        out = compile_predicate('[0x100 + 0x80000000]:u32 == 1')
        self.assertTrue(out['hex'])


# ===================================================================================== E10, E11, E20 deep
class DeepLink:
    def __init__(self, hooks):
        self.hook_list = hooks
        self.ops = []

    def one(self, *op, timeout=None):
        self.ops.append(op)
        if op[0] == 'hooks':
            return {'ok': True, 'hooks': self.hook_list}
        if op[0] == 'unhook':
            self.hook_list = [h for h in self.hook_list if h['id'] != int(op[1], 16)]
            return {'ok': True}
        return {'ok': True}

    def ping(self, **kw):
        return {'frame': 100}


class DeepFixes(unittest.TestCase):
    def tools(self, hooks, status='proven'):
        engine = SimpleNamespace(resolve=lambda name: 0x5AC1E0, summary=lambda address: {'status': status,
                                                                                        'name': 'Script::Run'})
        live = SimpleNamespace(link=DeepLink(hooks), engine=lambda: engine, in_game=lambda: None, ready=lambda: None,
                               reads=lambda spans: [None for _ in spans])
        return fnv_deep.DeepTools(live), live

    def test_e10_a_watch_does_not_remove_another_tools_hook(self):
        for label in ('dbg 1 Script::Run', 'script_line_dbg', 'act:1', 'tp:3'):
            tools, live = self.tools([{'id': 7, 'label': label, 'target': '0x005AC1E0', 'stolen': 6}])
            with self.assertRaises(fnv_deep.HookError) as cm:
                tools.ensure('script_run')
            self.assertEqual(cm.exception.code, 'ALREADY_HOOKED')
            self.assertFalse([op for op in live.link.ops if op[0] == 'unhook'], label)

    def test_e10_a_watch_label_on_the_same_function_is_replaced(self):
        tools, live = self.tools([{'id': 7, 'label': 'script_effect_start', 'target': '0x005AC1E0', 'stolen': 6}])
        with self.assertRaises(fnv_deep.HookError) as cm:       # then the unreadable function stops it: not ALREADY_HOOKED
            tools.ensure('script_run')
        self.assertEqual(cm.exception.code, 'UNREADABLE')
        self.assertTrue([op for op in live.link.ops if op[0] == 'unhook'])

    def test_e20_a_class_proven_function_takes_a_watch_hook(self):
        # a name that is not one of the fixed watch targets goes through the proof status
        tools, _ = self.tools([], status='class_proven')
        self.assertEqual(tools._function('Foo::Bar')[0], 0x5AC1E0)
        tools, _ = self.tools([], status='class proven')
        self.assertEqual(tools._function('Foo::Bar')[0], 0x5AC1E0)
        tools, _ = self.tools([], status='unnamed')
        with self.assertRaises(fnv_deep.HookError):
            tools._function('Foo::Bar')

    def test_e11_profile_stop_drops_the_baseline(self):
        tools, _ = self.tools([])
        tools._baseline['profile'] = {'frame': 1, 'time': 0}
        tools.script_profile('stop')
        self.assertNotIn('profile', tools._baseline)

    def test_e11_crash_context_disable_unhooks_once_the_profile_is_stopped(self):
        hooks = [{'id': 1, 'label': 'script_run', 'target': '0x1', 'stolen': 6, 'calls': 0, 'seq': 0}]
        tools, live = self.tools(hooks)
        tools._baseline.update(profile={'frame': 1}, crash_context=True)
        tools.script_profile('stop')                              # crash context is on: the hooks stay
        self.assertTrue(live.link.hook_list)
        tools.crash_context('disable')
        self.assertEqual(live.link.hook_list, [])


# ===================================================================================== E13, E17, E19, E20, E23 watch
class DbgLink(FakeLink):
    """FakeLink plus the debug-register ops: info, set, read, clear (one register or `f`)."""

    def __init__(self, busy_mask=0, bp_mask=0, hits=()):
        super().__init__([])
        self.busy, self.bp_slots, self.hit_rows = busy_mask, bp_mask, list(hits)
        self.hello = {'mainThreadId': 1000}

    def one(self, *op, timeout=None):
        if op[0] != 'dbg':
            return super().one(*op, timeout=timeout)
        self.ops.append(op)
        if op[1] == 'info':
            return {'ok': True, 'active': self.busy | self.bp_slots, 'bpSlots': self.bp_slots, 'seq': 0, 'slots': []}
        if op[1] == 'set':
            slot = int(op[2])
            if self.bp_slots & (1 << slot):
                return {'ok': False, 'error': 'no_debug_register'}
            self.busy |= 1 << slot
            return {'ok': True, 'seq': 0, 'active': self.busy}
        if op[1] == 'read':
            after = int(op[2], 16)
            return {'ok': True, 'seq': len(self.hit_rows), 'hits': [h for h in self.hit_rows if h[0] > after]}
        if op[1] == 'clear':
            self.busy = self.bp_slots if op[2] == 'f' else self.busy & ~(1 << int(op[2]))
            return {'ok': True}
        return {'ok': False, 'error': 'bad_arguments'}


class WatchFixes(unittest.TestCase):
    def setUp(self):
        fnv_watch._DR_CLAIMED.clear()
        self.addCleanup(fnv_watch._DR_CLAIMED.clear)

    def test_e13_a_watch_takes_a_free_register_and_clears_only_that_one(self):
        link = DbgLink(bp_mask=0b0001)                            # DR0 holds a hardware breakpoint
        slot = fnv_watch.claim_debug_register(link)
        self.assertEqual(slot, 1)
        self.assertEqual(fnv_watch.claim_debug_register(link), 2)  # a second watch at the same time
        fnv_watch.release_debug_register(link, slot)
        self.assertIn(('dbg', 'clear', '1'), link.ops)
        self.assertNotIn(('dbg', 'clear', 'f'), link.ops)
        self.assertEqual(fnv_watch.claim_debug_register(link), 1)

    def test_e13_four_busy_registers_give_none(self):
        self.assertIsNone(fnv_watch.claim_debug_register(DbgLink(busy_mask=0b1111)))

    def test_e13_hits_of_other_registers_are_dropped(self):
        rows = [[1, 0, '0x1'], [2, 1, '0x2'], [3, 1, '0x3']]
        self.assertEqual(fnv_watch.hits_of_slot(rows, 1), rows[1:])


def blame_live(link, hooks=None, records=None):
    deep = SimpleNamespace(hooks=lambda: list(hooks or []), records=records or (lambda h, since=0, limit=4096: (0, [], 0)),
                           record=lambda h, seq: None)
    return SimpleNamespace(link=link, ready=lambda: None, deep=deep, game=SimpleNamespace(process=lambda: (1234, 0)),
                           resolve=lambda ref: {'ptr': 0x30000000})


class FakeStatementHook:
    def __init__(self, installed=False):
        self.installed, self.holding, self.log_on, self.was = installed, False, False, installed

    def existing(self):
        return {'id': 0} if self.installed else None

    def ensure(self):
        self.installed = True

    def remove(self):
        self.installed = False


class BlameFixes(unittest.TestCase):
    def setUp(self):
        fnv_watch._DR_CLAIMED.clear()
        self.addCleanup(fnv_watch._DR_CLAIMED.clear)
        self.ticks = 0

    def clock(self):
        self.ticks += 1
        return self.ticks * 0.1

    def tool(self, link, statement_hook=None, **kw):
        tool = fnv_blame.BlameTools(blame_live(link, **kw), sleep=lambda s: None, clock=self.clock,
                                    statement_hook=statement_hook)
        tool.dbg._symbol = lambda address, return_address=False: {'function': 'Actor::Writer', 'source': 'a.cpp:1'}
        return tool

    def test_e13_field_uses_a_free_register_and_clears_only_its_own(self):
        hits = [[1, 1, '0x401000', 1000, '0x5', '0x0', '0x0', 1, 0, 0],        # this watch's register
                [2, 0, '0x401000', 1000, '0x6', '0x0', '0x0', 1, 0, 0]]        # the breakpoint's: not a write here
        link = DbgLink(bp_mask=0b0001, hits=hits)
        out = self.tool(link).field('0x011DEA0C', seconds=0.3)
        sets = [op for op in link.ops if op[:2] == ('dbg', 'set')]
        self.assertEqual(sets[0][2], '1')
        self.assertIn(('dbg', 'clear', '1'), link.ops)
        self.assertNotIn(('dbg', 'clear', 'f'), link.ops)
        self.assertEqual(out['total'], 1)

    def test_e13_field_says_when_all_four_registers_are_busy(self):
        with self.assertRaises(fnv_conv.ConvError) as cm:
            self.tool(DbgLink(busy_mask=0b1111)).field('0x011DEA0C', seconds=0.3)
        self.assertEqual(cm.exception.code, 'WATCH_REFUSED')

    def test_e23_field_removes_the_statement_hook_it_installed(self):
        hook = FakeStatementHook(installed=False)
        self.tool(DbgLink(), hook).field('0x011DEA0C', seconds=0.3)
        self.assertFalse(hook.installed)

    def test_e23_field_leaves_a_statement_hook_that_was_already_there(self):
        hook = FakeStatementHook(installed=True)
        self.tool(DbgLink(), hook).field('0x011DEA0C', seconds=0.3)
        self.assertTrue(hook.installed)

    def test_e23_field_leaves_the_statement_hook_when_a_break_needs_it(self):
        hook = FakeStatementHook(installed=False)
        tool = self.tool(DbgLink(), hook)
        hook.ensure = lambda: (setattr(hook, 'installed', True), setattr(hook, 'holding', True))
        tool.field('0x011DEA0C', seconds=0.3)
        self.assertTrue(hook.installed)

    def test_e19_a_statement_hook_with_id_zero_still_gives_the_exact_statement(self):
        link = DbgLink()
        tool = self.tool(link)
        tool.dbg._symbol = lambda address, return_address=False: {'function': 'ScriptRunner::EvaluateLine'}
        seen = []
        tool._statement_at = lambda hook_id, seq: seen.append((hook_id, seq)) or {'script': 'X', 'line': 3}
        row = tool._field_hit([1, 0, '0x5E1550', 1000, '0x5', '0x0', '0x0', 1, 0, 7], 0x11DEA0C)
        self.assertEqual(seen, [(0, 7)])
        self.assertEqual(row['script'], {'script': 'X', 'line': 3})

    def test_e17_a_new_hook_with_the_same_label_is_read_from_the_start(self):
        since_seen = []

        def records(h, since=0, limit=4096):
            since_seen.append((h['id'], since))
            return 10, [], 0
        hooks = [{'id': 3, 'label': 'blame.disable'}]
        tool = self.tool(DbgLink(), hooks=hooks, records=records)
        tool._collect()
        tool._collect()
        hooks[0] = {'id': 4, 'label': 'blame.disable'}          # removed and armed again: its ring starts at 0
        tool._collect()
        self.assertEqual(since_seen, [(3, 0), (3, 10), (4, 0)])

    def test_e17_a_new_game_process_drops_the_old_hits(self):
        link = DbgLink()
        live = blame_live(link)
        tool = fnv_blame.BlameTools(live, sleep=lambda s: None, clock=self.clock)
        tool.run('report')
        tool.hits.append({'source': 'field'})
        live.game = SimpleNamespace(process=lambda: (4321, 9))
        out = tool.run('report')
        self.assertEqual(out['total'], 0)

    def test_e20_blame_presets_accept_class_proven(self):
        engine = SimpleNamespace(resolve=lambda name: 0x500000,
                                 summary=lambda address, with_location=True: {'status': 'class_proven'})
        tool = self.tool(DbgLink())
        self.assertEqual(tool._preset_target(engine, ('TESObjectREFR::Disable',)), (0x500000, 'TESObjectREFR::Disable'))


if __name__ == '__main__':
    unittest.main()
