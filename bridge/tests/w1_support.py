"""Shared fakes for the Plan 2 W1 tests (fnv_inject, fnv_test steps, fnv_session, fnv_health): the fake companion of
test_fnv_time with the injector ops (inp, inpcfg) and an event ring, and the live and game stand-ins. No game, no pipe.
"""
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_fnv_time import FakeCompanion, DISABLE, ENABLE, CLAMP  # noqa: E402
from geck_mcp.fnv_inject import POLL_FN, FORCE_FN  # noqa: E402
from geck_mcp.fnv_time import TimeControl, WaitFor  # noqa: E402

# A prologue the decoder takes whole: push ebp; mov ebp,esp; sub esp,0x10 (6 bytes, no relative jump or call).
PROLOGUE = bytes([0x55, 0x8B, 0xEC, 0x83, 0xEC, 0x10, 0x53, 0x56, 0x57, 0x8B, 0x45, 0x08]) + b'\x90' * 12


class EventCompanion(FakeCompanion):
    """FakeCompanion plus the injector's inp/inpcfg ops and an event ring (events(since) like the link)."""

    def __init__(self):
        super().__init__()
        self.installed = False
        self.inp_jobs = []
        self.inpcfg_calls = []
        self.refuse_inp = False
        self.ring = []
        self.seq = 0
        self.put(POLL_FN, PROLOGUE)

    def add_event(self, kind, text=''):
        self.seq += 1
        self.ring.append({'seq': self.seq, 'type': kind, 'text': text})

    def events(self, since=0):
        return {'seq': self.seq, 'events': [e for e in self.ring if e['seq'] > since]}

    def op(self, name, a):
        if name == 'inp':
            if not a:
                return {'jobs': list(self.inp_jobs), 'installed': self.installed}
            if a[0] == 'clear':
                self.inp_jobs = []
                return {'ok': True}
            if self.refuse_inp:
                return {'ok': False, 'error': 'busy'}
            kind = {'ctl': 'control', 'key': 'key', 'btn': 'button', 'mouse': 'mouse'}[a[0]]
            self.inp_jobs.append({'kind': kind, 'args': a[1:]})
            return {'ok': True, 'job': len(self.inp_jobs), 'frame': self.frame, 'jobs': len(self.inp_jobs)}
        if name == 'inpcfg':
            self.inpcfg_calls.append(list(a))
            self.installed = True
            return {'ok': True}
        return super().op(name, a)


class W1Game:
    def __init__(self):
        self.proc = (4242, 'FalloutNV.exe')
        self.stopped = []
        self.quit_calls = 0
        self.crash = None

    def process(self):
        return self.proc

    def keep_unpaused(self, *args, **kwargs):
        return None

    def stop_task(self, task):
        self.stopped.append(task)

    def quit(self):
        self.quit_calls += 1
        self.proc = None
        return {'quit': True}

    def crash_since(self, since):
        return self.crash


class W1Live:
    """The live service stand-in: link (an EventCompanion), game, engine map, journal, clock and project."""

    def __init__(self):
        self.link = EventCompanion()
        self.game = W1Game()
        self.functions = {
            DISABLE: {'name': 'GameTimerRecord::FUN_00aa50a0', 'status': 'unnamed',
                      'signature': 'public: void __thiscall GameTimerRecord::FUN_00aa50a0(void)'},
            ENABLE: {'name': 'GameTimerRecord::Enable', 'status': 'decomp',
                     'signature': 'public: void __thiscall GameTimerRecord::Enable(void)'},
            CLAMP: {'name': 'C86D4C0Timer::FUN_0086d4c0', 'status': 'unnamed',
                    'signature': 'public: void __thiscall C86D4C0Timer::FUN_0086d4c0(unsigned int)'},
            POLL_FN: {'name': 'InputPoller::Controls_UpdateValues', 'status': 'unnamed',
                      'signature': 'public: void __thiscall InputPoller::Controls_UpdateValues(void)'},
            FORCE_FN: {'name': 'OSInputGlobals::SetControlHeld', 'status': 'decomp',
                       'signature': 'public: void __thiscall OSInputGlobals::SetControlHeld(int)'},
        }
        self.journal = SimpleNamespace(entries=lambda *a, **k: [], last_seq=lambda: 0, add=lambda e, *a: dict(e, id='j'),
                                       mark_undone=lambda ids: None, confirm=lambda i, after=None: None)
        self.reverted = []
        self.project = SimpleNamespace(state_dir=Path('.'), fnv=SimpleNamespace(tests_dir=None))
        self.time_control = TimeControl(self, sleep=self.link.sleep, clock=self.link.clock)
        self.wait_for = WaitFor(self.time_control)

    def engine(self):
        return SimpleNamespace(functions=self.functions)

    def script_errors(self, since='last'):
        return {'errors': []}

    def revert_all(self, since=None):
        self.reverted.append(since)
        return {'reverted': 0}
