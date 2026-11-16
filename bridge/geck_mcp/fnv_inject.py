"""Frame-exact input for the live tools (Plan 2, W1): the input injector of the engine companion. Wire ops:
docs/FNV-COMPANION-V2.md section 7.

The injector is a hook after the engine's input poll (InputPoller::Controls_UpdateValues). `inpcfg` installs it once
per game process. Jobs then hold a key, a mouse button, mouse deltas or a control for an exact number of frames; each
job ends by itself. Before anything is sent, the poll and force functions are checked against the engine map.
The OSInputGlobals pointer is a static: the engine map does not list it yet, so its address comes from the decomp
units and the result says so.
"""
from .fnv_conv import ConvError, verdict
from .fnv_link import word

POLL_FN = 0x00A23010              # InputPoller::Controls_UpdateValues (the map has it unnamed; signature checked)
OS_GLOBALS_PTR = 0x011F35CC       # the pointer to OSInputGlobals (static)
FORCE_FN = 0x00A24280             # OSInputGlobals::Controls_ForceUserAction (the map: OSInputGlobals::SetControlHeld)
FORCE_NAMES = ('ForceUserAction', 'SetControlHeld')
PROLOGUE_BYTES = 24               # bytes read at the poll function for the stolen-byte count (as fnv_control does)
CONTROL_MAX = 12                  # controls 0..12 go through the engine's own force call
KEY_MAX = 255                     # DirectInput scan codes this module sends
BUTTON_MAX = 7
FRAMES_MAX = 10000                # 0x2710, the same cap as a frame step
DETERMINISTIC_FPS = 60            # the fixed frame step of a deterministic run
DETERMINISTIC_SEED = 1            # the random seed of a deterministic run


def engine_checks(engine):
    """The poll and force functions by address, each checked against the engine map: thiscall with the signature
    this module expects (and the name, where the map has one). Otherwise the tool refuses before any call."""
    out = {}
    for key, address, argument, names in (('poll', POLL_FN, '(void)', None),
                                          ('force', FORCE_FN, '(int)', FORCE_NAMES)):
        record = engine.functions.get(address) or {}
        text = record.get('signature') or ''
        name = record.get('name') or ''
        if not record or '__thiscall' not in text or argument not in text or \
                (names and not any(n in name for n in names)):
            raise ConvError('ENGINE_MAP_MISMATCH', 'The engine map does not have the input function %s at 0x%08X with '
                            'a thiscall signature %s. Next: call fnv_setup_check; the engine map may be from another '
                            'build.' % (key, address, argument), {'address': '0x%08X' % address, 'found': text or None})
        out[key] = address
    return out


def static_info(engine, address):
    """Whether the engine map lists a static at this address (the OSInputGlobals pointer is read from the decomp)."""
    hit = (getattr(engine, 'statics_by_address', None) or {}).get(address)
    return {'address': '0x%08X' % address, 'inEngineMap': bool(hit), 'name': hit.get('name') if hit else None}


def shared_time(live):
    """The one TimeControl and WaitFor of a live service (the gate state and the pause flag must not split in two)."""
    from .fnv_time import TimeControl, WaitFor
    control = getattr(live, 'time_control', None)
    if control is None:
        control = TimeControl(live)
        live.time_control = control
    waiter = getattr(live, 'wait_for', None)
    if waiter is None:
        waiter = WaitFor(control)
        live.wait_for = waiter
    return control, waiter


