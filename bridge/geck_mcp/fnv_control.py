"""Behaviour hooks, code patches, tracepoints and safe engine getters (fnv-mcp phase 2, engine companion).

  fnv_hook_set    change what an engine function does: skip it with a chosen result, replace its result, change
                  one argument, or run a script line whenever it is called (with a filter and a hit limit)
  fnv_patch       write bytes, NOPs, a ret or a jmp/call at an address in the game's code, journaled with the
                  original bytes and checked against the clean exe
  fnv_tracepoint  record calls of a function when a condition holds: arguments as objects, the caller and the
                  call stack (return addresses on the stack, kept when a call instruction precedes them)
  fnv_engine_get  call a proven read-only getter (Get*/Is*/Has*/Can*...) with typed arguments, outside full
                  control: its direct callees are checked for setters first

Every hook goes through the companion's watch-hook machinery (fnvmcp_engine.c): the stolen prologue is decoded
by the bridge, the companion moves it into a trampoline, and hookact adds the behaviour (ACT_* below).
"""
import re
import struct
import time
from collections import Counter

from .fnv_agent import TRACE_PREFIX, clean_type, decode_word, layout_args, param_kind, parse_signature
from .fnv_deep import HF_EXIT, HF_RECORD, HookError, decode_prologue
from .fnv_link import hexs, word
from .fnv_live import LiveError

HF_STACK = 8
ACT = {'off': 0, 'skip': 1, 'skip_float': 2, 'result': 3, 'arg': 4, 'script': 5}
ACT_NAMES = {v: k for k, v in ACT.items()}
ACT_PREFIX, TP_PREFIX = 'act:', 'tp:'
EXE_TEXT = (0x00401000, 0x01100000)
SETTER_WORDS = ('Set', 'Add', 'Remove', 'Modify', 'Mark', 'Clear', 'Reset', 'Update', 'Create', 'Delete', 'Init',
                'Load', 'Save', 'Apply', 'Push', 'Pop', 'Insert', 'Free', 'Destroy', 'Kill', 'Start', 'Stop',
                'Queue', 'Run', 'Execute', 'Spawn', 'Place', 'Move', 'Equip', 'Unequip', 'Damage', 'Restore', 'Change',
                'Enable', 'Disable', 'Activate', 'Release', 'Allocate', 'Write', 'Store', 'Register', 'Unregister',
                'Attach', 'Detach', 'Play', 'Fire', 'Cast', 'Dispel', 'Increment', 'Decrement', 'Toggle')
GETTER = re.compile(r'^(Get|Is|Has|Can|Count|Find|Lookup|Calc|Calculate|Compute|Should|Was|Does|Contains|Check)[A-Z0-9_]')
ALWAYS_OK_CALLEES = re.compile(r'^(BSString::|NiFixedString::|BSSimpleList|NiTArray|NiTList|BSSpinLock::|'
                               r'ScrapHeap|MemoryManager::|FormHeap_|GetSingleton|operator )')


def _stack_words(layout):
    words = 0
    for a in layout:
        if a.get('where') == 'stack':
            words = max(words, a['index'] + (2 if a['kind'] in ('double', 'int64') else 1))
        elif a.get('where') is None:
            return None  # by-value struct: size unknown
    return words


