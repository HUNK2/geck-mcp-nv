"""fnv_time (clock and frame gate) and fnv_wait_for against a fake companion: the ops sent, the clock balance, the gate, the frame
counter, the predicate bytes (run by a small stack machine in this file), engine map checks and the next steps.
No game and no pipe: FakeCompanion answers batch() the way the wire ops of docs/FNV-COMPANION-V2.md do."""
from pathlib import Path
from types import SimpleNamespace
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geck_mcp import fnv_time  # noqa: E402
from geck_mcp.fnv_conv import ConvError  # noqa: E402
from geck_mcp.fnv_live import LiveService  # noqa: E402
from geck_mcp.fnv_pred import compile_predicate  # noqa: E402
from geck_mcp.fnv_time import TimeControl, WaitFor  # noqa: E402

TG = fnv_time.TIME_GLOBAL
FC = fnv_time.FRAME_COUNTER
MULT = fnv_time.MULTIPLIER
MULT_T = fnv_time.MULTIPLIER_TARGET
DISABLE, ENABLE, CLAMP = 0x00AA50A0, 0x00AA5040, 0x0086D4C0
# the companion's own frame counter, read through predicate slot 62 (fnv_time.FRAME_SLOT); 'frame' compiles to 0x50
FRAME_READ = [('pred', 'set', '3e', '50'), ('pred', 'eval', '3e')]
READ_TYPES = {0: '<B', 1: '<b', 2: '<H', 3: '<h', 4: '<I', 5: '<i', 6: '<f'}


