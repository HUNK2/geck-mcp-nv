"""Source-level debugger for the retail game (Plan 2, W2): fnv_debug. Breaks, stops, stacks, locals, eval, stepping.

Design: plan2/B-debug.md section 1; companion ops: docs/FNV-COMPANION-V2.md section 8. A break at a function's first
byte becomes a hook hold when the hook machinery accepts the function (kind `hook` stop, the register block as
context); anything else is a trap breakpoint (`bp set`: hardware for up to 4 main-thread breaks, int3 beyond).
`fault` turns on the companion's crash hold. Stops are read with `stops`; the stop's registers come from its
CONTEXT (`bp`, `step`, `fault`) or its hook register block (`hook`), read with `rd`.

Frames use the EBP chain of the game's /Od /Oy- code. A hook stop has no frame of its own yet: its parameters
are read from the saved stack arguments and its locals are reported as not set up. Locals and line data come
from the symbol database; without it, function names and breaks still work.
"""
import re
import struct
import threading
import time
from pathlib import Path

from engine_map import symdb_schema

from . import fnv_deep, fnv_engine, fnv_procmem, fnv_symdb
from .fnv_conv import ConvError, dispatch, verdict
from .fnv_deep import HookError, decode_prologue
from .fnv_link import LinkError, hexs, word
from .fnv_pred import ProcState, compile_predicate

_is_pointer = fnv_symdb.is_pointer_text

REGISTERS = symdb_schema.REGISTERS
REG_SAVE = ('eax', 'ecx', 'edx', 'ebx', 'esp', 'ebp', 'esi', 'edi', 'eflags')
# Saved register block of a hook stop (the layout the hooks save today): eflags edi esi ebp esp ebx edx ecx eax slot ret args
HOOK_BLOCK = {'eflags': 0x00, 'edi': 0x04, 'esi': 0x08, 'ebp': 0x0C, 'esp': 0x10, 'ebx': 0x14, 'edx': 0x18,
              'ecx': 0x1C, 'eax': 0x20}
HOOK_RET = 0x28
HOOK_ARGS = 0x2C
HOOK_MAX_ARGS = 8
HOOK_BLOCK_READ = HOOK_ARGS + 4 * HOOK_MAX_ARGS
BP_IDS = 64
HW_SLOTS = 4
STEP_LIMIT = 2000
STOP_WAIT = 10.0
WINDOW = 5
TEMP_BP = 255
ACTIONS = ('break', 'status', 'stack', 'locals', 'eval', 'set', 'continue', 'step', 'until', 'return', 'skip',
           'freeze', 'thaw', 'threads', 'dump')
READ_ACTIONS = ('status', 'stack', 'locals')
STEP_MODES = ('instruction', 'into', 'over', 'out', 'line')
OUTSIDE_ACTIONS = ('freeze', 'thaw', 'threads', 'dump')   # work from outside the process: no companion needed
PRED_SLOT_FIRST, PRED_SLOT_LAST = 32, 55   # predicate slots for break conditions (waits use 62 and 63)
MAIN_SLOT = PRED_SLOT_LAST                 # holds the program `main`: the thread filter of thread=main hook holds
SCALARS = {'char': 1, 'unsigned char': 1, 'bool': 1, 'short': 2, 'unsigned short': 2, 'int': 4, 'unsigned int': 4,
           'long': 4, 'unsigned long': 4, 'float': 4, 'double': 8, 'u8': 1, 'i8': 1, 'u16': 2, 'i16': 2, 'u32': 4,
           'i32': 4, 'f32': 4}
MEMORY_TYPES = {'u8': 'unsigned char', 'i8': 'char', 'u16': 'unsigned short', 'i16': 'short', 'u32': 'unsigned int',
                'i32': 'int', 'f32': 'float'}
TOKEN = re.compile(r'\s*(0[xX][0-9A-Fa-f]+|\d+\.\d+|\d+|\$?[A-Za-z_]\w*|->|[.\[\]:()])')