class Injector:
    """Installs the injector once per game process and queues jobs. wait=True (the default) runs frames until the
    job has ended, so a caller that goes on after it sees the input's effect."""

    def __init__(self, live):
        self.live = live
        self.pid = None
        self.configured = False
        self.info = None

    @classmethod
    def of(cls, live):
        injector = getattr(live, 'injector', None)
        if injector is None:
            injector = cls(live)
            live.injector = injector
        return injector

    # ------------------------------------------------------------ plumbing
    def batch(self, ops, timeout=None):
        return self.live.link.batch(ops, timeout=timeout)

    def require_game(self):
        proc = self.live.game.process()
        if not proc:
            raise ConvError('NOT_RUNNING', 'Input needs the game running. Next: call fnv_game launch with a save or a cell.')
        if proc[0] != self.pid:
            self.pid, self.configured = proc[0], False
        return proc[0]

    def ensure(self):
        """inpcfg once per game process. A companion that already has an injector installed (from an earlier bridge)
        is taken as it is."""
        self.require_game()
        if self.configured:
            return
        status = self.batch([('inp',)])[0]
        if status.get('installed') is True:
            self.configured = True
            return
        engine = self.live.engine()
        fns = engine_checks(engine)
        self.info = {'poll': '0x%08X' % POLL_FN, 'force': '0x%08X' % FORCE_FN,
                     'osGlobals': static_info(engine, OS_GLOBALS_PTR)}
        raw_result = self.batch([('rd', word(POLL_FN), '%x' % PROLOGUE_BYTES)])[0]
        if not raw_result.get('ok'):
            raise ConvError('MEMORY_UNREADABLE', 'Cannot read the input poll function at 0x%08X (%s). Next: call '
                            'fnv_status.' % (POLL_FN, raw_result.get('error')), raw_result)
        raw = bytes.fromhex(raw_result['hex'])
        deep = getattr(self.live, 'deep', None)
        clean = deep._clean_bytes(POLL_FN, PROLOGUE_BYTES) if deep is not None else None
        if clean is not None and raw[:8] != clean[:8] and raw[0] != 0xE9:
            raise ConvError('PATCHED_ELSEWHERE', 'Another module changed the start of the input poll function in a way '
                            'an injector cannot chain onto. Next: call fnv_patch_map.',
                            {'live': raw[:16].hex(' '), 'clean': clean[:16].hex(' ')})
        from .fnv_deep import HookError, decode_prologue
        try:
            stolen, relocs = decode_prologue(raw)
        except HookError as e:
            raise ConvError('UNSUPPORTED_PROLOGUE', 'The input poll function cannot be hooked: %s. Next: call fnv_patch_map.'
                            % e, {'code': e.code})
        if relocs:
            # The contract's inpcfg takes no relocation list, so a prologue with a rel32 cannot be installed.
            raise ConvError('PROLOGUE_RELOCATION', 'The first %d bytes of the input poll function hold a relative jump '
                            'or call, which inpcfg cannot relocate. Next: call fnv_companion_install (a newer companion).'
                            % stolen, {'stolen': stolen, 'relocations': relocs})
        result = self.batch([('inpcfg', word(fns['poll']), word(stolen), word(OS_GLOBALS_PTR), word(fns['force']))])[0]
        if not result.get('ok'):
            raise ConvError('INPUT_REFUSED', 'The engine companion refused the input injector (%s). Next: call '
                            'fnv_status; if the companion is old, call fnv_companion_install.' % result.get('error'), result)
        self.configured = True

    def wait_frames(self, frames):
        """Run frames until a job of `frames` frames has ended. One extra frame covers the frames that passed between
        the queue request and the wait's base frame."""
        _, waiter = shared_time(self.live)
        return waiter.run(frames=frames + 1, timeout_seconds=30 + frames / 20.0)

    def job(self, kind, detail, frames, op, wait):
        if not 1 <= int(frames) <= FRAMES_MAX:
            raise ValueError('frames must be from 1 to %d' % FRAMES_MAX)
        self.ensure()
        result = self.batch([op])[0]
        if not result.get('ok'):
            raise ConvError('INPUT_REFUSED', 'The engine companion refused the %s job (%s). Next: call fnv_status; if the '
                            'companion is old, call fnv_companion_install.' % (kind, result.get('error')), result)
        recorder = getattr(self.live, 'replay_recorder', None)
        if recorder is not None:            # fnv_replay record: the job and the frame it was queued at
            recorder(op, result.get('frame'))
        waited = self.wait_frames(int(frames)) if wait else None
        return verdict('%s for %d frames' % (detail, int(frames)),
                       'The job ends by itself after its frames; the engine input poll applies it each frame.'
                       if wait else 'Queued; it ends by itself after its frames.',
                       queued=True, kind=kind, frames=int(frames), job=result.get('job'),
                       frameQueued=result.get('frame'), waited=bool(wait),
                       frameAfter=waited.get('frame') if waited else None)

    # ------------------------------------------------------------ jobs
    def control(self, code, frames, wait=True):
        code = int(code)
        if not 0 <= code <= CONTROL_MAX:
            raise ValueError('control code must be from 0 to %d' % CONTROL_MAX)
        return self.job('control', 'Control %d held' % code, frames, ('inp', 'ctl', word(code), word(frames)), wait)

    def key(self, dik, frames, wait=True):
        dik = int(dik)
        if not 0 <= dik <= KEY_MAX:
            raise ValueError('key must be a DirectInput scan code from 0 to %d' % KEY_MAX)
        return self.job('key', 'Key %d held' % dik, frames, ('inp', 'key', word(dik), word(frames)), wait)

    def button(self, index, frames, wait=True):
        index = int(index)
        if not 0 <= index <= BUTTON_MAX:
            raise ValueError('mouse button must be from 0 to %d' % BUTTON_MAX)
        return self.job('button', 'Mouse button %d held' % index, frames,
                        ('inp', 'btn', word(index), word(frames)), wait)

    def mouse(self, dx, dy, frames, wait=True):
        return self.job('mouse', 'Mouse moved by (%d, %d) each frame' % (int(dx), int(dy)), frames,
                        ('inp', 'mouse', word(int(dx)), word(int(dy)), word(frames)), wait)

    def clear(self):
        self.require_game()
        result = self.batch([('inp', 'clear')])[0]
        if not result.get('ok'):
            raise ConvError('INPUT_REFUSED', 'The engine companion did not clear the input jobs (%s). Next: call '
                            'fnv_status.' % result.get('error'), result)
        return verdict('Input jobs cleared', 'Every queued job is dropped; the keys and buttons are released.',
                       changed=True)

    def status(self):
        self.require_game()
        result = self.batch([('inp',)])[0]
        jobs = result.get('jobs') or []
        return verdict('%d input job%s queued' % (len(jobs), '' if len(jobs) == 1 else 's'),
                       'The injector is installed.' if result.get('installed') else 'The injector is not installed yet.',
                       installed=bool(result.get('installed')), jobs=jobs, addresses=self.info)


def scan_to_job(injector, scan, frames, wait=True):
    """A DirectInput scan code: 256 and up is a mouse button (256 + button), anything else a key."""
    if scan >= 256:
        return injector.button(scan - 256, frames, wait)
    return injector.key(scan, frames, wait)