class FakeCompanion:
    """Memory, the frame gate, the timer calls, predicates and `until`, with a clock that moves with the frames."""

    def __init__(self):
        self.mem = {}
        self.requests = []
        self.held = False
        self.refuse_gatecfg = False
        self.preds = {}
        self.cells = []
        self.cell = '0002A2B3'
        self.time = 0.0
        self.put_u32(TG, 0)                  # clock counter
        self.put_f32(TG + 4, 0.0)            # fixed step (ms per frame, a float; 0 = off)
        self.put_f32(TG + 0x0C, 1 / 60)      # frame delta
        self.put_u32(FC, 100)
        self.put_f32(MULT, 1.0)
        self.put_f32(MULT_T, 1.0)
        self.put_u8(fnv_time.LOADING_FLAG, 0)
        self.put_u8(fnv_time.MENU_MODE, 0)

    # ------------------------------------------------------------ memory
    def put(self, address, data):
        for i, b in enumerate(data):
            self.mem[address + i] = b

    def get(self, address, n):
        return bytes(self.mem.get(address + i, 0) for i in range(n))

    def put_u32(self, address, value):
        self.put(address, struct.pack('<I', value))

    def put_u8(self, address, value):
        self.put(address, bytes([value]))

    def put_f32(self, address, value):
        self.put(address, struct.pack('<f', value))

    def u32(self, address):
        return struct.unpack('<I', self.get(address, 4))[0]

    @property
    def frame(self):
        return self.u32(FC)

    @property
    def counter(self):
        return self.u32(TG)

    def advance(self, frames):
        self.put_u32(FC, self.frame + frames)
        self.time += frames / 60.0

    def sleep(self, seconds):
        """The fake real time passes: the game runs at 60 frames a second unless the gate is held."""
        self.time += seconds
        if not self.held:
            self.put_u32(FC, self.frame + int(round(seconds * 60)))

    def clock(self):
        return self.time

    # ------------------------------------------------------------ the predicate machine (section 2 of the contract)
    def read_typed(self, address, kind):
        fmt = READ_TYPES[kind]
        return struct.unpack(fmt, self.get(address, struct.calcsize(fmt)))[0]

    def run_pred(self, data):
        return self.exec_pred(data) != 0

    def exec_pred(self, data):
        stack, i = [], 0
        while i < len(data):
            op = data[i]
            i += 1
            if op == 0x01:
                stack.append((struct.unpack_from('<i', data, i)[0], False))
                i += 4
            elif op == 0x02:
                stack.append((struct.unpack_from('<f', data, i)[0], True))
                i += 4
            elif op == 0x10:
                base, value, n = data[i], struct.unpack_from('<I', data, i + 1)[0], data[i + 5]
                i += 6
                offsets = [struct.unpack_from('<i', data, i + 4 * k)[0] for k in range(n)]
                i += 4 * n
                kind = data[i]
                i += 1
                assert base == 0, 'the fake only runs absolute loads'
                p = value
                for off in offsets[:-1]:
                    p = struct.unpack('<I', self.get(p + off, 4))[0]
                stack.append((self.read_typed(p + offsets[-1], kind), kind == 6))
            elif 0x20 <= op <= 0x25:
                b, a = stack.pop(), stack.pop()
                x, y = a[0], b[0]
                res = {0x20: x == y, 0x21: x != y, 0x22: x < y, 0x23: x <= y, 0x24: x > y, 0x25: x >= y}[op]
                stack.append((1 if res else 0, False))
            elif op in (0x30, 0x31):
                b, a = stack.pop()[0] != 0, stack.pop()[0] != 0
                stack.append((int((a and b) if op == 0x30 else (a or b)), False))
            elif op == 0x32:
                stack.append((0 if stack.pop()[0] else 1, False))
            elif op in (0x40, 0x41):
                b, a = stack.pop(), stack.pop()
                value = a[0] + b[0] if op == 0x40 else a[0] - b[0]
                stack.append((value, a[1] or b[1]))
            elif op in (0x42, 0x43, 0x44):
                b, a = stack.pop()[0], stack.pop()[0]
                value = {0x42: a & b, 0x43: a >> b, 0x44: (a << b) & 0xFFFFFFFF}[op]
                stack.append((value, False))
            elif op == 0x50:
                stack.append((self.frame, False))
            elif op == 0x51:
                stack.append((1, False))
            else:
                raise AssertionError('unknown op 0x%02x' % op)
        return stack[-1][0]

    # ------------------------------------------------------------ the wire
    def batch(self, ops, timeout=None):
        self.requests.append([tuple(str(p) for p in op) for op in ops])
        return [self.op(op[0], [str(p) for p in op[1:]]) for op in ops]

    def op(self, name, a):
        if name == 'gatecfg':
            return {'ok': False, 'error': 'unknown_op'} if self.refuse_gatecfg else {'ok': True}
        if name == 'gate':
            return {'held': self.held, 'clockPausedByUs': False, 'timeout': 120000, 'msSinceContact': 0,
                    'stepLeft': 0, 'untilPred': None}
        if name == 'rd':
            return {'ok': True, 'hex': self.get(int(a[0], 16), int(a[1], 16)).hex()}
        if name == 'wr':
            address, data = int(a[0], 16), bytes.fromhex(a[1])
            old = self.get(address, len(data))
            self.put(address, data)
            return {'ok': True, 'old': old.hex()}
        if name == 'call':
            address, args = int(a[0], 16), [int(x, 16) for x in a[4:]]
            if address == DISABLE:
                self.put_u32(TG, self.counter + 1)
            elif address == ENABLE:
                self.put_u32(TG, max(0, self.counter - 1))
            elif address == CLAMP:
                fps = args[0]
                self.put_f32(TG + 4, 1000.0 / fps if fps else 0.0)   # the engine stores a float
            else:
                return {'ok': False, 'error': 'unknown_function'}
            return {'ok': True, 'eax': '0'}
        if name == 'step':
            n = int(a[0], 16)
            self.advance(n)
            self.held = True
            return {'stepped': n, 'frame': self.frame}
        if name == 'hold':
            if a[0] == 'on':
                self.held = True
                return {'held': True, 'timeout': int(a[1], 16) if len(a) > 1 else 120000}
            self.held = False
            return {'held': False}
        if name == 'pred':
            slot = int(a[1], 16)
            if a[0] == 'set':
                self.preds[slot] = bytes.fromhex(a[2])
                return {'ok': True, 'id': slot, 'ops': 0}
            if a[0] == 'eval':
                top = self.exec_pred(self.preds[slot])
                return {'ok': True, 'value': 1 if top else 0, 'top': top, 'errors': 0}
            self.preds.pop(slot, None)
            return {'ok': True, 'cleared': 1}
        if name == 'until':
            slot, limit = int(a[0], 16), int(a[1], 16)
            n, met = 0, False
            while True:
                if self.run_pred(self.preds[slot]):
                    met = True
                    break
                if n >= limit:
                    break
                self.advance(1)
                n += 1
            self.held = True
            return {'met': met, 'frames': n, 'frame': self.frame}
        if name == 'eval':
            cell = self.cells.pop(0) if self.cells else self.cell
            return {'ok': True, 'result': {'type': 'form', 'formId': cell}}
        raise AssertionError('unexpected op %s' % name)