def _addr(value):
    """An address or id from the companion: a hex string (or an int)."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return int(str(value).strip(), 16)


def _num(value):
    """A decimal id from the companion (thread ids), or an int."""
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text, 16) if text.lower().startswith('0x') else int(text)


def _hx(value):
    return '0x%08X' % value


def _u32(raw, offset):
    return struct.unpack_from('<I', raw, offset)[0]


def decode(data, type_text, size):
    """Python value of a variable's bytes, with the rules the hang doctor uses: pointers and integers as ints
    (pointers unsigned), float and double as floats, anything that is not 1, 2, 4 or 8 bytes as None."""
    text = type_text or ''
    if size == 4 and _is_pointer(text):
        return struct.unpack('<I', data)[0]
    if text == 'float' and size == 4:
        return struct.unpack('<f', data)[0]
    if text == 'double' and size == 8:
        return struct.unpack('<d', data)[0]
    if size not in (1, 2, 4, 8):
        return None
    unsigned = 'unsigned' in text or text.startswith(('UInt', 'uint', 'bool', 'size_t'))
    return int.from_bytes(data, 'little', signed=not unsigned)


def _class_name(type_text):
    """'const Actor *' -> 'Actor'; None for scalars and arrays."""
    text = re.sub(r'\b(const|volatile)\b', '', type_text or '').replace('*', '').replace('&', '').strip()
    return text if text and not re.search(r'[\[\]]', text) and text not in SCALARS else None


def _scalar_size(type_text):
    text = (type_text or '').strip()
    if _is_pointer(text):
        return 4
    return SCALARS.get(text)


def _parse_number(text):
    """An int (decimal or 0x hex) or a float from command text."""
    try:
        return int(str(text).strip(), 0)
    except ValueError:
        pass
    try:
        return float(str(text).strip())
    except ValueError:
        raise ConvError('BAD_VALUE', 'Not a number: %r. Next: pass a decimal, a 0x hex number or a float.' % text)


def _tokens(text):
    out, pos, text = [], 0, str(text)
    while pos < len(text):
        if text[pos:].strip() == '':
            break
        m = TOKEN.match(text, pos)
        if not m:
            raise ConvError('BAD_EXPRESSION', 'Bad expression at column %d: %r. Next: use names, ->, ., [index], '
                            'registers as $eax, or [0x...]:u32.' % (pos + 1, text[pos:pos + 1]))
        out.append(m.group(1))
        pos = m.end()
    return out


def _call_length(code):
    """Length of the call instruction at the start of code (bytes), or None when it is not a call."""
    try:
        length, _ = fnv_deep._instruction(code, 0)
    except Exception:    # not a decodable instruction: not a call either
        return None
    i = 0
    while i < len(code) - 2 and code[i] in (0x64, 0x65, 0x66, 0x2E, 0x3E, 0x26, 0x36, 0xF2, 0xF3):
        i += 1
    if code[i] == 0xE8 or (code[i] == 0xFF and ((code[i + 1] >> 3) & 7) == 2):
        return length
    return None


class Debugger:
    """The fnv_debug actions over one live service. Breaks are kept here by number (`breaks`), the companion's
    own ids are kept in each break record."""

    def __init__(self, live, sleep=time.sleep, clock=time.monotonic):
        self.live = live
        self.sleep = sleep
        self.clock = clock
        self.pid = None
        self.start = None
        self.breaks = {}
        self.next_number = 1
        self.frozen = []
        self.pending_to = set()          # temporary `to` sites left armed (a timeout, or another stop came first)
        self.stop_breaks = {}            # stop seq -> the break number it was taken for (kept after a clear; G13)
        # One lock for everything that picks a bp id or a predicate slot and then sets it: shared with fnv_flow,
        # fnv_blame and the script debugger, since tool calls run on parallel worker threads.
        self.lock = threading.RLock()
        self._claimed_slots = set()      # predicate slots taken by other tools (fnv_flow) out of this pool
        self.state = ProcState(live)

    @property
    def link(self):
        return self.live.link

    # ================================================================ state kept across server restarts
    def _persist(self):
        """Breaks and frozen thread ids go to the state file (keyed by pid and start): they live in the game, and the
        server may restart (a supervisor does it on every code change)."""
        if self.pid is None:
            return
        records = [dict(b) for _, b in sorted(self.breaks.items())]
        self.state.save('dbg_breaks', self.pid, self.start,
                        {'next': self.next_number, 'breaks': records} if records else None)
        self.state.save('dbg_frozen', self.pid, self.start, list(self.frozen) or None)
        self.state.save('dbg_pending_to', self.pid, self.start, sorted(self.pending_to) or None)

    def _restore(self):
        saved = self.state.load('dbg_breaks', self.pid, self.start) or {}
        self.breaks = {int(b['number']): b for b in saved.get('breaks', []) if isinstance(b, dict) and 'number' in b}
        self.next_number = max([int(saved.get('next') or 1)] + [n + 1 for n in self.breaks])
        self.frozen = [int(t) for t in (self.state.load('dbg_frozen', self.pid, self.start) or [])]
        self.pending_to = {int(a) for a in (self.state.load('dbg_pending_to', self.pid, self.start) or [])}
        self.stop_breaks = {}      # stop seqs start again in a new game process (S6 review A M5)

    # ================================================================ entry point
    def run(self, action, **args):
        if action not in ACTIONS:
            raise ConvError('UNKNOWN_ACTION', 'Unknown action %r. Valid actions: %s.' % (action, ', '.join(ACTIONS)),
                            {'actions': list(ACTIONS)})
        if action in OUTSIDE_ACTIONS:
            self._process()       # freeze, thaw, threads and dump work from outside: a frozen game cannot answer ready()
        else:
            if action == 'status' and (self.frozen or self._frozen_on_record()):
                self._process()
                return dispatch(action, {'status': self.status})
            self.live.ready()
            self._process()
        table = {
            'break': lambda: self.break_(args.get('op'), args.get('location'), args.get('break_id'), args.get('when'),
                                         args.get('hits'), args.get('thread') or 'main', args.get('do') or 'stop',
                                         args.get('log')),
            'status': self.status,
            'stack': lambda: self.stack(args.get('depth') or 24, bool(args.get('with_locals'))),
            'locals': lambda: self.locals_(args.get('frame') or 0),
            'eval': lambda: self.eval_(args.get('expression'), args.get('frame') or 0),
            'set': lambda: self.set_(args.get('expression'), args.get('value'), args.get('frame') or 0),
            'continue': self.continue_,
            'step': lambda: self.step(args.get('mode') or 'line'),
            'until': lambda: self.until(args.get('location')),
            'return': lambda: self.return_(args.get('value')),
            'skip': lambda: self.skip(args.get('location')),
            'freeze': self.freeze,
            'thaw': self.thaw,
            'threads': self.threads,
            'dump': lambda: self.dump(args.get('file')),
        }
        return dispatch(action, table)

    def _process(self):
        found = self.live.game.process()
        if not found:
            raise ConvError('NOT_RUNNING', 'Debugging needs the game running.')
        start = found[1] if len(found) > 1 else None
        if found[0] != self.pid or (self.start is not None and start != self.start):
            self.pid, self.start = found[0], start
            self._restore()     # a new game process starts clean; the same one gets its breaks and frozen threads back
        elif self.start is None:
            self.start = start
        return found[0]

    def _frozen_on_record(self):
        found = self.live.game.process()
        if not found:
            return False
        return bool(self.state.load('dbg_frozen', found[0], found[1] if len(found) > 1 else None))

    # ================================================================ plumbing
    def _engine(self):
        return self.live.engine()

    def _symdb(self, required=False):
        db = self._engine().symdb
        if db is None and required:
            raise ConvError('NO_SYMBOLS', 'This needs the symbol database, and there is none next to the engine map. '
                            'Next: build it with python -m engine_map.symdb --decomp <your checkout>.')
        return db

    def read(self, address, length):
        """Bytes of game memory through the companion, or None when unreadable (the hang doctor's reader shape)."""
        result = self.link.one('rd', word(address), word(length))
        return bytes.fromhex(result['hex']) if result.get('ok') else None

    def _read_or_raise(self, address, length, what):
        data = self.read(address, length)
        if data is None:
            raise ConvError('MEMORY_UNREADABLE', 'Cannot read %d bytes at 0x%08X (%s). Next: call fnv_status.'
                            % (length, address, what))
        return data

    def _u32_at(self, address):
        data = self.read(address, 4)
        return struct.unpack('<I', data)[0] if data else None

    def _write(self, address, data):
        result = self.link.one('wr', word(address), bytes(data).hex())
        if not result.get('ok'):
            raise ConvError('MEMORY_UNWRITABLE', 'Cannot write %d bytes at 0x%08X (%s).' % (len(data), address,
                                                                                         result.get('error')))

    def _stops(self):
        result = self.link.one('stops')
        if 'stops' not in result:
            raise ConvError('COMPANION_REFUSED', 'The engine companion did not list its stops (%s). Next: call '
                            'fnv_status; if the companion is old, call fnv_companion_install.'
                            % (result.get('error') or 'no reason given'), result)
        stops = sorted(result['stops'], key=lambda s: s['seq'])
        reached = {_addr(s['eip']) for s in stops if s.get('kind') == 'step' and s.get('eip')} & self.pending_to
        if reached:     # the companion consumed these `to` sites when the thread got there (S6 review A I1)
            self.pending_to -= reached
            self._persist()
        for stop in stops:
            if stop.get('seq') not in self.stop_breaks and stop.get('kind') in ('hook', 'bp'):
                hit = self._break_of(stop)
                if hit:
                    self.stop_breaks[stop['seq']] = hit['number']
        return stops

    def _break_of(self, stop):
        return next((b for b in self.breaks.values() if b.get('id') == stop.get('id')
                     and b['mechanism'] in ('hook', 'hw', 'int3')), None)

    def _current(self):
        """The latest stop; the game must be stopped by the debugger."""
        stops = self._stops()
        if not stops:
            raise ConvError('NOT_STOPPED', 'The game is not stopped by the debugger. Next: set a break (fnv_debug '
                            'break) and wait for it, or call fnv_debug status.')
        return stops[-1]

    @staticmethod
    def _same_thread(a, b):
        if a is None or b is None:
            return True
        try:
            return _num(a) == _num(b)
        except ValueError:
            return str(a) == str(b)

    def _await(self, after_seq, thread=None, pending=None):
        """Poll the stops until one of this thread with a sequence number above after_seq appears (a single step, or
        a `to`). A stop of another thread (a break with thread=any, a script hold, a fault) is not the result of the
        step: it stays listed for status. pending names a temporary `to` site that is still armed on a timeout."""
        deadline = self.clock() + STOP_WAIT
        while True:
            fresh = [s for s in self._stops() if s['seq'] > after_seq and self._same_thread(s.get('thread'), thread)]
            if fresh:
                return max(fresh, key=lambda s: s['seq'])
            if self.clock() >= deadline:
                extra = ''
                if pending:
                    self.pending_to.add(pending)
                    self._persist()
                    extra = (' The temporary stop site at %s stays armed and parks the thread when it is reached; '
                             'fnv_debug break op=clear (no break_id) removes it.' % _hx(pending))
                raise ConvError('STOP_TIMEOUT', 'The thread did not stop again within %d s.%s Next: call fnv_debug '
                                'status.' % (STOP_WAIT, extra), {'pendingTo': _hx(pending) if pending else None})
            self.sleep(0.05)

    def _resume(self, stop, *args, wait=False):
        """resume <seq> <mode...>. With wait, return the stop that comes next."""
        result = self.link.one('resume', word(stop['seq']), *[str(a) for a in args])
        if not result.get('ok'):
            code = {'bad_mode': 'BAD_MODE', 'no_stop': 'NOT_STOPPED'}.get(result.get('error'), 'COMPANION_REFUSED')
            raise ConvError(code, 'The engine companion refused resume %s (%s).' % (' '.join(str(a) for a in args),
                                                                                     result.get('error')), result)
        if not wait:
            return None
        pending = int(str(args[1]), 16) if args and args[0] == 'to' and len(args) > 1 else None
        return self._await(stop['seq'], stop.get('thread'), pending)

    # ================================================================ engine helpers
    def _resolve(self, text):
        """(address, function start or None, where) for a function name, an address or file:line."""
        text = (text or '').strip()
        if not text:
            raise ConvError('BAD_LOCATION', 'A location is needed: a function name, an address, or file:line.')
        engine = self._engine()
        m = re.fullmatch(r'(.+?):(\d+)', text)
        if m:
            db = self._symdb(required=True)
            file, line = m.group(1).strip(), int(m.group(2))
            hits = db.addresses_of(file, line)
            if not hits:
                raise ConvError('NO_CODE', 'No code for %s:%d in the symbol database. Next: check the path (a suffix '
                                'such as playercharacter.cpp works) and that the line has code.' % (file, line))
            address, function = hits[0]
            return address, function, '%s:%d' % (file, line)
        if text.lower().startswith('0x') or re.fullmatch(r'[0-9A-Fa-f]{6,8}', text):
            address = _addr(text)
            hit = engine.containing(address)
            if not hit:
                raise ConvError('NOT_FOUND', 'No function of the engine map contains 0x%08X.' % address)
            return address, hit[0], None
        try:
            function = engine.resolve(text)
        except fnv_engine.EngineMapError as e:
            raise ConvError(e.code, str(e), e.evidence)
        return function, function, None

    def _function_name(self, address):
        if address is None:
            return None
        try:
            return self._engine().summary(address, with_location=False).get('name')
        except KeyError:
            return None

    def _modules(self, address):
        """The game's modules, only for an address outside the exe image (None when unknown)."""
        if address is None or 0x00400000 <= address < 0x01400000:
            return None
        try:
            found = self.live.game.process()
            pid = found[0] if isinstance(found, (tuple, list)) else getattr(found, 'pid', found)
            return fnv_procmem.game_modules(int(pid)) if pid else None
        except Exception:  # noqa: BLE001 - module names are extra
            return None

    def _symbol(self, address, return_address=False):
        """engine.symbolize as a small dict: function, offset, source, sourceLine (empty when unknown)."""
        sym = self._engine().symbolize(address, self._modules(address), return_address=return_address)
        if sym.get('kind') == 'module':
            # Code in a plugin DLL (xNVSE, JIP, ...): named by module and offset.
            return {'function': '%s+%s' % (sym['module'], sym['offset']), 'module': sym['module'],
                    'address': _hx(address), 'offset': sym['offset']}
        if sym.get('kind') != 'function':
            return {'function': None, 'address': _hx(address)}
        out = {'function': sym.get('name'), 'address': _hx(address), 'offset': sym.get('offset')}
        for key in ('source', 'sourceLine'):
            if key in sym:
                out[key] = sym[key]
        return out

    def _position(self, address):
        """{'function', 'file', 'line'} of an address, or None outside the symbol database."""
        db = self._symdb()
        if db is None:
            return None
        fn = db.function_containing(address)
        if not fn:
            return None
        ln = db.line_at(address)
        return {'function': fn['addr'], 'file': ln['file'] if ln else None, 'line': ln['line'] if ln else None}

    def _source_text(self, file, line):
        """One decomp source line, or None."""
        try:
            lines = self._engine()._lines(file)
            return lines[int(line) - 1]
        except (fnv_engine.EngineMapError, OSError, IndexError, ValueError):
            return None

    def _window(self, source):
        if not source or ':' not in source:
            return []
        file, line = source.rsplit(':', 1)
        try:
            lines = self._engine()._lines(file)
        except (fnv_engine.EngineMapError, OSError):
            return []
        line = int(line)
        return [{'line': i, 'text': lines[i - 1].rstrip()[:200], 'current': i == line}
                for i in range(max(1, line - WINDOW), min(len(lines), line + WINDOW) + 1)]

    def _nargs(self, function):
        db = self._symdb()
        if db is None:
            return HOOK_MAX_ARGS
        words = 0
        for local in db.locals_of(function):
            if local['kind'] == 'bprel' and local['param'] and (local['offset'] or 0) >= 8:
                words = max(words, (local['offset'] - 8 + max(4, ((local['size'] or 4) + 3) // 4 * 4)) // 4)
        return max(1, min(HOOK_MAX_ARGS, words))

    # ================================================================ breaks
    def claim_pred_slot(self):
        """A free predicate slot of the breaks' pool (PRED_SLOT_FIRST..LAST): not used by a break of this debugger, not
        claimed by another tool, not in use in the companion (a break of an earlier server process). Call with self.lock
        held, store the program, and release_pred_slot when the condition is no longer needed."""
        used = {b.get('predSlot') for b in self.breaks.values()} | self._claimed_slots
        try:
            used |= {int(p['id']) for p in self.link.one('pred', 'list').get('preds', [])}
        except (LinkError, KeyError, TypeError, ValueError):
            pass
        slot = next((n for n in range(PRED_SLOT_FIRST, MAIN_SLOT) if n not in used), None)
        if slot is None:
            raise ConvError('TOO_MANY_CONDITIONS', 'Every predicate slot for breaks is in use. Next: clear a break that '
                            'has a when condition (fnv_debug break op=clear).')
        self._claimed_slots.add(slot)
        return slot

    def release_pred_slot(self, slot):
        """Give a slot back and clear it in the companion (best effort)."""
        with self.lock:
            self._claimed_slots.discard(slot)
            if slot is not None:
                try:
                    self.link.one('pred', 'clear', word(slot))
                except LinkError:
                    pass

    def _store_pred(self, text):
        """Compile text and store it in a new slot; returns the slot."""
        program = compile_predicate(text)['hex']
        slot = self.claim_pred_slot()
        stored = self.link.one('pred', 'set', word(slot), program)
        if not stored.get('ok'):
            self._claimed_slots.discard(slot)
            raise ConvError('PREDICATE_REFUSED', 'The companion refused the condition (%s).' % stored.get('error'),
                            stored)
        return slot

    def _main_slot(self):
        """The shared predicate slot that holds `main`; returns its word."""
        stored = self.link.one('pred', 'set', word(MAIN_SLOT), compile_predicate('main')['hex'])
        if not stored.get('ok'):
            raise ConvError('PREDICATE_REFUSED', 'The companion refused the main-thread condition (%s).'
                            % stored.get('error'), stored)
        return word(MAIN_SLOT)

    def break_(self, op, location, break_id, when, hits, thread, do, log):
        if op is None:
            op = 'set' if location else 'list'
        if op == 'list':
            return self.break_list()
        if op == 'clear':
            return self.break_clear(break_id)
        if op != 'set':
            raise ConvError('BAD_ARGUMENTS', 'break op must be set, list or clear, not %r.' % op)
        if thread not in ('main', 'any'):
            raise ConvError('BAD_ARGUMENTS', 'thread must be main or any.')
        if do not in ('stop', 'log'):
            raise ConvError('BAD_ARGUMENTS', 'do must be stop or log.')
        if (location or '').strip().lower() == 'fault':
            return self.break_fault()
        if when:
            compile_predicate(when)       # a bad condition is refused before anything is allocated
        log_hex = None
        if do == 'log':
            if not log or len(log) != 1:
                raise ConvError('BAD_ARGUMENTS', 'do=log needs exactly one expression in log for now. Next: pass '
                                'log=["expression"]; several expressions need the multi-value program.')
            log_hex = compile_predicate(log[0])['hex']
        address, function, source = self._resolve(location)
        with self.lock:
            return self._break_set(location, when, hits, thread, do, log, log_hex, address, function, source)

    def _break_set(self, location, when, hits, thread, do, log, log_hex, address, function, source):
        name = self._function_name(function)
        number = self.next_number
        self.next_number += 1
        record = {'number': number, 'location': location, 'address': _hx(address), 'function': name,
                  'source': source, 'when': when or None, 'do': do, 'log': log[0] if log else None,
                  'thread': thread, 'predSlot': None}
        if address == function and do == 'stop' and not hits:
            # A hook hold has no thread filter of its own: a thread=main break adds `main and` to its condition.
            hold_slot, hold_pred = None, 'ff'
            try:
                if thread == 'main' and not when:
                    hold_pred = self._main_slot()          # one shared slot holds `main`
                elif thread == 'main':
                    hold_slot = self._store_pred('main and (%s)' % when)
                    hold_pred = word(hold_slot)
                elif when:
                    hold_slot = self._store_pred(when)
                    hold_pred = word(hold_slot)
                pop = self._pop_bytes_of(function)
                hook = self._hook_hold(address, hold_pred,
                                       'dbg %d %s' % (number, name or _hx(address)), self._nargs(function), pop or 0)
                record.update(mechanism='hook', id=hook['id'], predSlot=hold_slot, popKnown=pop is not None)
                self.breaks[number] = record
                self._persist()
                return self._break_result(record, 'A hook hold: the thread parks when the function is called.')
            except (HookError, ConvError) as e:
                if hold_slot is not None:
                    self.release_pred_slot(hold_slot)
                record['hookRefused'] = str(e)
        pred, pred_slot = 'ff', None
        if when:
            pred_slot = self._store_pred(when)
            pred = word(pred_slot)
            record['predSlot'] = pred_slot
        mechanism = 'hw' if thread == 'main' and self._hw_in_use() < HW_SLOTS else 'int3'
        flags = (1 if do == 'log' else 0) | (2 if thread == 'any' else 0)
        try:
            bp_id = self._free_bp_id()
            if hits and hits > 1:
                # The companion counts the hits that pass the condition and acts from the Nth one on. Sent before the
                # set, so the count is armed with the breakpoint (set first, the next frame's hit would stop at once).
                after = self.link.one('bp', 'after', word(bp_id), word(int(hits)))
                if not after.get('ok'):
                    raise ConvError('BREAK_REFUSED', 'The companion refused the hit count (%s). Next: the companion may '
                                    'be older than bp after: reinstall it (fnv_companion_install).' % after.get('error'),
                                    after)
                record['stopAt'] = int(hits)     # stop on this passing hit and every later one
            try:
                try:
                    result = self._set_bp(bp_id, address, mechanism, pred, flags, log_hex)
                except ConvError:
                    if mechanism != 'hw':
                        raise
                    mechanism = 'int3'
                    result = self._set_bp(bp_id, address, mechanism, pred, flags, log_hex)
            except ConvError:
                if hits and hits > 1:
                    self.link.one('bp', 'after', word(bp_id), '0')    # drop the pending count
                raise
        except ConvError:
            if pred_slot is not None:
                self.release_pred_slot(pred_slot)
            raise
        record.update(mechanism=result.get('kind', mechanism), id=bp_id)
        self.breaks[number] = record
        self._persist()
        return self._break_result(record, 'A trap breakpoint (%s).' % record['mechanism'])

    def _set_bp(self, bp_id, address, mechanism, pred, flags, log_hex):
        op = ['bp', 'set', word(bp_id), word(address), mechanism, pred, word(flags)]
        if log_hex:
            op.append(log_hex)
        result = self.link.one(*op)
        if not result.get('ok'):
            raise ConvError('BREAK_REFUSED', 'The companion refused the breakpoint at 0x%08X (%s).'
                            % (address, result.get('error')), result)
        return result

    def _free_bp_id(self):
        used = {b['id'] for b in self.breaks.values() if b['mechanism'] in ('hw', 'int3')}
        used |= {b['id'] for b in self.link.one('bp', 'list').get('bps', [])}
        # a cleared break's stop can stay parked with its id; a new arm with that id would take it for its own (K4)
        used |= {int(s['id']) for s in self._stops() if s.get('kind') == 'bp' and s.get('id') is not None}
        for i in range(BP_IDS):
            if i not in used:
                return i
        raise ConvError('TOO_MANY_BREAKS', 'All %d trap breakpoint ids are in use. Next: clear some with fnv_debug '
                        'break clear.' % BP_IDS)

    def _hw_in_use(self):
        return sum(1 for b in self.breaks.values() if b['mechanism'] == 'hw')

    def _hook_hold(self, address, pred, label, nargs, pop=0):
        for h in self.link.one('hooks').get('hooks', []):
            if int(str(h.get('target')), 16) == address:
                raise HookError('ALREADY_HOOKED', 'The function already carries a hook (%s).' % h.get('label'),
                                {'hook': h.get('label')})
        raw = self.read(address, 24)
        if raw is None:
            raise HookError('UNREADABLE', 'Cannot read the function at 0x%08X' % address)
        stolen, relocs = decode_prologue(raw)
        result = self.link.one('hook', word(address), word(stolen), ','.join('%x' % r for r in relocs) or '-',
                               word(0), word(0), word(0), '0', word(nargs), '0', '0', '0', '0', hexs(label))
        if not result.get('ok'):
            raise HookError('HOOK_REFUSED', 'The companion refused the hook (%s).' % result.get('error'), result)
        hook = result['hook']
        # The 5th argument is the bytes a skip (return from the stop) pops: a callee-cleans function would otherwise
        # return with ESP off by its arguments.
        held = self.link.one('hookact', word(hook['id']), 'hold', pred, *([word(pop)] if pop else []))
        if not held.get('ok'):
            self.link.one('unhook', word(hook['id']))
            raise HookError('HOOK_REFUSED', 'The companion refused the hold (%s).' % held.get('error'), held)
        return hook

    def break_fault(self):
        result = self.link.one('faulthold', 'on')
        if not result.get('ok'):
            raise ConvError('COMPANION_REFUSED', 'The companion refused the crash hold (%s).' % result.get('error'),
                            result)
        with self.lock:
            number = self.next_number
            self.next_number += 1
        self.breaks[number] = {'number': number, 'location': 'fault', 'address': None, 'function': None,
                               'source': None, 'when': None, 'do': 'stop', 'log': None, 'thread': 'any',
                               'mechanism': 'fault', 'id': None}
        self._persist()
        return verdict('Crash hold is on.', 'A real fault parks the faulting thread; status shows the line.',
                       **{'break': self.breaks[number]})

    def _break_result(self, record, how):
        where = record['function'] or record['address']
        return verdict('Break %d set at %s.' % (record['number'], record['source'] or record['address']),
                       how + ' Stops in %s when it is reached.' % where, **{'break': _public(record)})

    def break_list(self):
        companion = {b['id']: b for b in self.link.one('bp', 'list').get('bps', [])}
        hooks = {h['id']: h for h in self.link.one('hooks').get('hooks', [])}
        rows = []
        for number, b in sorted(self.breaks.items()):
            row = _public(b)
            if b['mechanism'] == 'hook':
                row['hits'] = (hooks.get(b['id']) or {}).get('calls')
            elif b['mechanism'] in ('hw', 'int3'):
                listed = companion.get(b['id']) or {}
                row['hits'] = listed.get('hits')
                if listed.get('after'):
                    row['passed'] = listed.get('passed')
            rows.append(row)
        known_bps = {b['id'] for b in self.breaks.values() if b['mechanism'] in ('hw', 'int3')}
        known_hooks = {b['id'] for b in self.breaks.values() if b['mechanism'] == 'hook'}
        orphans = [{'kind': 'bp', 'id': i} for i in sorted(companion) if i not in known_bps and i != TEMP_BP]
        orphans += [{'kind': 'hook', 'id': i, 'label': h.get('label')} for i, h in sorted(hooks.items())
                    if str(h.get('label') or '').startswith('dbg ') and h.get('stolen', 1) and i not in known_hooks]
        note = 'Breaks are armed in the game until cleared or the game exits.'
        if orphans:
            note += (' %d armed in the game but not listed here (set by an earlier server process): fnv_debug break '
                     'op=clear with no break_id removes them.' % len(orphans))
        return verdict('%d breaks' % len(rows), note, total=len(rows), rows=rows, orphans=orphans)

    def break_clear(self, number):
        with self.lock:
            return self._break_clear(number)

    def _break_clear(self, number):
        targets = sorted(self.breaks) if number is None else [int(number)]
        cleared = []
        for n in targets:
            b = self.breaks.get(n)
            if b is None:
                raise ConvError('NO_BREAK', 'No break %s. Next: call fnv_debug break list.' % n)
            if b['mechanism'] == 'hook':
                self.link.one('unhook', word(b['id']))
            elif b['mechanism'] in ('hw', 'int3'):
                self.link.one('bp', 'clear', word(b['id']))
            elif b['mechanism'] == 'fault':
                self.link.one('faulthold', 'off')
            if b.get('predSlot') is not None:
                self.release_pred_slot(b['predSlot'])
            cleared.append(n)
            del self.breaks[n]
        swept = None
        if number is None:
            # Clear all also sweeps what an earlier server process left armed (nothing here knows those): every trap
            # breakpoint and temporary stop site (bp clear ff), the crash hold, and the `dbg N` hook holds.
            result = self.link.one('bp', 'clear', 'ff')
            swept = {'bps': result.get('cleared'), 'toSites': result.get('toSites'), 'hooks': []}
            self.pending_to.clear()
            self.link.one('faulthold', 'off')
            for h in self.link.one('hooks').get('hooks', []):
                if str(h.get('label') or '').startswith('dbg ') and h.get('stolen', 1):
                    self.link.one('unhook', word(h['id']))
                    swept['hooks'].append(h['id'])
            for item in self.link.one('pred', 'list').get('preds', []):
                slot = int(item['id'])
                if PRED_SLOT_FIRST <= slot <= PRED_SLOT_LAST and slot not in self._claimed_slots:
                    self.link.one('pred', 'clear', word(slot))
        self._persist()
        return verdict('Cleared %d break(s).' % len(cleared), None, cleared=cleared, swept=swept)

    # ================================================================ frames and stops
    def _why(self, stop):
        kind = stop.get('kind')
        if kind in ('hook', 'bp'):
            number = self.stop_breaks.get(stop.get('seq'))
            if number is None:
                hit = self._break_of(stop)
                number = hit['number'] if hit else None
                if number is not None:
                    self.stop_breaks[stop.get('seq')] = number
            if number is None:
                name = 'a cleared break'
            elif number in self.breaks:
                name = 'break %d' % number
            else:                  # cleared while this stop stayed parked (its bp id may serve a newer break now)
                name = 'break %d (cleared)' % number
            return name + (' (hook on the function entry)' if kind == 'hook' else ' (trap)')
        if kind == 'step':
            return 'single step'
        if kind == 'fault':
            return 'fault (crash hold)'
        return str(kind)

    def _top(self, stop):
        """Frame 0 of a stop: registers, the slots their values are saved in, and the frame pointer for locals."""
        eip = _addr(stop['eip'])
        if stop['kind'] == 'hook':
            block = _addr(stop['regs'])
            raw = self._read_or_raise(block, HOOK_BLOCK_READ, 'the hook register block')
            regs = {name: _u32(raw, off) for name, off in HOOK_BLOCK.items()}
            regs['eip'] = eip
            return {'regs': regs, 'slots': {n: block + o for n, o in HOOK_BLOCK.items()}, 'address': eip,
                    'ebp': None, 'paramBase': None, 'ready': False,
                    'callerRet': _u32(raw, HOOK_RET), 'callerEbp': regs['ebp'],
                    'args': [_u32(raw, HOOK_ARGS + 4 * i) for i in range(HOOK_MAX_ARGS)]}
        context = _addr(stop['context'])
        regs = fnv_procmem.parse_context(self._read_or_raise(context, fnv_procmem.WOW64_CONTEXT_SIZE, 'the CONTEXT'))
        slots = {n: context + fnv_procmem.CONTEXT_OFFSETS[n] for n in REG_SAVE}
        ready = self._frame_ready(eip)
        ebp = regs['ebp'] if ready else None
        return {'regs': regs, 'slots': slots, 'address': eip, 'ebp': ebp, 'paramBase': ebp, 'ready': ready,
                'callerRet': None, 'callerEbp': None, 'esp': regs['esp']}

    def _frame_ready(self, eip):
        """False at a function's first instructions, before its frame is set up (the symbol database's debug_start)."""
        db = self._symdb()
        fn = db.function_containing(eip) if db else None
        if not fn or fn.get('debug_start') is None:
            return True
        return eip - fn['addr'] >= fn['debug_start']

    def _prologue_caller(self, top):
        """(return address, caller's ebp) of a frame that is not set up yet (the stop is inside the prologue), or
        (None, None) when the prologue is not the standard `push ebp; mov ebp,esp`. Only offset 0 has the return
        address at [esp]; after the push it is at [esp+4], and from the mov on the frame pointer is valid."""
        regs = top['regs']
        db = self._symdb()
        fn = db.function_containing(top['address']) if db else None
        if fn is None:
            return self._u32_at(regs['esp']), regs['ebp']
        offset = top['address'] - fn['addr']
        if offset == 0:
            return self._u32_at(regs['esp']), regs['ebp']
        code = self.read(fn['addr'], 3)
        if code != b'\x55\x8b\xec':
            return None, None
        if offset < 3:      # after push ebp, before mov ebp,esp
            return self._u32_at(regs['esp'] + 4), self._u32_at(regs['esp'])
        return self._u32_at(regs['ebp'] + 4), self._u32_at(regs['ebp'])

    def frames(self, stop, depth=24):
        top = self._top(stop)
        out = [{'index': 0, 'address': top['address'], 'how': 'eip', 'ebp': top['ebp'],
                'paramBase': top['paramBase'], 'ready': top['ready'], 'regs': top['regs'], 'slots': top['slots'],
                'args': top.get('args')}]
        if stop['kind'] == 'hook':
            ret, ebp = top['callerRet'], top['callerEbp']
        elif top['ready']:
            ebp = self._u32_at(top['ebp'])
            ret = self._u32_at(top['ebp'] + 4)
        else:
            ret, ebp = self._prologue_caller(top)
        lo, hi = fnv_engine.EXE_RANGE
        while ret is not None and lo <= ret < hi and len(out) < depth:
            out.append({'index': len(out), 'address': ret, 'how': 'return', 'ebp': ebp, 'paramBase': ebp,
                        'ready': True, 'regs': {'ebp': ebp}, 'slots': {}})
            if not ebp:
                break
            nxt_ebp, nxt_ret = self._u32_at(ebp), self._u32_at(ebp + 4)
            if nxt_ebp is None or nxt_ebp <= ebp:
                break
            ret, ebp = nxt_ret, nxt_ebp
        return out

    def _frame_of(self, stop, index):
        frames = self.frames(stop, index + 1)
        if len(frames) <= index:
            raise ConvError('NO_FRAME', 'The stack has %d frames; frame %d does not exist. Next: call fnv_debug stack.'
                            % (len(frames), index))
        return frames[index]

    def _symbol_row(self, frame):
        sym = self._symbol(frame['address'], return_address=frame['how'] == 'return')
        row = {'index': frame['index'], 'function': sym.get('function'), 'address': sym['address'],
               'how': frame['how']}
        if sym.get('source'):
            row['source'] = sym['source']
        return row

    def status(self):
        if self.frozen:
            return verdict('Frozen: the game threads are suspended.', 'Call fnv_debug thaw before other tools, or '
                           'they will time out.', frozen=self.frozen, state='frozen')
        stops = self._stops()
        if not stops:
            pending = sorted(_hx(a) for a in self.pending_to)
            return verdict('Running: the debugger has no stop in the game.', '%d breaks armed.%s' % (
                len(self.breaks), ' Temporary stop sites still armed: %s (break op=clear with no id removes them).'
                % ', '.join(pending) if pending else ''),
                           state='running', breaks=[_public(b) for _, b in sorted(self.breaks.items())],
                           pendingTo=pending or None)
        stop = stops[-1]
        top = self._top(stop)
        sym = self._symbol(top['address'])
        view = {'seq': stop['seq'], 'kind': stop['kind'], 'why': self._why(stop), 'thread': stop.get('thread'),
                'main': stop.get('main'), 'function': sym.get('function'), 'address': sym['address'],
                'source': sym.get('source'), 'lines': self._window(sym.get('source'))}
        if stop['kind'] == 'fault':
            view['exception'] = _hx(self._u32_at(_addr(stop['exception'])) or 0)
        where = view['source'] or sym['address']
        reason = 'The game is parked by the debugger; use stack, locals or eval, then continue or step.'
        if not stop.get('main', True):
            reason += ' Thread %s is a worker thread: engine calls are frozen until thaw.' % stop.get('thread')
        return verdict('Stopped in %s at %s: %s.' % (view['function'] or 'unknown code', where, view['why']), reason,
                       state='stopped', stop=view, stops=len(stops))

    def stack(self, depth, with_locals):
        stop = self._current()
        frames = self.frames(stop, depth)
        rows = []
        for frame in frames:
            row = self._symbol_row(frame)
            if with_locals:
                row['locals'] = self.frame_locals(frame)['locals']
            rows.append(row)
        return verdict('%d frames' % len(rows), 'Frame 0 is where the thread stopped; the others are callers at their '
                       'call sites.', frames=rows)

    def locals_(self, index):
        stop = self._current()
        frame = self._frame_of(stop, index)
        result = self.frame_locals(frame)
        return verdict('%d locals in %s' % (len(result['locals']), result.get('function') or 'an unknown function'),
                       result.get('note') or 'Values are read now from the game; a note says why one is missing.',
                       frame=index, locals=result['locals'])

    def frame_locals(self, frame):
        db = self._symdb()
        if db is None:
            return {'function': None, 'locals': [], 'note': 'No symbol database: no locals.'}
        lookup = frame['address'] - 1 if frame['how'] == 'return' else frame['address']
        fn = db.function_containing(lookup)
        if not fn or not fn.get('has_debug'):
            return {'function': None, 'locals': [], 'note': 'No debug information for this function.'}
        offset = lookup - fn['addr']
        rows = [self._show(local, frame) for local in db.locals_of(fn['addr'], offset)]
        return {'function': fn['name'], 'locals': rows, 'note': None}

    def _show(self, local, frame):
        out = {'name': local['name'], 'location': local['location'], 'type': local['type'], 'param': local['param']}
        try:
            v = self._local_v(local, frame)
        except ConvError as e:
            out['note'] = str(e)
            return out
        if v['value'] is None:
            out['note'] = ('unreadable at 0x%08X' % v['address']) if v.get('error') else \
                'not decoded: size %s (a struct or an array)' % v['size']
            return out
        if isinstance(v['value'], int) and _is_pointer(v['type']):
            out['value'] = _hx(v['value'])
            cls = self._class_of(v['value']) if v['value'] else None
            if cls:
                out['class'] = cls
        else:
            out['value'] = v['value']
        return out

    def _class_of(self, pointer):
        vt = self._u32_at(pointer)
        info = self._engine().vtable_at(vt) if vt else None
        if info and info.get('kind') == 'vtable' and 'slot' not in info:
            return info.get('class')
        return None

    # ================================================================ values
    def _v_at(self, address, type_text, size):
        out = {'type': type_text, 'address': address, 'size': size, 'slot': None, 'reg': None, 'value': None}
        if size in (1, 2, 4, 8) or _scalar_size(type_text):
            size = size or _scalar_size(type_text)
            out['size'] = size
            data = self.read(address, size)
            if data is None:
                out['error'] = True
            else:
                out['value'] = decode(data, type_text, size)
        return out

    def _local_v(self, local, frame):
        kind, text = local['kind'], local['type']
        size = local.get('size') or _scalar_size(text)
        if kind == 'bprel' and local['param'] and frame.get('args') is not None and not frame['ready']:
            # a hook stop: parameters are the saved stack arguments (ebp+8 is argument 0)
            index = (local['offset'] - 8) // 4
            if 0 <= index < len(frame['args']) and size in (1, 2, 4, 8):
                raw = struct.pack('<I', frame['args'][index])[:size]
                return {'type': text, 'address': None, 'size': size, 'slot': None, 'reg': None,
                        'value': decode(raw, text, size), 'error': None}
        if kind == 'bprel':
            base = frame['paramBase'] if local['param'] else (frame['ebp'] if frame['ready'] else None)
            if base is None:
                raise ConvError('FRAME_NOT_SET_UP', 'The frame of %s is not set up at this address yet (before its '
                                'prologue): %s is not readable here.' % (frame.get('how'), local['name']))
            return self._v_at(base + local['offset'], text, size)
        if kind == 'register' or kind == 'regrel':
            name = REGISTERS.get(local['reg'])
            if name not in frame['regs'] or name not in frame['slots']:
                raise ConvError('REGISTER_GONE', '%s lives in %s, which is not kept for this frame. Next: use frame 0.'
                                % (local['name'], name))
            if kind == 'register':
                v = {'type': text, 'address': None, 'size': size, 'slot': frame['slots'][name], 'reg': name,
                     'value': None}
                if size in (1, 2, 4, 8):
                    v['value'] = decode(struct.pack('<I', frame['regs'][name])[:size], text, size)
                return v
            v = self._v_at(frame['regs'][name] + local['offset'], text, size)
            return v
        raise ConvError('NOT_AVAILABLE', '%s has no stored location the debugger can read (%s).'
                        % (local['name'], local['location']))

    def _find_member(self, db, cls, name, depth=0):
        layout = db.layout(cls) if cls else None
        if not layout:
            return None
        for m in layout.get('members') or []:
            if m['name'] == name:
                return m
        if depth < 4:
            for base in layout.get('bases') or []:
                hit = self._find_member(db, base['name'], name, depth + 1)
                if hit:
                    return dict(hit, offset=(hit['offset'] or 0) + (base.get('offset') or 0))
        return None

    def _member(self, value, name):
        db = self._symdb(required=True)
        if _is_pointer(value['type']):
            if not value['value']:
                raise ConvError('NULL_POINTER', 'Cannot read %s of a null pointer.' % name)
            base, cls = value['value'], _class_name(value['type'])
        else:
            base, cls = value['address'], _class_name(value['type'])
        if base is None:
            raise ConvError('NOT_ADDRESSABLE', 'The value before .%s has no address.' % name)
        member = self._find_member(db, cls, name)
        if member is None or member.get('offset') is None:
            raise ConvError('NO_MEMBER', 'No member %s in %s. Next: call fnv_symbols type %s.' % (name, cls, cls))
        return self._v_at(base + member['offset'], member['type'], member['size'])

    def _index(self, value, index):
        text = value['type'] or ''
        m = re.search(r'\[(\d+)\]$', text)
        if m and value['address'] is not None:
            elem = text[:m.start()].strip()
            size = _scalar_size(elem) or (value['size'] or 0) // int(m.group(1))
            return self._v_at(value['address'] + index * size, elem, size)
        if _is_pointer(text) and value['value']:
            elem = text[:-1].strip()
            size = _scalar_size(elem)
            if size is None:
                db = self._symdb(required=True)
                layout = db.layout(_class_name(elem)) if _class_name(elem) else None
                size = layout['size'] if layout else None
            if size is None:
                raise ConvError('NOT_INDEXABLE', 'Cannot index %s: the element size is unknown.' % text)
            return self._v_at(value['value'] + index * size, elem, size)
        raise ConvError('NOT_INDEXABLE', 'Cannot index a value of type %r. Next: index arrays and pointers only.' % text)

    def _eval_tokens(self, tokens, frame):
        pos = [0]

        def peek():
            return tokens[pos[0]] if pos[0] < len(tokens) else None

        def take():
            token = peek()
            pos[0] += 1
            return token

        def number_token(token):
            return float(token) if '.' in token else int(token, 0)

        def primary():
            token = take()
            if token is None:
                raise ConvError('BAD_EXPRESSION', 'The expression ends too early. Next: add the name or number.')
            if token == '[':
                address = number_token(take())
                if take() != ']' or take() != ':':
                    raise ConvError('BAD_EXPRESSION', 'Memory reads look like [0x011DEA0C]:u32.')
                type_name = take()
                if type_name not in MEMORY_TYPES:
                    raise ConvError('BAD_EXPRESSION', 'Unknown memory type %r (use u8 i8 u16 i16 u32 i32 f32).' % type_name)
                size = _scalar_size(MEMORY_TYPES[type_name])
                return self._v_at(address, MEMORY_TYPES[type_name], size)
            if token.startswith('$'):
                name = token[1:]
                if name not in frame['regs']:
                    raise ConvError('NO_REGISTER', 'No register %s in this frame. Next: use a register of frame 0 '
                                    '(eax ecx edx ebx esp ebp esi edi eip eflags).' % token)
                value = frame['regs'][name]
                return {'type': 'unsigned int', 'address': None, 'size': 4, 'value': value, 'reg': name,
                        'slot': frame['slots'].get(name), 'error': None}
            if token[0].isdigit() or (token[0] == '0'):
                return {'type': 'float' if isinstance(number_token(token), float) else 'int', 'address': None,
                        'size': 4, 'value': number_token(token), 'slot': None, 'reg': None}
            if re.match(r'[A-Za-z_]', token):
                return self._name_v(token, frame)
            raise ConvError('BAD_EXPRESSION', 'Unexpected %r. Next: use names, ->, ., [index].' % token)

        def postfix(value):
            while True:
                token = peek()
                if token == '->' or token == '.':
                    take()
                    name = take()
                    if not name or not re.match(r'[A-Za-z_]', name):
                        raise ConvError('BAD_EXPRESSION', 'A member name is needed after %s.' % token)
                    value = self._member(value, name)
                elif token == '[':
                    take()
                    index = number_token(take())
                    if take() != ']':
                        raise ConvError('BAD_EXPRESSION', 'Missing ] after an index.')
                    value = self._index(value, index)
                else:
                    return value

        result = postfix(primary())
        if peek() is not None:
            raise ConvError('BAD_EXPRESSION', 'Unexpected %r at the end. Next: remove it or split the expression.'
                            % peek())
        return result

    def _name_v(self, name, frame):
        db = self._symdb()
        if db is not None:
            lookup = frame['address'] - 1 if frame['how'] == 'return' else frame['address']
            fn = db.function_containing(lookup)
            if fn:
                offset = lookup - fn['addr']
                for local in db.locals_of(fn['addr'], offset):
                    if local['name'] == name:
                        return self._local_v(local, frame)
        raise ConvError('NO_SUCH_NAME', 'No local or parameter %r in frame %d. Next: call fnv_debug locals.'
                        % (name, frame['index']))

    def eval_(self, expression, index):
        if not expression:
            raise ConvError('BAD_EXPRESSION', 'An expression is needed, such as this->m_fHealth or $eax.')
        if '(' in expression or ')' in expression:
            raise ConvError('CALL_UNSUPPORTED', 'Casts and calls are not supported by eval yet. Next: read the field '
                            'with a path such as this->m_fHealth.')
        stop = self._current()
        frame = self._frame_of(stop, index)
        v = self._eval_tokens(_tokens(expression), frame)
        if v.get('error'):
            raise ConvError('MEMORY_UNREADABLE', 'Cannot read the value of %s (at 0x%08X).'
                            % (expression, v['address'] or 0))
        out = {'expression': expression, 'type': v['type'], 'address': _hx(v['address']) if v['address'] is not None
               else None, 'size': v['size'], 'register': v.get('reg')}
        value = v['value']
        if value is None:
            out['value'] = None
            return verdict('%s is not a scalar (size %s).' % (expression, v['size']),
                           'Use a member path to reach a scalar value.', **out)
        if isinstance(value, int) and _is_pointer(v['type']):
            cls = self._class_of(value) if value else None
            out['value'] = _hx(value)
            if cls:
                out['class'] = cls
        else:
            out['value'] = value
        return verdict('%s = %s' % (expression, out['value']), None, **out)

    def set_(self, expression, value_text, index):
        if not expression:
            raise ConvError('BAD_EXPRESSION', 'A target is needed, such as eax, a local or this->m_fHealth.')
        new = _parse_number(value_text)
        stop = self._current()
        frame = self._frame_of(stop, index)
        target = self._eval_tokens(_tokens(expression), frame)
        if target.get('slot') is not None:
            data = struct.pack('<I', int(new) & 0xFFFFFFFF)
            self._write(target['slot'], data)
            how = 'register %s in the stop' % target['reg']
        elif target['address'] is None:
            raise ConvError('NOT_WRITABLE', '%s has no address to write to. Next: write a local, a field or a '
                            'register.' % expression)
        elif frame['how'] == 'return' and target.get('reg'):
            raise ConvError('NOT_WRITABLE', 'Registers of caller frames are not kept. Next: use frame 0.')
        else:
            size = target['size']
            if not size or not (_scalar_size(target['type']) or size in (1, 2, 4, 8)):
                raise ConvError('NOT_WRITABLE', '%s is not a scalar.' % expression)
            if (target['type'] or '') == 'float' and size == 4:
                data = struct.pack('<f', float(new))
            elif (target['type'] or '') == 'double' and size == 8:
                data = struct.pack('<d', float(new))
            else:
                data = (int(new) & ((1 << (8 * size)) - 1)).to_bytes(size, 'little')
            self._write(target['address'], data)
            how = 'memory at %s' % _hx(target['address'])
        return verdict('Set %s to %s.' % (expression, new), 'Written to %s; it takes effect when the game resumes.'
                       % how, expression=expression, value=new, previous=target.get('value'))

    # ================================================================ running
    def continue_(self):
        stops = self._stops()
        if not stops:
            raise ConvError('NOT_STOPPED', 'The game is not stopped by the debugger. Next: call fnv_debug status.')
        self.link.one('resume', 'ff', 'run')
        return verdict('Resumed %d stop(s).' % len(stops), 'The game runs until the next break.', state='running')

    def step(self, mode):
        if mode not in STEP_MODES:
            raise ConvError('BAD_ARGUMENTS', 'mode must be one of %s.' % ', '.join(STEP_MODES))
        stop = self._current()
        self._unreached = set()
        if stop['kind'] == 'hook':
            raise ConvError('BAD_MODE', 'A hook stop cannot be stepped. Next: continue, or return <value> from the '
                            'function; to step the function, set a break on a line inside it.')
        if mode in ('instruction', 'into'):
            new = self._resume(stop, 'step', wait=True)
        elif mode == 'over':
            # Source-level over: to the next line, running calls without stopping in them. A stop on a call
            # instruction outside any line data still steps over just that call.
            position = self._position(_addr(stop['eip']))
            new = self._step_line(stop) if position and position.get('line') is not None else self._over(stop)
        elif mode == 'out':
            new = self._run_to(stop, self._return_address(stop), self._esp_of(stop) + 4)
        else:
            new = self._step_line(stop)
        left = sorted(self._unreached)
        reason = 'Mode %s.' % mode
        if left:
            reason += (' %s came first; the temporary stop at %s is still armed (continue runs on to it, break clear '
                       'removes it).' % (self._why(new), ', '.join(_hx(a) for a in left)))
        return verdict('Stopped at %s (%s).' % (_hx(_addr(new['eip'])), self._why(new)), reason,
                       stop=self.status_view(new), pendingTo=', '.join(_hx(a) for a in left) or None)

    def status_view(self, stop):
        sym = self._symbol(_addr(stop['eip']))
        return {'seq': stop['seq'], 'kind': stop['kind'], 'why': self._why(stop), 'function': sym.get('function'),
                'address': sym['address'], 'source': sym.get('source'), 'lines': self._window(sym.get('source'))}

    def _code(self, address):
        code = self.read(address, 8)
        if code is None:
            raise ConvError('MEMORY_UNREADABLE', 'Cannot read the instruction at 0x%08X.' % address)
        return code

    def _esp_of(self, stop):
        return self._top(stop)['regs']['esp']

    def _run_to(self, stop, address, min_esp=None):
        """Run to a temporary stop site. The site is only valid in the game's code (the companion writes an int3 at
        any address it is given). With min_esp, a stop at the site with a lower esp is a deeper recursion of the same
        call: it is passed on and the run continues to the real return."""
        lo, hi = fnv_engine.EXE_RANGE
        if not lo <= address < hi:
            raise ConvError('BAD_LOCATION', 'Cannot run to 0x%08X: it is outside the game\'s code, and a temporary stop '
                            'site written there would corrupt memory. Next: use a function or an address in the exe.'
                            % address)
        cur = stop
        for _ in range(STEP_LIMIT):
            new = self._resume(cur, 'to', word(address), wait=True)
            if _addr(new['eip']) == address and address in self.pending_to:
                self.pending_to.discard(address)
                self._persist()
            if _addr(new['eip']) != address:
                # Another stop of the thread came first: the companion keeps the `to` site armed for it, so it is
                # recorded (status lists it, a server restart keeps it) (R11).
                self.pending_to.add(address)
                getattr(self, '_unreached', set()).add(address)
                self._persist()
                return new
            if min_esp is None or self._esp_of(new) >= min_esp:
                return new
            cur = new
        raise ConvError('STEP_LIMIT', 'The return was not reached within %d recursive calls. Next: continue.' % STEP_LIMIT)

    def _over(self, stop):
        eip = _addr(stop['eip'])
        length = _call_length(self._code(eip))
        if length:
            after = self._run_to(stop, eip + length, self._esp_of(stop))
            return after
        return self._resume(stop, 'step', wait=True)

    def _return_address(self, stop):
        top = self._top(stop)
        if top['ready']:
            return self._read_u32(top['ebp'] + 4)
        ret, _ = self._prologue_caller(top)
        if ret is None:
            raise ConvError('FRAME_NOT_SET_UP', 'The stop is inside the prologue of a function whose frame setup is not '
                            'the standard one, so its return address is not known. Next: step instruction until the '
                            'frame is set up, or continue.')
        return ret

    def _read_u32(self, address):
        value = self._u32_at(address)
        if value is None:
            raise ConvError('MEMORY_UNREADABLE', 'Cannot read the return address at 0x%08X.' % address)
        return value

    def _step_line(self, stop):
        """Single-step until the source line changes; a call is run over with a temporary break after it."""
        start = self._position(_addr(stop['eip']))
        if start is None or start['line'] is None:
            raise ConvError('NO_LINES', 'This code has no source line in the symbol database. Next: use step '
                            'instruction.')
        cur = stop
        for _ in range(STEP_LIMIT):
            eip = _addr(cur['eip'])
            length = _call_length(self._code(eip))
            if length:
                cur = self._run_to(cur, eip + length, self._esp_of(cur))
                if _addr(cur['eip']) != eip + length:
                    return cur
            else:
                cur = self._resume(cur, 'step', wait=True)
            here = self._position(_addr(cur['eip']))
            if here is None or here['function'] != start['function'] or here['line'] is None:
                return cur
            if (here['file'], here['line']) != (start['file'], start['line']):
                return cur
        raise ConvError('STEP_LIMIT', 'The line did not change within %d steps. Next: use step out or until <line>.'
                        % STEP_LIMIT)

    def until(self, location):
        address, _, _ = self._resolve(location)
        stop = self._current()
        if stop['kind'] == 'hook':
            raise ConvError('BAD_MODE', 'until needs a break or step stop. Next: continue, or set a break at the '
                            'location first.')
        new = self._run_to(stop, address)
        here = _addr(new['eip'])
        if here != address:
            # Another stop came first (a break, a fault): the temporary site at the location stays armed in the game.
            self.pending_to.add(address)
            self._persist()
            return verdict('Stopped at %s before reaching %s.' % (_hx(here), location),
                           '%s came first; the temporary stop at %s is still armed (continue runs on to it, break '
                           'clear removes it).' % (new.get('why') or 'another stop', _hx(address)),
                           reached=False, stop=self.status_view(new))
        return verdict('Stopped at %s.' % _hx(here), 'Reached %s.' % location, reached=True,
                       stop=self.status_view(new))

    def return_(self, value_text):
        value = _parse_number(value_text if value_text is not None else '0')
        stop = self._current()
        eax = int(value) & 0xFFFFFFFF
        if stop['kind'] == 'hook':
            record = next((b for b in self.breaks.values() if b['mechanism'] == 'hook' and b.get('id') == stop.get('id')),
                          None)
            if record is None or not record.get('popKnown', True):
                # The hold's skip pops the bytes it was armed with; without the function's signature that is 0, which
                # corrupts the stack of a callee-cleans function.
                raise ConvError('POP_UNKNOWN', 'Cannot return from this hook stop: the bytes the function pops are not '
                                'known (it is not in the symbol database, or the hold was not set by fnv_debug break). '
                                'Next: continue, or set a break on a line inside the function.')
            self._resume(stop, 'skip', word(eax))
            return verdict('Returned %s from the hooked function without running it.' % value_text,
                           'The hook resumed with a skip.', state='running')
        eip = _addr(stop['eip'])
        db = self._symdb()
        fn = db.function_containing(eip) if db else None
        if fn:
            start = fn['addr']
        else:
            hit = self._engine().containing(eip)
            start = hit[0] if hit else None
        if start is None:
            raise ConvError('NOT_FOUND', 'No function of the engine map contains 0x%08X, so its start and epilogue are '
                            'unknown. Next: continue.' % eip)
        if eip != start and not fn:
            raise ConvError('NO_EPILOGUE', 'This function is not in the symbol database, so its epilogue is unknown and '
                            'a bare return from the middle of it would jump to garbage. Next: continue, or break at its '
                            'first instruction and return there.')
        if eip != start and not self._frame_ready(eip):
            raise ConvError('FRAME_NOT_SET_UP', 'The stop is inside the prologue, where the frame is half built: neither '
                            'a return nor the epilogue is valid. Next: step instruction until the prologue is done, or '
                            'set a break on its first instruction and return there.')
        if fn and eip != fn['addr']:
            # Inside the function its frame (and saved ebx/esi/edi) is built, so a bare `ret` would pop a local as the
            # return address. Run the function's own epilogue instead: eax = the value, continue at its last line
            # (the closing brace), which restores the saved registers and the frame and returns with the right pop.
            rows = db.lines_of(fn['addr'])
            if not rows:
                raise ConvError('NO_LINES', 'Cannot return from the middle of this function: it has no line data, so '
                                'its epilogue is unknown. Next: continue, or set a break on its first instruction.')
            last_offset, last_file, last_line = rows[-1]
            text = (self._source_text(last_file, last_line) or '').strip()
            if text != '}':
                raise ConvError('NO_EPILOGUE', 'The last line of this function is %r, not its closing brace, so its '
                                'epilogue is not known for sure. Next: continue, or break at its first instruction and '
                                'return there.' % text[:60])
            target = fn['addr'] + last_offset
            if eip >= target:
                self._write(self._top(stop)['slots']['eax'], struct.pack('<I', eax))
                self._resume(stop, 'run')
            else:
                self._write(self._top(stop)['slots']['eax'], struct.pack('<I', eax))
                self._resume(stop, 'eip', word(target))
            return verdict('Returned %s from the current function.' % value_text,
                           'eax was set and the function ran its own epilogue (%s:%d), so its saved registers and stack '
                           'frame were restored; the game runs on.' % (last_file, last_line), state='running',
                           epilogue=_hx(target))
        pop = self._pop_bytes_of(start)
        if pop is None:
            raise ConvError('POP_UNKNOWN', 'Cannot return: the bytes this function pops are not known (it is not in the '
                            'symbol database), and a wrong count corrupts the stack of the caller. Next: continue.')
        self._resume(stop, 'ret', word(eax), word(pop))
        return verdict('Returned %s from the current function.' % value_text,
                       'At its first instruction: the stack popped %d argument bytes; the game runs on.' % pop,
                       state='running')

    def _pop_bytes_of(self, function):
        """Argument bytes the function at this start address pops on return (0 for cdecl), or None when the symbol
        database does not know the function (the count is then unknown, not 0)."""
        db = self._symdb()
        fn = db.function(function) if db and function is not None else None
        if not fn:
            return None
        signature = (self._engine().functions.get(fn['addr']) or {}).get('signature') or ''
        if '__cdecl' in signature:
            return 0
        ends = [(local['offset'] or 0) + max(4, ((local['size'] or 4) + 3) // 4 * 4) for local in db.locals_of(fn['addr'])
                if local['kind'] == 'bprel' and local['param'] and (local['offset'] or 0) >= 8]
        return max(ends, default=8) - 8

    def _pop_bytes(self, stop):
        """Argument bytes the current function pops on return (0 for cdecl or when unknown)."""
        db = self._symdb()
        fn = db.function_containing(_addr(stop['eip'])) if db else None
        return (self._pop_bytes_of(fn['addr']) if fn else 0) or 0

    def skip(self, location):
        address, _, _ = self._resolve(location)
        stop = self._current()
        if stop['kind'] == 'hook':
            raise ConvError('BAD_MODE', 'skip moves within a function and a hook stop has no frame of its own. '
                            'Next: set a break on that line and skip from there.')
        here = self._position(_addr(stop['eip']))
        there = self._position(address)
        if here is None or there is None or here['function'] != there['function']:
            raise ConvError('NOT_SAME_FUNCTION', '%s is not in the function of the stop. Next: skip only within one '
                            'function.' % location)
        self._resume(stop, 'eip', word(address))
        return verdict('Moved execution to %s.' % _hx(address), 'It runs on from there; the stop is not reached again '
                       'until the next break.', state='running')

    # ================================================================ process control
    def _pipe_thread(self):
        """The companion's pipe thread id when the companion reports it in its ping (hello), else None."""
        hello = getattr(self.link, 'hello', None) or {}
        for key in ('pipeThread', 'pipeThreadId', 'pipeTid'):
            if hello.get(key):
                try:
                    return _num(hello[key])
                except ValueError:
                    return None
        return None

    def freeze(self):
        if self.frozen:
            return verdict('Already frozen.', 'Call fnv_debug thaw to resume the game.', frozen=self.frozen)
        keep = [t for t in (self._pipe_thread(),) if t]
        with fnv_procmem.GameProcess(self.pid) as proc:
            self.frozen = proc.suspend_all(keep=keep) if keep else proc.suspend_all()
        self._persist()
        return verdict('Froze %d threads.' % len(self.frozen),
                       'Engine calls are unavailable until fnv_debug thaw, and the dead-man switch cannot fire '
                       '(its thread is suspended). thaw and status work without the companion and the freeze is kept '
                       'in the state file, so a restarted server can still thaw it.',
                       frozen=self.frozen, state='frozen', pipeThreadKept=bool(keep))

    def thaw(self):
        was = list(self.frozen)
        with fnv_procmem.GameProcess(self.pid) as proc:
            if was:
                done = proc.resume_all(was)
            else:
                # Nothing on record (a freeze from before a restart with no state file, or another tool). Resuming
                # every thread would also release threads the game, a plugin or the companion holds suspended (its
                # code writes suspend the game's threads): only a game whose frames stand still is thawed, and only
                # the threads suspend_all freezes (not the companion's pipe thread) (R13).
                why = self._not_frozen(proc)
                if why:
                    return verdict('Nothing is frozen.', why, resumed=[], state=None, forced=False)
                keep = {t for t in (self._pipe_thread(),) if t}
                done = proc.resume_all([t for t in proc.threads() if t not in keep])
        self.frozen = []
        self._persist()
        return verdict('Thawed %d threads.' % len(done), 'The game runs again.' if was else
                       'Nothing was on record as frozen and the frames stood still: each thread suspend_all would '
                       'freeze was resumed once.', resumed=done, state='running', forced=not was)

    def _not_frozen(self, proc):
        """Why a thaw with nothing on record must resume nothing, or None when the game looks frozen by suspension
        (S6 review A M3: a debugger stop or a held frame gate also stops the frames)."""
        try:
            ping = self.link.ping(timeout=1.0) or {}
        except Exception:  # noqa: BLE001 - a frozen game cannot answer
            ping = {}
        if ping.get('parked'):
            return ('Nothing was on record as frozen: the game is parked by the debugger (%s stop(s)). Next: fnv_debug '
                    'continue.' % ping['parked'])
        if ping.get('held'):
            return ('Nothing was on record as frozen: the frame gate is held. Next: fnv_time release (or '
                    'resume for a paused clock).')
        stalled = self._frames_stalled(proc)
        if stalled is None:
            return ('Nothing was on record as frozen and the frame counter cannot be read, so I cannot tell whether the '
                    'game is frozen: no thread was resumed.')
        if not stalled:
            return "Nothing was on record as frozen and the game's frames advance, so no thread was resumed."
        return None

    def _frames_stalled(self, proc, window=0.5):
        """Does the engine's frame counter stand still over `window` seconds (read from outside the game)?"""
        from .fnv_time import FRAME_COUNTER
        first = proc.read(FRAME_COUNTER, 4)
        self.sleep(window)
        second = proc.read(FRAME_COUNTER, 4)
        if first is None or second is None:
            return None
        return first == second

    def threads(self):
        stops = {}
        try:
            for stop in self._stops():
                stops.setdefault(_num(stop.get('thread')), stop['seq'])
        except (ConvError, LinkError):
            pass       # a frozen game cannot answer: the stop column stays empty
        rows = []
        with fnv_procmem.GameProcess(self.pid) as proc:
            ids = proc.threads()
            main = proc.main_thread(ids)
            for tid in ids:
                regs = proc.thread_context(tid) or {}
                rows.append({'thread': tid, 'main': tid == main, 'eip': _hx(regs['eip']) if 'eip' in regs else None,
                             'stopSeq': stops.get(tid), 'frozen': tid in self.frozen})
        return verdict('%d threads' % len(rows), 'The main thread runs the companion; worker threads park on stops.',
                       total=len(rows), rows=rows)

    def dump(self, name):
        safe = re.sub(r'[^A-Za-z0-9_.-]', '_', (name or 'game').strip())[:80] or 'game'
        if not safe.lower().endswith('.dmp'):
            safe += '.dmp'
        folder = Path(self.live.project.state_dir) / 'dumps'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / safe
        was_frozen = bool(self.frozen)
        suspended = []
        with fnv_procmem.GameProcess(self.pid) as proc:
            if not was_frozen:
                suspended = proc.suspend_all()
            try:
                size = proc.minidump(str(path))
            finally:
                proc.resume_all(suspended)
        return verdict('Wrote a minidump (%d bytes).' % size, 'Frozen while it was written; the game ran on after.',
                       path=str(path), bytes=size)


def _public(record):
    keep = ('number', 'location', 'address', 'function', 'source', 'when', 'do', 'log', 'thread', 'mechanism', 'stopAt')
    return {k: record.get(k) for k in keep}