class ControlTools:
    def __init__(self, agent):
        self.agent = agent
        self.live = agent.live
        self.deep = agent.deep
        self._state = {}

    @property
    def link(self):
        return self.live.link

    # ================================================================== shared
    def _function(self, function, allow_unproven=False):
        engine = self.live.engine()
        address = engine.resolve(function)
        record = engine.summary(address)
        if record.get('status') not in ('proven', 'class_proven', 'class proven') and not allow_unproven:
            raise HookError('UNPROVEN_FUNCTION', '%s is %s, not proven: pass allow_unproven=true to hook it anyway '
                            '(fnv_func_find shows the status)' % (record.get('name') or function, record.get('status')),
                            {'function': record})
        fn = engine.functions.get(address, {})
        sig = parse_signature(fn.get('signature') or '')
        return address, record, fn.get('signature'), sig

    def _install(self, address, label, flags, nargs, filter_sel, filter_value, prefixes):
        for h in self.deep.hooks():
            if int(h['target'], 16) != address:
                continue
            if any(h['label'].startswith(p) for p in prefixes):
                self.deep.remove(h['id'])
                continue
            raise HookError('ALREADY_HOOKED', 'The function already carries the "%s" hook (another tool); remove it '
                            'with fnv_hooks first' % h['label'], {'hook': h['label']})
        raw = self.live.reads([(address, 24)])[0]
        if raw is None:
            raise HookError('UNREADABLE', 'Cannot read the function at 0x%08X' % address)
        clean = self.deep._clean_bytes(address, 24)
        if clean is not None and raw[:8] != clean[:8] and raw[0] != 0xE9:
            raise HookError('PATCHED_ELSEWHERE', 'Another module changed the start of this function in a way a hook '
                            'cannot chain onto', {'live': raw[:16].hex(' '), 'clean': clean[:16].hex(' ')})
        stolen, relocs = decode_prologue(raw)
        result = self.link.one('hook', word(address), word(stolen), ','.join('%x' % r for r in relocs) or '-',
                               word(flags), word(filter_sel), word(filter_value), '0', word(nargs),
                               '0', '0', '0', '0', hexs(label))
        if not result.get('ok'):
            raise HookError('HOOK_REFUSED', 'The companion refused the hook: %s' % result.get('error'), result)
        return result['hook']

    def _value_word(self, value, kind):
        if kind == 'float':
            return struct.unpack('<I', struct.pack('<f', float(value)))[0]
        if kind == 'bool':
            return 1 if value in (True, 1, '1', 'true', 'True') else 0
        if kind in ('ref', 'form'):
            return self.live.resolve(str(value))['ptr']
        if kind == 'ptr':
            return int(str(value), 16) if str(value).lower().startswith('0x') else int(value)
        return (int(value, 0) if isinstance(value, str) else int(value)) & 0xFFFFFFFF

    def _decode_calls(self, h, address, record, signature, sig, since_key, limit, with_stack=False):
        """Records since the last read, decoded with the decomp signature (shares fnv_trace's reader)."""
        state = self.agent._traces.setdefault(address, {'since': 0, 'callsAtStart': 0, 'filter': None, 'started': None})
        before = state['since']
        layout = layout_args(sig)[0] if sig else []
        out = self.agent._read_trace(h, address, record, signature, sig, layout, limit, True)
        if with_stack:
            seq, recs, _ = self.deep.records(h, before, limit=4096)
            stacks = {r[0]: self._call_stack(r[24:48]) for r in recs}
            for call in out.get('calls') or []:
                call['stack'] = stacks.get(call['seq'])
        return out

    def _call_stack(self, words, depth=12):
        """Return addresses among the stack words: kept when the bytes before them are a call instruction."""
        candidates = [w for w in words if w and EXE_TEXT[0] <= w < EXE_TEXT[1]]
        if not candidates:
            return []
        image = self.deep._image(required=False)
        blobs = {}
        if image:
            for w in candidates:
                try:
                    blobs[w] = image.read(w - 7, 7)
                except Exception:
                    blobs[w] = None
        missing = [w for w in candidates if blobs.get(w) is None]
        if missing:
            for w, raw in zip(missing, self.live.reads([(w - 7, 7) for w in missing])):
                blobs[w] = raw
        out = []
        for w in candidates:
            b = blobs.get(w)
            if not b or len(b) < 7:
                continue
            # call rel32 (5 bytes) or call r/m32 (FF /2: 2, 3, 4, 6 or 7 bytes long)
            is_call = b[2] == 0xE8 or any(b[k] == 0xFF and (b[k + 1] >> 3) & 7 == 2 for k in (0, 1, 3, 4, 5))
            if is_call:
                out.append({'returnTo': '0x%08X' % w, 'in': self.agent._name_of(w)})
            if len(out) >= depth:
                break
        return out

    # ================================================================== fnv_hook_set
    def hook_set(self, function, action='skip', value=None, value_type=None, arg=None, filter=None, limit=0,
                 script=None, pop_bytes=None, allow_unproven=False, read_limit=30):
        """action: skip | result | arg | script | read | remove | list."""
        self.live.in_game()
        if action == 'list':
            return {'hooks': [self._hook_view(h) for h in self.deep.hooks() if h['label'].startswith(ACT_PREFIX)]}
        address, record, signature, sig = self._function(function, allow_unproven)
        label = ACT_PREFIX + '%08X' % address
        if action in ('read', 'remove'):
            h = self.deep.hook(label)
            if not h:
                raise LiveError('NOT_HOOKED', 'No behaviour hook on %s' % (record.get('name') or function))
            out = self._decode_calls(h, address, record, signature, sig, label, read_limit)
            out['hook'] = self._hook_view(h)
            if out['hook']['action'] == 'result':
                out['resultNote'] = ('returned = what the function computed; callers got %s instead'
                                     % out['hook']['actionValue'])
            elif out['hook']['action'] in ('skip', 'skip_float'):
                out['resultNote'] = 'skipped calls did not run: callers got %s' % out['hook']['actionValue']
            if action == 'remove':
                self.deep.remove(h['id'])
                self.live.journal.add({'kind': 'hook_removed', 'label': label, 'function': record.get('name'),
                                       'undo': None})
                out['removed'] = True
            return out
        if action not in ACT or action == 'off':
            raise ValueError('action is skip, result, arg, script, read, remove or list')
        layout, nargs = layout_args(sig) if sig else ([], 8)
        returns = param_kind(sig['returns']) if sig else None
        mode, aux, val_kind = ACT[action], 0, value_type
        if action in ('skip', 'result'):
            if returns is None and value_type is None:
                raise ValueError('The decomp has no signature for this function: give value_type')
            val_kind = value_type or {'float': 'float', 'double': 'float', 'bool': 'bool', 'pointer': 'ptr'}.get(returns, 'int')
            if action == 'skip':
                if returns in ('float', 'double') or val_kind == 'float':
                    mode = ACT['skip_float']
                    val_kind = 'float'
                if pop_bytes is not None:
                    aux = int(pop_bytes)
                else:
                    if not sig:
                        raise ValueError('Unknown calling convention: give pop_bytes (bytes the function removes)')
                    words = _stack_words(layout)
                    if words is None:
                        raise ValueError('A by-value struct parameter: give pop_bytes')
                    aux = 0 if sig['convention'] == 'cdecl' else 4 * words
                if returns == 'void' and value is None:
                    value = 0
            elif returns in ('float', 'double'):
                raise ValueError('A float result is returned in st0, which the hook cannot replace after the call: use '
                                 'action=skip with the value instead')
            if value is None:
                raise ValueError('value is required for %s' % action)
        elif action == 'arg':
            if arg is None or value is None:
                raise ValueError('arg (parameter index) and value are required')
            index = int(arg)
            if not sig or index >= len(layout) or layout[index].get('where') is None:
                raise ValueError('The function has no parameter %d the hook can change' % index)
            where = layout[index]
            aux = {'ecx': 100, 'edx': 101}.get(where['where'], where.get('index', 0))
            val_kind = value_type or {'float': 'float', 'bool': 'bool', 'pointer': 'ptr'}.get(where['kind'], 'int')
            if where['kind'] in ('double', 'int64'):
                raise ValueError('64-bit parameters cannot be replaced (one word only)')
        elif action == 'script':
            if not script:
                raise ValueError('script (one console/script line) is required')
            value = 0
        if script and len(script.encode('cp1252', 'replace')) >= 255:
            raise ValueError('script must be shorter than 255 bytes')
        word_value = self._value_word(value, val_kind or 'int') if action != 'script' else 0
        fsel, fval, fdesc = self.agent._filter(sig, layout, filter) if filter else (0, 0, None)
        self.live.changing('behaviour hook on %s' % (record.get('name') or function))
        h = self._install(address, label, HF_RECORD | HF_EXIT, nargs, fsel, fval, (ACT_PREFIX, TRACE_PREFIX, TP_PREFIX))
        result = self.link.one('hookact', word(h['id']), word(mode), word(word_value), word(aux), word(int(limit or 0)),
                               hexs(script) if script else '-')
        if not result.get('ok'):
            self.deep.remove(h['id'])
            raise HookError('HOOK_REFUSED', 'The companion refused the action: %s' % result.get('error'), result)
        seq = self.live.u32s([int(h['seqAddress'], 16)])[0] or 0
        self.agent._traces[address] = {'since': seq, 'callsAtStart': h['calls'], 'filter': fdesc, 'started': time.time()}
        entry = self.live.journal.add({'kind': 'hook_set', 'function': record.get('name') or function,
                                       'address': '0x%08X' % address, 'action': action, 'value': value,
                                       'arg': arg, 'filter': fdesc, 'limit': limit, 'script': script,
                                       'undo': {'op': 'unhook', 'label': label},
                                       'pid': (self.link.hello or {}).get('pid')})
        return {'installed': True, 'function': record, 'signature': signature, 'action': ACT_NAMES[mode],
                'value': value, 'valueWord': '0x%08X' % word_value, 'popBytes': aux if mode in (1, 2) else None,
                'argument': {'index': arg, 'in': layout[int(arg)]['where'] if action == 'arg' else None} if action == 'arg' else None,
                'filter': fdesc, 'limit': limit or None, 'script': script, 'journalId': entry['id'],
                'hook': self._hook_view(result['hook']),
                'note': 'Active now for every call that passes the filter (any thread). action=read shows the calls '
                        'it changed; fnv_undo or action=remove takes it out and the function behaves as before.'}

    @staticmethod
    def _hook_view(h):
        return {'id': h['id'], 'label': h['label'], 'function': h['target'], 'calls': h['calls'],
                'action': ACT_NAMES.get(h.get('action', 0)), 'actionValue': h.get('actionValue'),
                'actionHits': h.get('actionHits'), 'actionLimit': h.get('actionLimit') or None,
                'filter': h.get('filter'), 'filterValue': h.get('filterValue')}

    # ================================================================== fnv_patch
    def patch(self, address, bytes_hex=None, nop=None, ret=None, jmp=None, call=None, expect=None, force=False):
        self.live.ready()
        at = int(str(address), 16) if str(address).lower().startswith('0x') or re.fullmatch(r'[0-9A-Fa-f]{6,8}', str(address)) \
            else self.live.engine().resolve(str(address))
        if sum(x is not None for x in (bytes_hex, nop, ret, jmp, call)) != 1:
            raise ValueError('Give exactly one of bytes, nop, ret, jmp, call')
        if bytes_hex is not None:
            data = bytes.fromhex(re.sub(r'\s', '', bytes_hex))
        elif nop is not None:
            data = b'\x90' * int(nop)
        elif ret is not None:
            data = b'\xC3' if int(ret) == 0 else b'\xC2' + struct.pack('<H', int(ret))
        else:
            target = jmp if jmp is not None else call
            dest = int(str(target), 16) if str(target).lower().startswith('0x') else self.live.engine().resolve(str(target))
            data = (b'\xE9' if jmp is not None else b'\xE8') + struct.pack('<i', dest - (at + 5))
        if not 1 <= len(data) <= 64:
            raise ValueError('A patch is 1..64 bytes')
        current = self.live.reads([(at, len(data))])[0]
        if current is None:
            raise LiveError('UNREADABLE', 'Cannot read 0x%08X' % at)
        if expect is not None and current != bytes.fromhex(re.sub(r'\s', '', expect)):
            raise LiveError('UNEXPECTED_BYTES', 'The bytes at 0x%08X are not the expected ones' % at,
                            {'live': current.hex(' '), 'expected': expect})
        clean = self.deep._clean_bytes(at, len(data)) if EXE_TEXT[0] <= at < EXE_TEXT[1] else None
        foreign = clean is not None and current != clean
        hooks = [h for h in self.deep.hooks() if int(h['target'], 16) <= at < int(h['target'], 16) + h['stolen']]
        if hooks:
            raise LiveError('HOOKED_HERE', 'A companion hook (%s) owns these bytes: remove it first' % hooks[0]['label'])
        if foreign and not force:
            raise LiveError('PATCHED_ELSEWHERE', 'Another module already changed these bytes: pass force=true to '
                            'overwrite them (fnv_patch_map shows who)', {'live': current.hex(' '), 'clean': clean.hex(' ')})
        engine = self.live.engine()
        sym = engine.symbolize(at) if EXE_TEXT[0] <= at < EXE_TEXT[1] else {}
        self.live.changing('code patch at 0x%08X' % at)
        old = self.link.write(at, data)
        entry = self.live.journal.add({'kind': 'patch', 'address': '0x%08X' % at, 'old': old.hex(), 'new': data.hex(),
                                       'function': sym.get('name'),
                                       'undo': {'op': 'write', 'address': '0x%08X' % at, 'hex': old.hex()},
                                       'pid': (self.link.hello or {}).get('pid')})
        return {'address': '0x%08X' % at, 'in': {'function': sym.get('name'), 'offset': sym.get('offset'),
                                                  'status': sym.get('status')} if sym else None,
                'old': old.hex(' '), 'new': data.hex(' '), 'overwroteForeignPatch': foreign,
                'cleanBytes': clean.hex(' ') if clean is not None else None, 'journalId': entry['id'], 'changed': old != data,
                'note': 'Live in the running game until undone (fnv_undo with this journal id) or the game restarts. '
                        'A patch that splits an instruction crashes when the CPU reaches it: patch whole instructions.'}

    # ================================================================== fnv_tracepoint
    def tracepoint(self, function, action='start', when=None, limit=30, allow_unproven=False, stack=True):
        """when: [{on: this|caller|param0..7, ref|value}] - the first is checked in the game, the rest here."""
        self.live.in_game()
        if action == 'list':
            return {'tracepoints': [{'label': h['label'], 'id': h['id'], 'calls': h['calls'], 'hits': h['seq'],
                                     'function': self.agent._name_of(int(h['target'], 16))}
                                    for h in self.deep.hooks() if h['label'].startswith(TP_PREFIX)]}
        address, record, signature, sig = self._function(function, allow_unproven)
        label = TP_PREFIX + '%08X' % address
        layout, nargs = layout_args(sig) if sig else ([], 8)
        conditions = list(when or [])
        if action == 'start':
            fsel, fval, fdesc = self.agent._filter(sig, layout, conditions[0]) if conditions else (0, 0, None)
            rest = [self.agent._filter(sig, layout, c) for c in conditions[1:]]
            flags = HF_RECORD | HF_EXIT | (HF_STACK if stack else 0)
            h = self._install(address, label, flags, nargs, fsel, fval, (TP_PREFIX, TRACE_PREFIX))
            seq = self.live.u32s([int(h['seqAddress'], 16)])[0] or 0
            self.agent._traces[address] = {'since': seq, 'callsAtStart': h['calls'], 'filter': fdesc,
                                           'started': time.time()}
            self._state[label] = {'rest': [(s, v, d) for s, v, d in rest], 'conditions': conditions, 'hits': []}
            return {'started': True, 'function': record, 'signature': signature, 'conditions': conditions,
                    'checkedInGame': fdesc, 'checkedHere': [d for _, _, d in rest],
                    'note': 'Recording calls that meet the conditions, with their call stacks. The game never stops. '
                            'Read with action=read; stop with action=stop.'}
        h = self.deep.hook(label)
        if not h:
            raise LiveError('NOT_TRACING', 'No tracepoint on %s: start one first' % (record.get('name') or function))
        out = self._decode_calls(h, address, record, signature, sig, label, 4096, with_stack=stack)
        st = self._state.setdefault(label, {'rest': [], 'conditions': [], 'hits': []})
        kept = [c for c in out.get('calls') or [] if self._matches(c, st['rest'])]
        st['hits'] = (st['hits'] + kept)[-500:]
        stacks = Counter(' < '.join(str((f['in'] or {}).get('function') if isinstance(f['in'], dict) else f['in'])
                                    for f in (c.get('stack') or [])[:4]) for c in st['hits'])
        result = {'function': record, 'conditions': st['conditions'], 'hitsTotal': len(st['hits']),
                  'newHits': len(kept), 'callsSinceStart': out.get('callsSinceStart'), 'hits': st['hits'][-limit:],
                  'commonStacks': [{'stack': k, 'count': n} for k, n in stacks.most_common(8)],
                  'byCaller': out.get('byCaller'), 'overwritten': out.get('overwritten'),
                  'note': 'stack lists return addresses found on the stack above the call (each preceded by a call '
                          'instruction), innermost first: the functions the call came through. Deeper entries can be stale values left on the stack by earlier calls; trust the first frames most.'}
        if action == 'stop':
            self.deep.remove(h['id'])
            self._state.pop(label, None)
            result['stopped'] = True
        return result

    @staticmethod
    def _matches(call, rest):
        for selector, value, desc in rest:
            on = desc['on']
            if on == 'this':
                got = (call.get('this') or {}).get('ptr')
            elif on == 'caller':
                got = (call.get('caller') or {}).get('address') if isinstance(call.get('caller'), dict) else None
            else:
                index = int(on[-1])
                args = call.get('args') or []
                got = args[index]['value'] if index < len(args) else None
            if got is None:
                return False
            try:
                number = int(got, 16) if isinstance(got, str) and got.startswith('0x') else int(got)
            except (TypeError, ValueError):
                return False
            if number & 0xFFFFFFFF != value:
                return False
        return True

    # ================================================================== fnv_engine_get
    def engine_get(self, function, this=None, args=None):
        self.live.ready()
        engine = self.live.engine()
        address = engine.resolve(function)
        record = engine.summary(address)
        name = record.get('name') or ''
        short = name.split('::')[-1]
        if record.get('status') not in ('proven', 'class_proven'):
            raise LiveError('NOT_ALLOWED', '%s is %s: fnv_engine_get calls proven functions only (fnv_engine_call can '
                            'call anything, journaled)' % (name or function, record.get('status')), {'function': record})
        if not GETTER.match(short):
            raise LiveError('NOT_A_GETTER', '%s does not have a getter name (Get/Is/Has/Can/Count/Find...): use '
                            'fnv_engine_call' % name, {'function': record})
        calls, _ = engine.walk(address, 'callees', 1, 200)
        setters = [c['name'] for c in calls if c.get('name') and not ALWAYS_OK_CALLEES.match(c['name']) and
                   c['name'].split('::')[-1].startswith(SETTER_WORDS)]
        if setters:
            raise LiveError('MAY_WRITE', '%s calls functions that change state (%s): use fnv_engine_call (journaled)'
                            % (name, ', '.join(setters[:5])), {'function': record, 'calls': setters})
        fn = engine.functions.get(address, {})
        sig = parse_signature(fn.get('signature') or '')
        returns = param_kind(sig['returns']) if sig else 'uint'
        ret = {'float': 'float', 'double': 'double', 'void': 'void', 'bool': 'bool', 'pointer': 'ptr'}.get(returns, 'int')
        typed = []
        layout = layout_args(sig)[0] if sig else []
        for i, a in enumerate(args or []):
            if isinstance(a, dict):
                typed.append(a)
                continue
            kind = layout[i]['kind'] if i < len(layout) else 'int'
            typed.append({'type': {'float': 'float', 'double': 'double', 'bool': 'bool', 'pointer':
                                   'ref' if isinstance(a, str) and not a.lower().startswith('0x') else 'ptr'}.get(kind, 'int'),
                          'value': a})
        result = self._call(address, record, sig, this, typed, ret)
        out = {'function': record, 'signature': fn.get('signature'), 'result': result['value'],
               'uses': [name], 'note': 'A proven getter whose direct callees change nothing; nothing was journaled.'}
        if ret == 'ptr' and result['raw']:
            described = self.agent.describe_pointers({result['raw']})
            if result['raw'] in described:
                out['object'] = described[result['raw']]
        return out

    def _call(self, address, record, sig, this, typed, ret):
        live = self.live
        conv = (sig or {}).get('convention') or ('thiscall' if this is not None else 'cdecl')
        words = []
        for a in typed:
            kind, value = a.get('type', 'int'), a.get('value')
            if kind in ('int', 'uint', 'bool'):
                words.append((int(value, 0) if isinstance(value, str) else int(value)) & 0xFFFFFFFF)
            elif kind == 'ptr':
                words.append(int(str(value), 16) if str(value).lower().startswith('0x') else int(value))
            elif kind in ('ref', 'form'):
                words.append(live.resolve(str(value))['ptr'])
            elif kind == 'float':
                words.append(struct.unpack('<I', struct.pack('<f', float(value)))[0])
            elif kind == 'double':
                words += list(struct.unpack('<II', struct.pack('<d', float(value))))
            else:
                raise ValueError('Argument type %r: int, bool, ptr, ref, form, float or double' % kind)
        this_ptr = 0
        if this is not None:
            this_ptr = int(this, 16) if isinstance(this, str) and this.lower().startswith('0x') else live.resolve(str(this))['ptr']
        ecx = edx = 0
        if conv == 'thiscall':
            if this is None:
                raise ValueError('%s is a method: give this (a ref, form or 0x pointer)' % record.get('name'))
            ecx = this_ptr
        elif conv == 'fastcall':
            regs = ([this_ptr] if this is not None else []) + words
            ecx, edx = (regs + [0, 0])[:2]
            words = regs[2:]
        elif this is not None:
            words = [this_ptr] + words
        code = {'float': 'f', 'double': 'f', 'void': 'v'}.get(ret, 'i')
        result = self.link.one('call', word(address), code, word(ecx), word(edx), *[word(w) for w in words])
        if not result.get('ok'):
            raise LiveError('CALL_REJECTED', 'The companion refused the call: %s' % result.get('error'), result)
        eax = int(result['eax'], 16)
        value = {'int': struct.unpack('<i', struct.pack('<I', eax))[0], 'bool': bool(eax & 0xFF), 'ptr': '0x%08X' % eax,
                 'float': result.get('st0'), 'double': result.get('st0'), 'void': None}.get(ret, eax)
        return {'value': value, 'raw': eax}