class FakeGame:
    def __init__(self):
        self.proc = (4242, 'FalloutNV.exe')

    def process(self):
        return self.proc


class FakeLive:
    def __init__(self, companion):
        self.link = companion
        self.game = FakeGame()
        self.functions = {
            DISABLE: {'name': 'GameTimerRecord::FUN_00aa50a0', 'status': 'unnamed',
                      'signature': 'public: void __thiscall GameTimerRecord::FUN_00aa50a0(void)'},
            ENABLE: {'name': 'GameTimerRecord::Enable', 'status': 'decomp',
                     'signature': 'public: void __thiscall GameTimerRecord::Enable(void)'},
            CLAMP: {'name': 'C86D4C0Timer::FUN_0086d4c0', 'status': 'unnamed',
                    'signature': 'public: void __thiscall C86D4C0Timer::FUN_0086d4c0(unsigned int)'},
        }

    def engine(self):
        return SimpleNamespace(functions=self.functions)


class TimeTestCase(unittest.TestCase):
    def setUp(self):
        self.comp = FakeCompanion()
        self.live = FakeLive(self.comp)
        self.control = TimeControl(self.live, sleep=self.comp.sleep, clock=self.comp.clock)
        self.wait = WaitFor(self.control)

    def last_ops(self):
        return self.comp.requests[-1]


class TimeControlTests(TimeTestCase):
    def test_pause_and_resume_balance_the_clock_counter(self):
        out = self.control.run('pause')
        self.assertEqual(list(out)[:2], ['verdict', 'reason'])
        self.assertEqual(self.comp.counter, 1)
        self.assertEqual(self.last_ops(), [('rd', '11f6394', '4'), ('call', 'aa50a0', 'v', '11f6394', '0'),
                                           ('rd', '11f6394', '4')])
        self.assertTrue(out['changed'])
        self.assertTrue(out['pausedByUs'])
        self.assertEqual((out['clockCounterBefore'], out['clockCounter']), (0, 1))

        sent = len(self.comp.requests)
        again = self.control.run('pause')
        self.assertFalse(again['changed'])
        self.assertEqual(self.comp.counter, 1)
        self.assertEqual(len(self.comp.requests), sent)          # no second Disable

        out = self.control.run('resume')
        self.assertEqual(self.comp.counter, 0)
        self.assertEqual(self.last_ops()[0], ('call', 'aa5040', 'v', '11f6394', '0'))
        self.assertTrue(out['changed'])
        self.assertFalse(out['pausedByUs'])

        sent = len(self.comp.requests)
        idle = self.control.run('resume')
        self.assertFalse(idle['changed'])
        self.assertEqual(self.comp.counter, 0)
        self.assertEqual(len(self.comp.requests), sent)          # no Enable without our own pause

    def test_resume_leaves_a_pause_someone_else_holds(self):
        self.comp.put_u32(TG, 1)           # focus loss already holds the counter
        self.control.run('pause')
        self.assertEqual(self.comp.counter, 2)
        out = self.control.run('resume')
        self.assertEqual(self.comp.counter, 1)
        self.assertIn('stays stopped', out['reason'])

    def test_hold_configures_the_gate_once_and_release_opens_it(self):
        out = self.control.run('hold', timeout_seconds=30)
        self.assertEqual(self.comp.requests[0], [('gatecfg', 'aa50a0', 'aa5040', '11f6394')])
        self.assertEqual(self.comp.requests[1], [('gate',), ('hold', 'on', '7530')])
        self.assertTrue(self.comp.held)
        self.assertTrue(out['held'])
        self.assertTrue(out['changed'])
        self.assertEqual(out['timeoutSeconds'], 30)

        again = self.control.run('hold')
        self.assertFalse(any(op[0] == 'gatecfg' for op in self.last_ops()))
        self.assertFalse(again['changed'])
        self.assertEqual(len([r for r in self.comp.requests if r and r[0][0] == 'gatecfg']), 1)

        out = self.control.run('release')
        self.assertFalse(self.comp.held)
        self.assertTrue(out['changed'])
        self.assertFalse(out['held'])
        self.assertFalse(self.control.run('release')['changed'])

    def test_step_runs_exact_frames_at_a_fixed_rate(self):
        out = self.control.run('step', frames=60, fps=60)
        self.assertEqual(self.comp.requests[0], [('gatecfg', 'aa50a0', 'aa5040', '11f6394')])
        # E22: the fixed step the game had is read first and put back at the stop frame (here: off, 0 fps)
        self.assertEqual(self.comp.requests[1], [('rd', '11f6398', '4')])
        self.assertEqual(self.comp.requests[2], FRAME_READ + [('call', '86d4c0', 'v', '11f6394', '0', '3c'),
                                                              ('step', '3c'),
                                                              ('call', '86d4c0', 'v', '11f6394', '0', '0'),
                                                              ('gate',)])
        self.assertEqual(out['advanced'], 60)
        self.assertEqual((out['frameBefore'], out['frameAfter']), (100, 160))
        self.assertEqual(out['fixedFps'], 60)
        self.assertTrue(out['held'])
        self.assertTrue(self.comp.held)

    def test_step_without_fps_sends_no_clamp(self):
        out = self.control.run('step', frames=5)
        self.assertEqual(self.last_ops(), FRAME_READ + [('step', '5'), ('gate',)])
        self.assertIsNone(out['fixedFps'])
        self.assertEqual(out['advanced'], 5)

    def test_step_reports_advanced_from_the_companion_frame(self):
        self.comp.put_u32(FC, 300)
        out = self.control.run('step', frames=5)
        # before comes from the pred eval top of the companion's frame, after from the step result
        self.assertEqual((out['frameBefore'], out['frameAfter'], out['advanced']), (300, 305, 5))
        self.assertEqual(self.comp.frame, 305)
        self.assertNotIn(('rd', '11a2fe0', '4'), self.last_ops())

    def test_step_and_fixed_refuse_bad_numbers(self):
        for kwargs in ({'frames': 0}, {'frames': 10001}, {'frames': 5, 'fps': 1001}):
            with self.assertRaises(ValueError):
                self.control.run('step', **kwargs)
        with self.assertRaises(ValueError) as cm:
            self.control.run('step')
        self.assertIn('frames=N', str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            self.control.run('fixed')
        self.assertIn('fps=0', str(cm.exception))
        self.assertEqual(self.comp.requests, [])

    def test_fixed_sets_and_turns_off_the_step(self):
        out = self.control.run('fixed', fps=60)
        self.assertEqual(self.last_ops()[0], ('call', '86d4c0', 'v', '11f6394', '0', '3c'))
        self.assertEqual(self.last_ops()[1], ('rd', '11f6398', '4'))
        self.assertEqual(out['fixedFps'], 60)
        self.assertEqual(out['fixedStepMs'], 16.6667)
        off = self.control.run('fixed', fps=0)
        self.assertEqual(self.last_ops()[0], ('call', '86d4c0', 'v', '11f6394', '0', '0'))
        self.assertIsNone(off['fixedFps'])
        self.assertIsNone(off['fixedStepMs'])

    def test_speed_writes_both_multiplier_floats(self):
        out = self.control.run('speed', x=4.0)
        self.assertIn(('wr', '11ac3a0', '00008040'), self.last_ops())
        self.assertIn(('wr', '11ac3a4', '00008040'), self.last_ops())
        self.assertEqual(self.comp.get(MULT, 4), struct.pack('<f', 4.0))
        self.assertEqual(self.comp.get(MULT_T, 4), struct.pack('<f', 4.0))
        self.assertEqual(out['previous'], 1.0)
        self.assertEqual(out['multiplier'], 4.0)
        for bad in (0.0, 11.0):
            with self.assertRaises(ValueError):
                self.control.run('speed', x=bad)

    def test_status_measures_the_real_frame_rate(self):
        out = self.control.run('status')
        self.assertEqual(out['verdict'], 'The game is running at frame 100')
        self.assertFalse(out['held'])
        self.assertEqual(out['realFps'], 60.0)
        self.assertEqual(out['multiplier'], 1.0)
        self.assertIsNone(out['fixedFps'])
        self.assertEqual(out['clockCounter'], 0)
        self.assertFalse(out['pausedByUs'])
        self.assertIsNone(out['next'])

    def test_status_while_held_has_no_fps_and_says_what_next(self):
        self.control.run('hold')
        out = self.control.run('status')
        self.assertTrue(out['held'])
        self.assertIsNone(out['realFps'])
        self.assertIn('release', out['next'])
        self.assertIn('held', out['reason'])

    def test_status_reports_the_pause_and_fixed_step(self):
        self.control.run('pause')
        self.control.run('fixed', fps=50)
        out = self.control.run('status')
        self.assertTrue(out['pausedByUs'])
        self.assertEqual(out['clockCounter'], 1)
        self.assertEqual(out['fixedFps'], 50.0)
        self.assertIn('paused by this tool', out['reason'])

    def test_game_not_running_next_step_is_launch(self):
        self.live.game.proc = None
        with self.assertRaises(ConvError) as cm:
            self.control.run('status')
        self.assertEqual(cm.exception.code, 'NOT_RUNNING')
        self.assertIn('fnv_game launch', str(cm.exception))
        self.assertEqual(self.comp.requests, [])

    def test_gate_refusal_is_reported_with_the_fix(self):
        self.comp.refuse_gatecfg = True
        with self.assertRaises(ConvError) as cm:
            self.control.run('hold')
        self.assertEqual(cm.exception.code, 'GATE_UNSUPPORTED')
        self.assertIn('fnv_companion_install', str(cm.exception))
        self.assertFalse(self.comp.held)

    def test_engine_map_disagreement_refuses_before_any_call(self):
        good = self.live.functions[DISABLE]['signature']
        self.live.functions[DISABLE]['signature'] = 'public: void __thiscall Something(int)'
        with self.assertRaises(ConvError) as cm:
            self.control.run('pause')
        self.assertEqual(cm.exception.code, 'ENGINE_MAP_MISMATCH')
        self.assertIn('fnv_setup_check', str(cm.exception))
        self.assertEqual(self.comp.requests, [])
        self.live.functions[DISABLE]['signature'] = good
        del self.live.functions[ENABLE]
        with self.assertRaises(ConvError) as cm:
            self.control.run('hold')
        self.assertEqual(cm.exception.code, 'ENGINE_MAP_MISMATCH')
        self.assertIn('0x00AA5040', str(cm.exception))

    def test_a_new_game_process_resets_the_flags_and_the_gate_config(self):
        self.control.run('pause')
        self.control.run('hold')
        self.live.game.proc = (5555, 'FalloutNV.exe')
        out = self.control.run('resume')
        self.assertFalse(out['changed'])
        self.control.run('hold')
        self.assertEqual(self.comp.requests[-2][0], ('gatecfg', 'aa50a0', 'aa5040', '11f6394'))

    def test_act_actions_are_refused_without_full_engine_control(self):
        svc = LiveService.__new__(LiveService)
        svc.project = SimpleNamespace(fnv=SimpleNamespace(engine_control='read'), project_id='test')
        svc._game = None
        result = svc.run('fnv_time', lambda: self.control.run('pause'), 'act')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'CONTROL_DISABLED')
        self.assertEqual(self.comp.requests, [])
        svc.project.fnv.engine_control = 'full'
        result = svc.run('fnv_time', lambda: self.control.run('pause'), 'act')
        self.assertTrue(result['ok'])
        self.assertEqual(result['outcome'], 'acknowledged')

    def test_unknown_action_lists_the_valid_ones(self):
        with self.assertRaises(ConvError) as cm:
            self.control.run('jump')
        self.assertEqual(cm.exception.code, 'UNKNOWN_ACTION')
        self.assertIn('pause', str(cm.exception))


class WaitForTests(TimeTestCase):
    def test_frames_wait_restores_an_open_gate(self):
        out = self.wait.run(frames=10)
        self.assertEqual(self.comp.requests[0], [('gatecfg', 'aa50a0', 'aa5040', '11f6394')])
        self.assertEqual(self.comp.requests[1], [('gate',), ('rd', '11a2fe0', '4'), ('rd', '11f6394', '4'),
                                                 ('rd', '11f6398', '4'), ('rd', '11ac3a0', '4'),
                                                 ('rd', '11ac3a4', '4')])
        # the base frame is read with the gate closed, so nothing runs between the base and the wait
        self.assertEqual(self.comp.requests[2], [('hold', 'on', 'ea60')] + FRAME_READ)
        pred = compile_predicate('frame >= 110')['hex']
        self.assertEqual(self.comp.requests[3], [('pred', 'set', '3f', pred), ('until', '3f', 'c'),
                                                 ('hold', 'off'), ('pred', 'clear', '3f')])
        self.assertEqual(out['frame'], 110)
        self.assertEqual(out['frames'], 10)
        self.assertFalse(out['heldBefore'])
        self.assertFalse(self.comp.held)
        # the wait slot is cleared; the frame-reading slot (62) is reused by every frame read and stays set
        self.assertNotIn(fnv_time.PRED_ID, self.comp.preds)
        self.assertEqual(set(self.comp.preds), {fnv_time.FRAME_SLOT})

    def test_a_debugger_stop_during_the_wait_is_reported_as_stopped(self):
        original = type(self.comp).batch

        def batch(comp, ops, timeout=None):
            if any(op[0] == 'until' for op in ops):
                return [{'ok': True}, {'ok': False, 'error': 'stopped', 'met': False, 'frames': 7, 'frame': 107}] +                        [{'ok': True}] * (len(ops) - 2)
            return original(comp, ops, timeout)
        from unittest import mock
        with mock.patch.object(type(self.comp), 'batch', batch):
            with self.assertRaises(fnv_time.ConvError) as cm:
                self.wait.run(frames=10)
        self.assertEqual(cm.exception.code, 'STOPPED')
        self.assertIn('fnv_debug', str(cm.exception))

    def test_frames_wait_keeps_a_held_gate_held(self):
        self.control.run('hold')
        out = self.wait.run(frames=5)
        self.assertNotIn(('hold', 'off'), self.last_ops())
        self.assertEqual(out['frames'], 5)
        self.assertTrue(self.comp.held)

    def test_predicate_text_waits_until_true(self):
        out = self.wait.run(predicate='[0x011A2FE0]:u32 >= 105')
        self.assertEqual(out['frames'], 5)
        self.assertEqual(out['predicate'], '[0x011A2FE0]:u32 >= 105')
        self.assertIn('predicateOps', out)

    def test_predicate_that_never_holds_times_out_and_restores_the_gate(self):
        with self.assertRaises(ConvError) as cm:
            self.wait.run(predicate='frame < 0', max_frames=20)
        self.assertEqual(cm.exception.code, 'WAIT_TIMEOUT')
        self.assertIn('Next:', str(cm.exception))
        self.assertEqual(self.comp.frame, 120)
        self.assertFalse(self.comp.held)

    def test_seconds_use_the_fixed_step_and_the_timescale(self):
        self.control.run('fixed', fps=50)            # 20 ms steps: 50 frames a second
        self.comp.put_f32(MULT, 2.0)                 # game seconds run at half speed
        out = self.wait.run(seconds=2)
        self.assertEqual(out['estimatedFrames'], 50)
        self.assertEqual(out['frames'], 50)
        self.assertEqual(out['rateSource'], 'fixed step')
        out = self.wait.run(seconds=2, clock='real')
        self.assertEqual(out['estimatedFrames'], 120)    # real seconds: the measured 60 fps, not the step (G6)

    def test_seconds_while_held_assume_60_fps(self):
        self.control.run('hold')
        out = self.wait.run(seconds=1)
        self.assertEqual(out['estimatedFrames'], 60)
        self.assertTrue(out['rateSource'].startswith('assumed 60'))

    def test_seconds_with_zero_timescale_is_refused(self):
        self.comp.put_f32(MULT, 0.0)
        with self.assertRaises(ConvError) as cm:
            self.wait.run(seconds=1)
        self.assertEqual(cm.exception.code, 'BAD_TIMESCALE')
        self.assertIn('speed with x=1', str(cm.exception))

    def test_loaded_checks_the_cell_on_a_later_round(self):
        self.comp.cells = ['00000000', '0002A2B3']
        out = self.wait.run(loaded=True)
        self.assertEqual(out['rounds'], 2)
        self.assertEqual(out['cell'], '0002A2B3')
        self.assertEqual(out['frames'], 2)
        self.assertFalse(self.comp.held)
        loaded_text = fnv_time.LOADED_TEXT
        first = [op for request in self.comp.requests for op in request
                 if op[0] == 'pred' and op[1] == 'set' and op[2] == '3f'][0]
        self.assertEqual(first[3], compile_predicate(loaded_text + ' and frame >= 102')['hex'])

    def test_loaded_flags_block_until_clear(self):
        self.comp.put_u8(fnv_time.LOADING_FLAG, 1)
        with self.assertRaises(ConvError) as cm:
            self.wait.run(loaded=True, timeout_seconds=1)
        self.assertEqual(cm.exception.code, 'WAIT_TIMEOUT')
        self.assertIn('fnv_status', str(cm.exception))
        self.assertFalse(self.comp.held)

    def test_exactly_one_condition(self):
        for kwargs in ({}, {'frames': 5, 'loaded': True}, {'predicate': 'frame >= 1', 'seconds': 2}):
            with self.assertRaises(ValueError) as cm:
                self.wait.run(**kwargs)
            self.assertIn('exactly one', str(cm.exception))
        self.assertEqual(self.comp.requests, [])

    def test_bad_predicate_text_is_reported_with_its_column(self):
        with self.assertRaises(ConvError) as cm:
            self.wait.run(predicate='frame ==')
        self.assertEqual(cm.exception.code, 'BAD_PREDICATE')
        self.assertIn('column 9', str(cm.exception))
        self.assertFalse(any(op[0] == 'until' for request in self.comp.requests for op in request))

    def test_game_not_running_next_step(self):
        self.live.game.proc = None
        with self.assertRaises(ConvError) as cm:
            self.wait.run(frames=3)
        self.assertEqual(cm.exception.code, 'NOT_RUNNING')
        self.assertIn('fnv_game launch', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
