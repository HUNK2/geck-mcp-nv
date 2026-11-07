"""Tools for agents that test and debug their mods on their own (fnv-mcp agent wave, after P1).

They lean on the game's own code: decomp names with proof status, decomp signatures, class layouts and the
engine's own functions. Kinds as everywhere: read (changes nothing), act (journaled, needs engine_control=full).

  fnv_trace          read     any proven engine function: calls, arguments decoded with its decomp signature,
                              results, callers by name (watch hook, never changes the call)
  fnv_watch          read     values sampled over time: expressions, memory fields by class layout, actor values
  fnv_screenshot     read     the frame: the engine's own back-buffer capture (SaveScreenshot), else the window
  fnv_test_save      act      named test saves kept apart from other saves (save, load, list, delete)
  fnv_crash_history  read     crash logs grouped by signature across runs: is it the same crash again?
  fnv_move           act      teleport the player or a ref to a ref, a cell or a position; undoable
  fnv_spawn          act      place a form near a ref and return the new refs; undoable
"""
import json
import re
import struct
import time
from collections import Counter
from pathlib import Path

from . import fnv_crash, fnv_link
from .fnv_deep import HF_EXIT, HF_RECORD, RECORD_WORDS, RING, HookError, decode_prologue  # noqa: F401 (RECORD_WORDS for tests)
from .fnv_link import hexs, word
from .fnv_live import LiveError

TRACE_PREFIX = 'trace:'
TEST_SAVE_PREFIX = 'fnvmcp_test_'
SAVE_NAME = re.compile(r'^[A-Za-z0-9_\-]{1,60}$')
SAVE_SCREENSHOT = 0x00878860   # SaveScreenshot(const char *path): decomp name, identified by its own log strings
SELECTOR = {'ecx': 1, 'edx': 11, 'caller': 10}


# ====================================================================================== signatures
def split_params(inner):
    params, depth, current = [], 0, ''
    for c in inner:
        if c in '<(':
            depth += 1
        elif c in '>)':
            depth -= 1
        if c == ',' and depth == 0:
            params.append(current.strip())
            current = ''
        else:
            current += c
    if current.strip() and current.strip() != 'void':
        params.append(current.strip())
    return params


def parse_signature(signature):
    """'public: bool __thiscall ns::A::F(float,class X *)' -> {'returns', 'convention', 'params'} or None."""
    m = re.search(r'__(thiscall|cdecl|stdcall|fastcall)\s', signature or '')
    if not m:
        return None
    returns = re.sub(r'\b(public|private|protected):|\b(virtual|static)\b', '', signature[:m.start()]).strip()
    start = signature.find('(', m.end())
    depth, end = 0, None
    for i in range(start, len(signature)):
        if signature[i] == '(':
            depth += 1
        elif signature[i] == ')':
            depth -= 1
            if depth == 0:
                end = i
                break
    inner = signature[start + 1:end] if start >= 0 and end else ''
    return {'returns': returns, 'convention': m.group(1), 'params': split_params(inner)}


def param_kind(text):
    t = re.sub(r'\bconst\b', '', text or '').strip()
    if '*' in t or t.endswith('&'):
        return 'pointer'
    if t == 'float':
        return 'float'
    if t == 'double':
        return 'double'
    if t == 'bool':
        return 'bool'
    if '__int64' in t:
        return 'int64'
    if 'char' in t:
        return 'byte'
    if 'short' in t:
        return 'short'
    if t.startswith('enum') or t in ('int', 'long', 'signed int', 'signed long'):
        return 'int'
    if 'unsigned' in t:
        return 'uint'
    if t.startswith(('struct', 'class', 'union')):
        return 'struct'   # passed by value: size unknown, later arguments cannot be placed
    if t in ('', 'void'):
        return 'void'
    return 'int'


def layout_args(sig, nargs_max=8):
    """Where each parameter lives: ('ecx'|'edx'|'stack', word index, kind). Stops at a by-value struct."""
    out, stack, regs = [], 0, ['ecx', 'edx'] if sig['convention'] == 'fastcall' else []
    for text in sig['params']:
        kind = param_kind(text)
        if kind == 'struct':
            out.append({'type': text, 'kind': kind, 'where': None})
            break
        words = 2 if kind in ('double', 'int64') else 1
        if regs and words == 1 and kind != 'float':  # MSVC fastcall: integral and pointer arguments only
            out.append({'type': text, 'kind': kind, 'where': regs.pop(0)})
            continue
        out.append({'type': text, 'kind': kind, 'where': 'stack', 'index': stack})
        stack += words
    return out, min(stack, nargs_max)


def clean_type(text):
    t = re.sub(r'\b(class|struct|const|enum)\b', '', text or '')
    t = re.sub(r'\bfnv_[0-9a-fA-F]{8}::|\bFUN_[0-9a-fA-F]{8}_shell::', '', t)
    return re.sub(r'\s+', ' ', t).strip()


def _f32(w):
    return round(struct.unpack('<f', struct.pack('<I', w & 0xFFFFFFFF))[0], 4)


def decode_word(kind, words, i):
    w = words[i] if i < len(words) else 0
    if kind == 'float':
        return _f32(w)
    if kind == 'double':
        hi = words[i + 1] if i + 1 < len(words) else 0
        return round(struct.unpack('<d', struct.pack('<II', w, hi))[0], 6)
    if kind == 'int64':
        hi = words[i + 1] if i + 1 < len(words) else 0
        return struct.unpack('<q', struct.pack('<II', w, hi))[0]
    if kind == 'bool':
        return bool(w & 0xFF)
    if kind == 'byte':
        return w & 0xFF
    if kind == 'short':
        return w & 0xFFFF
    if kind == 'int':
        return struct.unpack('<i', struct.pack('<I', w))[0]
    if kind == 'pointer':
        return '0x%08X' % w
    return w


def _plausible_pointer(value):
    return 0x00010000 <= value < 0x7FFF0000 and value & 3 == 0


# ====================================================================================== the tools
class AgentTools:
    def __init__(self, live, deep, social=None, data=None):
        self.live = live
        self.deep = deep
        self.social = social
        self.data = data
        self._traces = {}
        self._form_classes = None

    @property
    def link(self):
        return self.live.link

    @property
    def game(self):
        return self.live.game

    # ------------------------------------------------------------------ pointer rendering
    def form_classes(self):
        if self._form_classes is None:
            try:
                names, _ = self.live.engine().descendants('TESForm', limit=5000)
                self._form_classes = set(names) | {'TESForm'}
            except Exception:
                self._form_classes = set()
        return self._form_classes

    def describe_pointers(self, pointers):
        """Pointer -> {'class', 'formId', 'formType', 'editorId', 'name'} for objects with a vtable (RTTI)."""
        pointers = sorted({p for p in pointers if p and _plausible_pointer(p)})
        if not pointers:
            return {}
        classes = self.live.rtti(pointers)
        forms = [p for p in pointers if (classes.get(p) or {}).get('class') and
                 (classes[p]['class'] in self.form_classes() or classes[p]['class'].startswith(('TES', 'BGS'))
                  or classes[p]['class'] in ('Actor', 'Character', 'Creature', 'PlayerCharacter'))]
        heads = dict(zip(forms, self.live.form_ids(forms))) if forms else {}
        ids = [h['formId'] for h in heads.values() if h and h['formId'] not in (None, '00000000')]
        described = self.deep.describe_forms(ids) if ids else {}
        out = {}
        for p in pointers:
            cls = (classes.get(p) or {}).get('class')
            entry = {'class': cls} if cls else {'class': None}
            head = heads.get(p)
            if head:
                entry.update(formId=head['formId'], formType=head['formType'])
                extra = described.get(head['formId']) or {}
                for key in ('editorId', 'name', 'type', 'master'):
                    if extra.get(key):
                        entry[key] = extra[key]
            out[p] = entry
        return out

    # ================================================================== fnv_trace
    def _trace_function(self, function):
        engine = self.live.engine()
        address = engine.resolve(function)
        record = engine.summary(address)
        if record.get('status') not in ('proven', 'class_proven', 'class proven'):
            raise HookError('UNPROVEN_FUNCTION', '%s is %s: traces go on proven functions only (fnv_func_find shows '
                            'the status)' % (record.get('name') or function, record.get('status')), {'function': record})
        fn = engine.functions.get(address, {})
        sig = parse_signature(fn.get('signature') or '')
        return address, record, fn.get('signature'), sig

    def _filter(self, sig, args_layout, spec):
        if not spec:
            return 0, 0, None
        on = str(spec.get('on', 'this')).lower()
        if on == 'this':
            if not sig or sig['convention'] != 'thiscall':
                raise ValueError('filter on this needs a __thiscall function')
            selector = SELECTOR['ecx']
        elif on == 'caller':
            selector = SELECTOR['caller']
        elif re.fullmatch(r'(arg|param)\d', on):
            index = int(on[-1])
            if index >= len(args_layout) or args_layout[index]['where'] is None:
                raise ValueError('The function has no parameter %d that a filter can read' % index)
            where = args_layout[index]
            selector = SELECTOR[where['where']] if where['where'] in SELECTOR else 2 + where['index']
            if where['where'] == 'stack' and where['index'] > 7:
                raise ValueError('Only the first 8 stack words can be filtered')
        else:
            raise ValueError('filter.on is this, caller or param0..param7')
        if 'ref' in spec:
            value = self.live.resolve(str(spec['ref']))['ptr']
        elif 'value' in spec:
            value = int(spec['value'], 0) if isinstance(spec['value'], str) else int(spec['value'])
        else:
            raise ValueError('filter needs ref or value')
        return selector, value & 0xFFFFFFFF, {'on': on, 'value': '0x%08X' % (value & 0xFFFFFFFF), 'spec': spec}

    def _install(self, address, label, nargs, filter_sel, filter_value):
        installed = self.deep.hooks()
        for h in installed:
            if int(h['target'], 16) != address:
                continue
            if h['label'] == label and h.get('filter') == filter_sel and \
                    int(h.get('filterValue', '0'), 16) == filter_value:
                return h, False
            if h['label'].startswith(TRACE_PREFIX):
                self.deep.remove(h['id'])
            else:
                raise HookError('ALREADY_HOOKED', 'The function already carries the "%s" watch hook (another tool); '
                                'remove it with fnv_hooks first' % h['label'], {'hook': h['label']})
        raw = self.live.reads([(address, 24)])[0]
        if raw is None:
            raise HookError('UNREADABLE', 'Cannot read the function at 0x%08X' % address)
        clean = self.deep._clean_bytes(address, 24)
        if clean is not None and raw[:8] != clean[:8] and raw[0] != 0xE9:
            raise HookError('PATCHED_ELSEWHERE', 'Another module changed the start of this function in a way a hook '
                            'cannot chain onto', {'live': raw[:16].hex(' '), 'clean': clean[:16].hex(' ')})
        stolen, relocs = decode_prologue(raw)
        result = self.link.one('hook', word(address), word(stolen), ','.join('%x' % r for r in relocs) or '-',
                               word(HF_RECORD | HF_EXIT), word(filter_sel), word(filter_value), '0', word(nargs),
                               '0', '0', '0', '0', hexs(label))
        if not result.get('ok'):
            raise HookError('HOOK_REFUSED', 'The companion refused the hook: %s' % result.get('error'), result)
        return result['hook'], True

    def trace(self, function=None, action='start', filter=None, limit=50, include_calls=True):
        """action: start | read | stop | list."""
        self.live.in_game()
        if action == 'list':
            return {'traces': [{'label': h['label'], 'id': h['id'], 'calls': h['calls'], 'recorded': h['seq'],
                                'function': self._name_of(int(h['target'], 16))}
                               for h in self.deep.hooks() if h['label'].startswith(TRACE_PREFIX)]}
        if not function:
            raise ValueError('function is required')
        address, record, signature, sig = self._trace_function(function)
        args_layout, nargs = layout_args(sig) if sig else ([], 8)
        label = TRACE_PREFIX + '%08X' % address
        if action == 'stop':
            h = self.deep.hook(label)
            out = self._read_trace(h, address, record, signature, sig, args_layout, limit, include_calls) if h else None
            if h:
                self.deep.remove(h['id'])
            self._traces.pop(address, None)
            return {'stopped': bool(h), 'function': record, 'final': out}
        if action == 'start':
            fsel, fval, fdesc = self._filter(sig, args_layout, filter)
            h, new = self._install(address, label, nargs, fsel, fval)
            seq = self.live.u32s([int(h['seqAddress'], 16)])[0] or 0
            calls = self.live.u32s([int(h['callsAddress'], 16)])[0] or 0
            self._traces[address] = {'since': seq, 'callsAtStart': calls, 'filter': fdesc, 'started': time.time()}
            return {'started': True, 'installed': new, 'function': record, 'signature': signature,
                    'parameters': [{'type': clean_type(a['type']), 'in': a['where'] if a['where'] != 'stack' else
                                    'stack word %d' % a['index']} for a in args_layout],
                    'convention': sig['convention'] if sig else None, 'filter': fdesc,
                    'note': 'Recording. Let the game run, then call fnv_trace with action=read (or stop). The hook only '
                            'observes: every call goes on unchanged.'}
        if action == 'read':
            h = self.deep.hook(label)
            if not h:
                raise LiveError('NOT_TRACING', 'No trace on %s: start one with action=start' % record.get('name'))
            return self._read_trace(h, address, record, signature, sig, args_layout, limit, include_calls)
        raise ValueError('action is start, read, stop or list')

    def _name_of(self, address):
        try:
            sym = self.live.engine().symbolize(address)
        except Exception:
            return '0x%08X' % address
        if sym.get('kind') == 'function':
            return {'function': sym.get('name'), 'status': sym.get('status'), 'offset': sym.get('offset'),
                    'address': sym.get('functionAddress')}
        return {'address': '0x%08X' % address, 'module': sym.get('module'), 'kind': sym.get('kind')}

    def _read_trace(self, h, address, record, signature, sig, args_layout, limit, include_calls):
        state = self._traces.setdefault(address, {'since': 0, 'callsAtStart': 0, 'filter': None, 'started': None})
        since = state['since']
        seq, recs, lost = self.deep.records(h, since, limit=RING)
        state['since'] = seq
        overwritten = max(0, seq - since - len(recs) - lost)
        calls_now = self.live.u32s([int(h['callsAddress'], 16)])[0] or 0
        pointer_values = set()
        decoded = []
        thiscall = sig and sig['convention'] == 'thiscall'
        returns = param_kind(sig['returns']) if sig else 'uint'
        for r in recs:
            regs = {'ecx': r[3], 'edx': r[4]}
            stack = list(r[12:20])
            args = []
            for a in args_layout:
                if a['where'] is None:
                    args.append({'type': clean_type(a['type']), 'value': 'by-value struct: not decoded'})
                    break
                if a['where'] in regs:
                    value = decode_word(a['kind'], [regs[a['where']]], 0)
                else:
                    value = decode_word(a['kind'], stack, a['index']) if a['index'] < 8 else None
                if a['kind'] == 'pointer' and isinstance(value, str):
                    pointer_values.add(int(value, 16))
                args.append({'type': clean_type(a['type']), 'value': value})
            ret = None
            if r[1] == 2:
                if returns == 'pointer':
                    ret = '0x%08X' % r[8]
                    pointer_values.add(r[8])
                elif returns in ('float', 'double'):
                    ret = 'float in st0: not recorded'
                elif returns == 'void':
                    ret = None
                else:
                    ret = decode_word(returns, [r[8], r[9]], 0)
            if thiscall:
                pointer_values.add(r[3])
            decoded.append({'seq': r[0], 'frame': r[22], 'thread': r[2], 'this': '0x%08X' % r[3] if thiscall else None,
                            'args': args, 'returned': ret, 'finished': r[1] == 2,
                            'us': round(((r[11] << 32) | r[10]) * 1e6 / h['qpcFrequency'], 1) if r[1] == 2 else None,
                            'callerAddress': r[5]})
        described = self.describe_pointers(pointer_values)
        callers = Counter(d['callerAddress'] for d in decoded)
        names = {a: self._name_of(a) for a in callers}
        threads = Counter(d['thread'] for d in decoded)
        try:
            main_thread = self.link.ping().get('mainThreadId')
        except Exception:
            main_thread = None
        main_thread = main_thread or (threads.most_common(1)[0][0] if threads else None)
        for d in decoded:
            d['caller'] = names[d.pop('callerAddress')]
            if d['this']:
                d['this'] = {'ptr': d['this'], **described.get(int(d['this'], 16), {})}
            for a in d['args']:
                if isinstance(a.get('value'), str) and a['value'].startswith('0x') and int(a['value'], 16) in described:
                    a['object'] = described[int(a['value'], 16)]
            if isinstance(d['returned'], str) and d['returned'].startswith('0x') and int(d['returned'], 16) in described:
                d['returnedObject'] = described[int(d['returned'], 16)]
        by_this = Counter(json.dumps(d['this'], sort_keys=True) for d in decoded if d['this'])
        out = {'function': record, 'signature': signature, 'convention': sig['convention'] if sig else None,
               'filter': state.get('filter'), 'callsSinceStart': calls_now - state.get('callsAtStart', 0),
               'recorded': len(decoded), 'lostRecords': lost, 'overwritten': overwritten,
               'byCaller': [{'caller': names[a], 'count': n} for a, n in callers.most_common(15)],
               'byThis': [{'this': json.loads(k), 'count': n} for k, n in by_this.most_common(10)],
               'threads': [{'thread': t, 'count': n, 'main': t == main_thread} for t, n in threads.most_common()],
               'calls': decoded[-limit:] if include_calls else None,
               'note': ('callsSinceStart counts every call (before the filter); recorded = calls that passed the '
                        'filter, read since the last read; overwritten = calls that fell out of the 4096-call ring '
                        'before this read (read more often or filter). Arguments are decoded with the decomp signature; float '
                        'results come back in st0, which the hook does not record.'),
               'uses': ['engine companion watch hook (records ecx/edx, 8 stack words, caller, eax/edx at exit)',
                        'decomp signature for argument types', 'MSVC RTTI + TESForm layout for objects',
                        'engine map to name the callers']}
        return out

    # ================================================================== fnv_watch
    def _watch_items(self, items):
        prepared = []
        for item in items:
            if isinstance(item, str):
                item = {'eval': item}
            if not isinstance(item, dict):
                raise ValueError('watch items are expressions or mappings')
            label = item.get('label')
            if 'eval' in item:
                ref = self.live.resolve(item['ref'])['ptr'] if item.get('ref') else 0
                expr = self.live.substitute_editor_ids(item['eval'])[0] if hasattr(self.live, 'substitute_editor_ids') \
                    else item['eval']
                prepared.append({'label': label or item['eval'], 'op': ('eval', hexs(expr), word(ref)), 'kind': 'eval'})
            elif 'actor_value' in item:
                ref = self.live.resolve(item.get('ref', 'player'))['ptr']
                prepared.append({'label': label or '%s of %s' % (item['actor_value'], item.get('ref', 'player')),
                                 'op': ('eval', hexs('GetAV %s' % item['actor_value']), word(ref)), 'kind': 'eval'})
            elif 'global' in item:
                expr = 'GetGlobalValue %s' % item['global']
                prepared.append({'label': label or 'global %s' % item['global'], 'op': ('eval', hexs(expr), '0'),
                                 'kind': 'eval'})
            elif 'variable' in item:
                expr = '%s.%s' % (item['of'], item['variable'])
                prepared.append({'label': label or expr, 'op': ('eval', hexs(expr), '0'), 'kind': 'eval'})
            elif 'position' in item:
                ref = self.live.resolve(item['position'])['ptr']
                for axis in 'XYZ':
                    prepared.append({'label': '%s %s' % (item['position'], axis.lower()),
                                     'op': ('eval', hexs('GetPos %s' % axis), word(ref)), 'kind': 'eval'})
            elif 'mem' in item:
                address, ftype, steps = self.live.locate(item['mem'], item.get('path'), item.get('class'))
                kind = (item.get('as') or ftype or 'uint32').strip()
                size = {'float': 4, 'double': 8, 'bool': 1, 'char': 1, 'unsigned char': 1, 'UInt8': 1, 'uint8': 1,
                        'short': 2, 'unsigned short': 2, 'UInt16': 2, 'uint16': 2}.get(kind, 4)
                prepared.append({'label': label or '%s %s' % (item['mem'], item.get('path') or ''),
                                 'op': ('rd', word(address), word(size)), 'kind': 'mem', 'type': kind, 'size': size,
                                 'address': '0x%08X' % address})
            else:
                raise ValueError('watch item: an expression, or {eval}, {actor_value, ref}, {global}, {variable, of}, '
                                 '{position: ref} or {mem, path}')
        return prepared

    @staticmethod
    def _mem_value(item, result):
        if not result.get('ok'):
            return None
        raw = bytes.fromhex(result['hex'])
        kind = item['type']
        if kind == 'float':
            return round(struct.unpack('<f', raw)[0], 4)
        if kind == 'double':
            return round(struct.unpack('<d', raw)[0], 6)
        if item['size'] == 1:
            return raw[0]
        if item['size'] == 2:
            return struct.unpack('<H', raw)[0]
        if kind.endswith('*'):
            return '0x%08X' % struct.unpack('<I', raw)[0]
        if kind in ('int', 'SInt32', 'int32', 'long'):
            return struct.unpack('<i', raw)[0]
        return struct.unpack('<I', raw)[0]

    def watch(self, items, seconds=5.0, interval_ms=100, until=None, keep_unpaused=True, max_samples=600):
        self.live.in_game()
        prepared = self._watch_items(items or [])
        if not prepared:
            raise ValueError('Give at least one item to watch')
        ops = [p['op'] for p in prepared]
        stop_index = None
        if until:
            stop_index = len(ops)
            ops.append(('eval', hexs(self.live.substitute_editor_ids(until)[0]), '0'))
        slot = self.game.keep_unpaused(seconds + 10) if keep_unpaused else None
        series = [[] for _ in prepared]
        stopped_by = None
        started = time.monotonic()
        first_frame = last_frame = None
        samples = 0
        try:
            deadline = started + min(float(seconds), 120.0)
            while time.monotonic() < deadline and samples < max_samples:
                results = self.link.batch(ops, timeout=10)
                t = round(time.monotonic() - started, 3)
                frame = results[0].get('frame') if results else None
                first_frame = first_frame if first_frame is not None else frame
                last_frame = frame
                for k, (item, result) in enumerate(zip(prepared, results)):
                    value = self._mem_value(item, result) if item['kind'] == 'mem' else fnv_link.value_of(result)
                    if not series[k] or series[k][-1]['value'] != value:
                        series[k].append({'t': t, 'frame': frame, 'value': value})
                samples += 1
                if stop_index is not None and fnv_link.value_of(results[stop_index]):
                    stopped_by = {'until': until, 't': t, 'frame': frame}
                    break
                time.sleep(max(0.0, interval_ms / 1000.0))
        finally:
            self.game.stop_task(slot)
        out = []
        for item, points in zip(prepared, series):
            numbers = [p['value'] for p in points if isinstance(p['value'], (int, float)) and not isinstance(p['value'], bool)]
            out.append({'item': item['label'], 'first': points[0]['value'] if points else None,
                        'last': points[-1]['value'] if points else None, 'changes': max(0, len(points) - 1),
                        'min': min(numbers) if numbers else None, 'max': max(numbers) if numbers else None,
                        'timeline': points[:200], 'address': item.get('address')})
        frames = (last_frame - first_frame) if first_frame is not None and last_frame is not None else None
        return {'samples': samples, 'seconds': round(time.monotonic() - started, 2), 'frames': frames,
                'stoppedBy': stopped_by, 'untilMet': bool(stopped_by) if until else None, 'items': out,
                'paused': frames == 0 and samples > 2,
                'note': 'timeline lists the value each time it changed (t in seconds from the start). paused=true means '
                        'the frame counter did not move: the game was paused or stalled.'}

    # ================================================================== fnv_screenshot
    def screenshot(self, name=None, max_width=1280, method='auto'):
        """The frame as PNG under state/fnv/screens. method: auto (engine, then window) | engine | window."""
        from PIL import Image
        proc = self.game.process()
        if not proc:
            raise LiveError('NOT_RUNNING', 'The game is not running')
        folder = Path(self.live.project.state_dir) / 'fnv/screens'
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[^A-Za-z0-9_-]+', '_', name or 'screen')[:60]
        stamp = time.strftime('%Y%m%d-%H%M%S')
        png = folder / ('%s-%s.png' % (safe, stamp))
        tried = []
        if method in ('auto', 'engine'):
            bmp = folder / ('%s-%s.bmp' % (safe, stamp))
            try:
                ok, why = self._engine_capture(bmp)
            except Exception as e:  # best effort
                ok, why = False, str(e)
            tried.append({'method': 'engine SaveScreenshot', 'ok': ok, 'detail': why})
            if ok:
                image = Image.open(bmp)
                image.load()
                bmp.unlink(missing_ok=True)
                return self._finish(image, png, max_width, 'engine back buffer (SaveScreenshot 0x00878860)', tried)
            if method == 'engine':
                raise LiveError('ENGINE_CAPTURE_FAILED', 'The engine could not capture the back buffer: %s' % why,
                                {'tried': tried})
        shot = window_capture(self.game, png)
        tried.append({'method': 'window', 'ok': bool(shot and not shot.get('error')), 'detail': shot})
        if not shot or shot.get('error'):
            raise LiveError('CAPTURE_FAILED', 'No screenshot could be taken', {'tried': tried})
        image = Image.open(shot['path'])
        image.load()
        out = self._finish(image, png, max_width, 'window capture', tried)
        out['blank'] = shot.get('blank')
        if shot.get('blank'):
            out['note'] = ('The window capture is blank: the game window is covered, minimised or does not draw '
                           'through PrintWindow. The engine capture needs iMultiSample=0 in the dev profile\'s '
                           'FalloutPrefs.ini (and bAllowScreenShot=1).')
        return out

    def _engine_capture(self, path):
        """Ask the engine to write its back buffer (SaveScreenshot with a path: no message, no counter)."""
        self.live.ready()
        raw = self.live.reads([(SAVE_SCREENSHOT, 16)])[0]
        clean = self.deep._clean_bytes(SAVE_SCREENSHOT, 16)
        if raw is None or (clean is not None and raw != clean):
            return False, 'SaveScreenshot is not the expected code (patched or other build)'
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        text = self.live._game_string(str(path))
        result = self.link.one('call', word(SAVE_SCREENSHOT), 'v', '0', '0', word(text), timeout=15)
        if not result.get('ok'):
            return False, 'call refused: %s' % result.get('error')
        for _ in range(20):
            if path.exists() and path.stat().st_size > 54:
                return True, 'ok'
            time.sleep(0.1)
        return False, ('no file written: the device refused to lock the back buffer (multisampling on, or '
                       'bAllowScreenShot=0 at startup)')

    @staticmethod
    def _finish(image, png, max_width, method, tried):
        image = image.convert('RGB')
        full = image.size
        if max_width and image.width > max_width:
            image = image.resize((max_width, round(image.height * max_width / image.width)))
        image.save(png)
        return {'path': str(png), 'size': list(image.size), 'fullSize': list(full), 'method': method, 'tried': tried}

    # ================================================================== fnv_test_save
    def test_save(self, action='list', name=None, timeout=180):
        saves = self.game.saves_dir
        if action == 'list':
            files = sorted(saves.glob(TEST_SAVE_PREFIX + '*.fos'), key=lambda p: p.stat().st_mtime, reverse=True) \
                if saves.exists() else []
            return {'folder': str(saves), 'saves': [{'name': p.stem[len(TEST_SAVE_PREFIX):], 'file': p.name,
                                                     'modified': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(p.stat().st_mtime)),
                                                     'bytes': p.stat().st_size} for p in files], 'changed': False}
        if not name or not SAVE_NAME.match(name):
            raise ValueError('name: 1-60 letters, digits, _ or -')
        full = TEST_SAVE_PREFIX + name
        path = saves / (full + '.fos')
        if action == 'delete':
            removed = []
            for p in (path, saves / (full + '.nvse')):
                if p.exists():
                    p.unlink()
                    removed.append(p.name)
            return {'removed': removed, 'changed': bool(removed)}
        self.live.in_game()
        if action == 'save':
            before = path.stat().st_mtime if path.exists() else 0
            result = self.live.console('SaveGame %s' % full)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if path.exists() and path.stat().st_mtime > before and path.stat().st_size > 0:
                    time.sleep(0.5)  # let the co-save land
                    return {'saved': full, 'file': str(path), 'bytes': path.stat().st_size,
                            'output': result.get('output'), 'note': 'Load it in a test with start: {test_save: %s} or '
                                                                    'fnv_test_save action=load.' % name}
                time.sleep(0.25)
            raise LiveError('SAVE_FAILED', 'The game did not write %s' % path.name, {'output': result.get('output')})
        if action == 'load':
            if not path.exists():
                raise LiveError('NOT_FOUND', 'No test save %s in %s' % (name, saves))
            pid = self.game.process()[0]
            seq = (self.link.events(0) or {}).get('seq', 0)
            result = self.link.schedule(0, 'load "%s"' % full)
            if not result.get('ok'):
                raise LiveError('LOAD_FAILED', 'Could not queue the load', result)
            # Wait for xNVSE's post-load message (the companion records it), then for gameplay.
            deadline = time.monotonic() + timeout
            loaded = None
            while time.monotonic() < deadline and loaded is None:
                try:
                    events = (self.link.events(seq) or {}).get('events', [])
                    loaded = next((e for e in events if e.get('type') == 'post_load_game'), None)
                except Exception:   # the pipe is busy while the game loads
                    pass
                if loaded is None:
                    time.sleep(0.5)
            if loaded is None:
                raise LiveError('LOAD_FAILED', 'The game did not report the load of %s' % full)
            if loaded.get('text') != 'success':
                raise LiveError('LOAD_FAILED', 'The game reported a failed load of %s' % full, {'event': loaded})
            ready = self.game.wait_in_game(deadline, pid, start='save:%s' % full)
            return {'loaded': full, **ready}
        raise ValueError('action is save, load, list or delete')

    # ================================================================== fnv_crash_history
    def crash_history(self, limit=40, include_shutdown=True):
        logs = []
        for d in fnv_crash.candidate_dirs(self.live.project):
            if not d.is_dir():
                continue
            for p in d.iterdir():
                name = p.name.lower()
                if p.is_file() and p.suffix.lower() == '.log' and ('crash' in name or d.name.lower() == 'crash logs'):
                    logs.append(p)
        logs = sorted({str(p).lower(): p for p in logs}.values(), key=lambda p: p.stat().st_mtime, reverse=True)
        logs = [p for p in logs if fnv_crash._has_crash(p)][:limit + 1]
        try:
            engine = self.live.engine()
        except Exception:
            engine = None
        groups = {}
        seen_path = Path(self.live.project.state_dir) / 'fnv/crash-signatures.json'
        seen = json.loads(seen_path.read_text(encoding='utf-8')) if seen_path.exists() else {}
        errors = []
        seen_moments = set()
        for p in logs:
            try:
                parsed = fnv_crash.parse_file(p)
                info = fnv_crash.explain(parsed, engine, self.live.project, max_frames=12)
            except Exception as e:
                errors.append({'file': str(p), 'error': str(e)})
                continue
            sig, parts = crash_signature(info)
            if info.get('duringShutdown') and not include_shutdown:
                continue
            # CrashLogger.log next to the exe and the dated copy in Crash Logs are the same crash.
            moment = (sig, round(p.stat().st_mtime / 5))
            if moment in seen_moments:
                continue
            seen_moments.add(moment)
            g = groups.setdefault(sig, {'signature': sig, 'parts': parts, 'count': 0, 'files': [],
                                        'duringShutdown': bool(info.get('duringShutdown')), 'example': None})
            g['count'] += 1
            when = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(p.stat().st_mtime))
            g['files'].append({'file': str(p), 'modified': when})
            g['first'] = when if not g.get('first') or when < g['first'] else g['first']
            g['last'] = when if not g.get('last') or when > g['last'] else g['last']
            if g['example'] is None:
                culprits = info.get('likelyCulprits') or []
                g['example'] = {'exception': (info.get('exception') or {}).get('code'),
                                'meaning': (info.get('exception') or {}).get('meaning'),
                                'crashSite': info.get('crashSite'),
                                'likelyCulprits': culprits[:3], 'runningScript': info.get('runningScript')}
        out = sorted(groups.values(), key=lambda g: g['last'], reverse=True)
        for g in out:
            g['new'] = g['signature'] not in seen
            g['files'] = g['files'][:5]
        for g in out:
            seen.setdefault(g['signature'], g['first'])
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(json.dumps(seen, indent=1) + '\n', encoding='utf-8')
        return {'logs': len(seen_moments), 'groups': out, 'distinct': len(out),
                'shutdownOnly': sum(1 for g in out if g['duringShutdown']), 'unreadable': errors[:10],
                'note': 'signature = exception code + crash site function (or module+offset) + the next game frames. '
                        'new=true: not seen by an earlier fnv_crash_history call. duringShutdown groups are crashes '
                        'in exit cleanup (they happen on quit), usually not your mod.'}

    # ================================================================== fnv_move
    def move(self, ref='player', to=None, cell=None, position=None, rotation=None, timeout=180):
        self.live.in_game()
        r = self.live.resolve(ref)
        before = self._place(r)
        is_player = r['formId'] == '00000014' or ref.lower() in ('player', 'playerref')
        lines, wait = [], False
        if to:
            target = self.live.resolve(to)
            lines.append('MoveTo %s' % target['formId'])
            wait = is_player
        elif cell:
            if not re.fullmatch(r'[A-Za-z0-9_]{1,120}', cell):
                raise ValueError('cell is a cell EditorID or form id')
            if position:
                # JIP LN MoveToCell: any ref, any cell, exact coordinates (FNV has no PositionCell/PositionWorld).
                target_cell = self.live.resolve(cell)['formId']
                p = position
                lines.append('MoveToCell %s %s %s %s' % (target_cell, p.get('x', 0), p.get('y', 0), p.get('z', 0)))
            elif is_player:
                lines.append('coc %s' % cell)
            else:
                raise ValueError('Moving a non-player ref into a cell needs a position in that cell')
            wait = is_player
        elif position:
            for axis in 'xyz':
                if axis in position:
                    lines.append('SetPos %s %s' % (axis.upper(), float(position[axis])))
        else:
            raise ValueError('Give to (a ref), cell or position')
        if rotation:
            for axis in 'xyz':
                if axis in rotation:
                    lines.append('SetAngle %s %s' % (axis.upper(), float(rotation[axis])))
        self.live.changing('move %s' % ref)
        pid = self.game.process()[0]
        outputs = []
        if wait and (cell or to):
            # The cell change runs after the reply: queue it, then wait for gameplay.
            for line in lines:
                if line.startswith('coc'):
                    result = self.link.schedule(0, line)
                elif line.startswith('MoveTo'):   # the player's MoveTo / MoveToCell, after the reply
                    result = self.link.schedule(0, 'player.' + line)
                else:
                    result = {'ok': self.live.console(line, r['ptr'])['accepted']}
                outputs.append({'line': line, 'ok': result.get('ok')})
            time.sleep(1.0)
            ready = self.game.wait_in_game(time.monotonic() + timeout, pid, start='move')
        else:
            for line in lines:
                result = self.live.console(line, r['ptr'])
                outputs.append({'line': line, 'ok': result['accepted'], 'output': result['output'][:5]})
            ready = None
        after = self._place(r)
        undo_lines = self._undo_place(before)
        entry = self.live.journal.add({'kind': 'move', 'ref': r['formId'], 'lines': lines, 'before': before,
                                       'undo': {'op': 'console_ref', 'ref': r['formId'], 'lines': undo_lines}
                                       if undo_lines else None})
        moved = None
        if before.get('position') and after.get('position') and before['cell'] == after['cell']:
            moved = round(sum((after['position'][a] - before['position'][a]) ** 2 for a in 'xyz') ** 0.5, 1)
        return {'ref': r['formId'], 'lines': outputs, 'before': before, 'after': after, 'distanceInCell': moved,
                'arrived': ready, 'journalId': entry['id'], 'undoable': bool(undo_lines)}

    def _place(self, r):
        v = self.live.values(['GetPos X', 'GetPos Y', 'GetPos Z', 'GetAngle Z', 'GetParentCell', 'IsInInterior',
                              'GetParentWorldspace'], r['ptr'])
        return {'position': {'x': v[0], 'y': v[1], 'z': v[2]} if v[0] is not None else None, 'heading': v[3],
                'cell': v[4], 'interior': bool(v[5]) if v[5] is not None else None, 'worldspace': v[6] or None}

    @staticmethod
    def _undo_place(before):
        p = before.get('position')
        if not p or not before.get('cell'):
            return None
        return ['MoveToCell %s %s %s %s' % (before['cell'], p['x'], p['y'], p['z']),
                'SetAngle Z %s' % (before.get('heading') or 0)]

    # ================================================================== fnv_spawn
    def spawn(self, form, near='player', count=1, distance=0, direction=0):
        self.live.in_game()
        if not 1 <= int(count) <= 20:
            raise ValueError('count is 1..20')
        base = self.live.resolve(form) if not hasattr(self.live, 'base_form') else self.live.base_form(form)
        anchor = self.live.resolve(near)
        self.live.changing('spawn %s' % form)
        made = []
        for _ in range(int(count)):
            result = self.link.eval('PlaceAtMe %s 1 %d %d' % (base['formId'] if base.get('formId') else base['expr'],
                                                              int(distance), int(direction)), anchor['ptr'])
            element = result.get('result') or {}
            if not result.get('ok') or element.get('type') != 'form' or not element.get('ptr'):
                raise LiveError('SPAWN_FAILED', 'PlaceAtMe did not return a reference', {'result': result, 'made': made})
            made.append({'ptr': int(element['ptr'], 16), 'formId': element.get('formId')})
        refs = []
        for m in made:
            entry = self.live.journal.add({'kind': 'spawn', 'base': base.get('formId'), 'ref': m['formId'],
                                           'near': anchor['formId'],
                                           'undo': {'op': 'console_ref', 'ref': m['formId'],
                                                    'lines': ['Disable', 'MarkForDelete']}})
            v = self.live.values(['GetPos X', 'GetPos Y', 'GetPos Z', 'GetDistance player', 'GetName'], m['ptr'])
            refs.append({'ref': m['formId'], 'ptr': '0x%08X' % m['ptr'], 'name': v[4],
                         'position': {'x': v[0], 'y': v[1], 'z': v[2]}, 'distanceToPlayer': v[3],
                         'journalId': entry['id']})
        return {'base': base.get('formId'), 'near': anchor['formId'], 'refs': refs, 'count': len(refs),
                'note': 'fnv_undo or fnv_revert_all disables and deletes the spawned refs.'}


    # ================================================================== fnv_condition_eval
    def _effect_list(self, form_ptr, offset):
        """MagicItem effect list: a BSSimpleList at +offset, or after a vtable there (EffectItemList); items are
        checked by their EffectSetting (form type 0x10) at EffectItem +0x14."""
        raw = self.live.reads([(form_ptr + offset, 8)])[0]
        if not raw:
            return []
        first = struct.unpack('<I', raw[:4])[0]
        for head in ((form_ptr + offset + 4) if 0x00F00000 <= first < 0x01200000 else None, form_ptr + offset):
            if head is None:
                continue
            items = self.live.walk_list(head, 64)
            if not items:
                continue
            settings = self.live.u32s([i + 0x14 for i in items])
            heads = self.live.form_ids([s or 0 for s in settings])
            if all(h and h['formType'] == 0x10 for h in heads):
                return list(zip(items, heads))
        return []

    def _condition_lists(self, ptr, form_type, subject, target, tab_subjects):
        """[(label, list address, subject ptr, target ptr, extra)] for a form, by type (decomp layouts)."""
        lists = []
        if form_type == 0x46:    # INFO: TESTopicInfo +0x18
            lists.append(('response conditions', ptr + 0x18, subject, target, {}))
        elif form_type == 0x49:  # PACK: TESPackage +0x40
            lists.append(('package conditions', ptr + 0x40, subject, target, {}))
        elif form_type == 0x48:  # IDLE: TESIdleForm +0x30
            lists.append(('idle conditions', ptr + 0x30, subject, target, {}))
        elif form_type == 0x6A:  # RCPE: TESRecipe +0x34
            lists.append(('recipe conditions', ptr + 0x34, subject, target, {}))
        elif form_type == 0x5C:  # CPTH: BGSCameraPath +0x18
            lists.append(('camera path conditions', ptr + 0x18, subject, target, {}))
        elif form_type == 0x62:  # MESG: buttons at +0x30, each with conditions at +0x08
            for k, button in enumerate(self.live.walk_list(ptr + 0x30, 20)):
                text = self.live.u32s([button])[0]
                label = self.social._texts([text])[0] if text and self.social else None
                lists.append(('button %d%s' % (k, ' "%s"' % label if label else ''), button + 0x08, subject, target,
                              {'button': k, 'text': label}))
        elif form_type == 0x56:  # PERK: requirements +0x40, entries +0x48
            lists.append(('perk requirements', ptr + 0x40, subject, target, {}))
            entries = self.live.walk_list(ptr + 0x48, 64)
            classes = self.live.rtti(entries) if entries else {}
            raws = self.live.reads([(e, 0x14) for e in entries]) if entries else []
            for k, (entry, raw) in enumerate(zip(entries, raws)):
                cls = (classes.get(entry) or {}).get('class')
                if not raw or cls != 'BGSEntryPointPerkEntry':
                    continue
                point, function, tabs = raw[8], raw[9], raw[0x0A]
                array, = struct.unpack_from('<I', raw, 0x10)
                if not array or not 0 < tabs <= 4:
                    continue
                for tab in range(tabs):
                    who = (tab_subjects[tab] if tab_subjects and tab < len(tab_subjects) else None)
                    who_ptr = subject if tab == 0 else (self.live.resolve(who)['ptr'] if who else 0)
                    lists.append(('entry %d (entry point %d) tab %d' % (k, point, tab), array + 8 * tab, who_ptr, 0,
                                  {'entry': k, 'entryPoint': point, 'function': function, 'tab': tab,
                                   'tabSubject': 'perk owner' if tab == 0 else (who or 'not given (tab_subjects)')}))
        elif form_type in (0x13, 0x14, 0x2F, 0x1D):  # ENCH, SPEL, ALCH, INGR: effect items, conditions at +0x1C
            offset = 0x24 if form_type in (0x13, 0x14) else 0x3C
            for k, (item, setting) in enumerate(self._effect_list(ptr, offset)):
                lists.append(('effect %d (%s)' % (k, setting['formId']), item + 0x1C, subject, target,
                              {'effect': k, 'effectSetting': setting['formId']}))
        elif form_type == 0x47:  # QUST: quest +0x54, stage log entries, objective targets
            lists.append(('quest conditions (gate its dialogue)', ptr + 0x54, subject, target, {}))
            stages = self.live.walk_list(ptr + 0x44, 300)
            raws = self.live.reads([(s, 0xC) for s in stages]) if stages else []
            for stage, raw in zip(stages, raws):
                if not raw:
                    continue
                for n, entry in enumerate(self.live.walk_list(stage + 4, 50)):
                    lists.append(('stage %d log entry %d' % (raw[0], n), entry + 4, subject, 0,
                                  {'stage': raw[0], 'logEntry': n}))
            for obj in self.live.walk_list(ptr + 0x4C, 100):
                index = self.live.u32s([obj + 4])[0]
                for n, tgt in enumerate(self.live.walk_list(obj + 0x14, 20)):
                    ref = self.live.u32s([tgt + 0x0C])[0] or 0
                    lists.append(('objective %s target %d' % (index, n), tgt + 4, ref, 0,
                                  {'objective': index, 'target': n, 'subject': 'the target ref'}))
        else:
            raise LiveError('NO_CONDITIONS', 'Form type 0x%02X has no condition lists this tool knows (INFO, PACK, PERK, '
                            'SPEL, ENCH, ALCH, INGR, QUST, RCPE, IDLE, MESG, CPTH)' % form_type)
        return lists

    def condition_eval(self, form, subject='player', target=None, tab_subjects=None, only_failed=False):
        self.live.in_game()
        f = self.live.resolve(form)
        head = self.live.form_ids([f['ptr']])[0]
        subject_ptr = self.live.resolve(subject)['ptr'] if subject else 0
        target_ptr = self.live.resolve(target)['ptr'] if target else (
            self.live.resolve('player')['ptr'] if head['formType'] == 0x46 else 0)
        lists = self._condition_lists(f['ptr'], head['formType'], subject_ptr, target_ptr, tab_subjects)
        items = self.social.read_condition_lists([a for _, a, _, _, _ in lists])
        results = self.social.evaluate_jobs([(it, s, t) for it, (_, _, s, t, _) in zip(items, lists)])
        subjects = self.describe_pointers({s for _, _, s, _, _ in lists if s} | {t for _, _, _, t, _ in lists if t})
        out = []
        for (label, _, s, t, extra), result, its in zip(lists, results, items):
            entry = {'list': label, **extra, 'subject': (subjects.get(s) or {}).get('formId') if s else None,
                     'target': (subjects.get(t) or {}).get('formId') if t else None, 'count': len(its),
                     'passes': result['result'] if its else True,
                     'failing': [c['index'] for c in result['conditions'] if not c.get('passed')],
                     'conditions': result['conditions']}
            if only_failed and entry['passes']:
                continue
            out.append(entry)
        return {'form': f['formId'], 'formType': head['formType'], 'lists': out, 'listCount': len(lists),
                'failedLists': [e['list'] for e in out if not e['passes']],
                'note': 'Each condition is run with the game\'s own condition function (command table), combined like '
                        'TESConditionItem::ContainsTrait: OR groups, ANDed. Subjects: the record\'s own rule '
                        '(perk tab 0 = perk owner, quest targets = the target ref, the rest = subject).',
                'uses': ['decomp layouts of each record\'s condition lists', 'command table condition handlers']}

    # ================================================================== fnv_quest_targets
    def quest_targets(self, quest):
        self.live.in_game()
        q = self.live.resolve(quest)
        head = self.live.form_ids([q['ptr']])[0]
        if not head or head['formType'] != 0x47:
            raise LiveError('NOT_A_QUEST', '%s is not a quest' % quest)
        player = self.live.resolve('player')['ptr']
        raw = self.live.reads([(q['ptr'], 0x64), (player + 0x6B8, 4), (player + 0x206, 1)])
        qraw, active_raw, dirty_raw = raw
        flags, stage = qraw[0x3C], qraw[0x60]
        active = struct.unpack('<I', active_raw)[0] if active_raw else 0
        tracked = set(self.live.walk_list(player + 0x6BC, 200))
        cached = set(self.live.walk_list(player + 0x6C4, 200))
        quest_state = {'running': bool(flags & 1), 'completed': bool(flags & 2), 'failed': bool(flags & 0x40),
                       'stage': stage, 'activeQuest': active == q['ptr'],
                       'playerActiveQuest': (self.live.form_ids([active])[0] or {}).get('formId') if active else None}
        objectives = []
        objs = self.live.walk_list(q['ptr'] + 0x4C, 100)
        oraws = self.live.reads([(o, 0x24) for o in objs]) if objs else []
        texts = self.social._texts([struct.unpack_from('<I', r, 8)[0] if r else 0 for r in oraws])
        targets_all = []
        for obj, oraw, text in zip(objs, oraws, texts):
            if not oraw:
                continue
            index, state = struct.unpack_from('<I', oraw, 4)[0], struct.unpack_from('<I', oraw, 0x20)[0]
            tptrs = self.live.walk_list(obj + 0x14, 20)
            objectives.append({'ptr': obj, 'index': index, 'text': text, 'state': state, 'displayed': bool(state & 1),
                               'completed': state > 1, 'tracked': obj in tracked, 'targets': tptrs})
            targets_all += [(obj, t) for t in tptrs]
        traws = dict(zip([t for _, t in targets_all], self.live.reads([(t, 0x30) for _, t in targets_all]))) \
            if targets_all else {}
        refs = {t: struct.unpack_from('<I', r, 0x0C)[0] for t, r in traws.items() if r}
        doors = {}
        for t, r in traws.items():
            if r:
                data, size = struct.unpack_from('<II', r, 0x24)
                if data and 0 < size < 64:
                    doors[t] = self.live.u32s([data])[0]
        described = self.describe_pointers(set(refs.values()) | set(doors.values()))
        lists = self.social.read_condition_lists([t + 4 for _, t in targets_all]) if targets_all else []
        evaluated = self.social.evaluate_jobs([(items, refs.get(t, 0), 0) for items, (_, t) in zip(lists, targets_all)]) \
            if targets_all else []
        ref_state = {}
        rptrs = [p for p in set(refs.values()) if p]
        if rptrs:
            ops = [('eval', hexs(e), word(p)) for p in rptrs for e in ('GetDisabled', 'GetParentCell', 'GetPos X',
                                                                       'GetPos Y', 'GetPos Z', 'GetDistance player')]
            values = [fnv_link.value_of(r) for r in self.link.batch(ops)]
            for k, p in enumerate(rptrs):
                v = values[6 * k:6 * k + 6]
                ref_state[p] = {'disabled': bool(v[0]), 'cell': v[1], 'position': {'x': v[2], 'y': v[3], 'z': v[4]},
                                'distanceToPlayer': v[5]}
        by_target = {t: (items, result) for (_, t), items, result in zip(targets_all, lists, evaluated)}
        for o in objectives:
            out_targets = []
            for n, t in enumerate(o.pop('targets')):
                ref = refs.get(t, 0)
                items, result = by_target.get(t, ([], {'conditions': [], 'result': True}))
                passes = result['result'] if items else True
                why = []
                if not quest_state['running']:
                    why.append('the quest is not running')
                if quest_state['completed']:
                    why.append('the quest is completed')
                if o['state'] != 1:
                    why.append('the objective is %s (markers need state exactly displayed)' %
                               {0: 'not displayed', 2: 'completed', 3: 'completed'}.get(o['state'], o['state']))
                if not o['tracked']:
                    why.append('the objective is not in the player\'s objective list')
                if not quest_state['activeQuest']:
                    why.append('the quest is not the player\'s active quest (the compass shows the active quest\'s '
                               'targets; SetActiveQuest)')
                if not ref:
                    why.append('the target has no reference')
                else:
                    flags_ref = (self.live.form_ids([ref])[0] or {}).get('flags', 0)
                    if flags_ref & 0x20:
                        why.append('the target reference is deleted')
                    if (ref_state.get(ref) or {}).get('disabled'):
                        why.append('the target reference is disabled')
                if not passes:
                    why.append('target conditions fail: %s' % ', '.join(
                        c['function'] for c in result['conditions'] if not c.get('passed')))
                out_targets.append({'target': n, 'ref': described.get(ref, {'formId': None}) if ref else None,
                                    'refState': ref_state.get(ref), 'conditionsPass': passes,
                                    'conditions': result['conditions'],
                                    'markerPointsAt': (described.get(doors[t]) or {'ptr': '0x%08X' % doors[t]})
                                    if doors.get(t) else ('the target' if ref else None),
                                    'viaDoor': bool(doors.get(t)), 'inPlayerTargetCache': t in cached,
                                    'shown': not why, 'whyNotShown': why or None})
            o['targets'] = out_targets
            o.pop('ptr')
        return {'quest': q['formId'], **quest_state, 'playerTargetCacheDirty': bool(dirty_raw and dirty_raw[0]),
                'objectives': objectives,
                'note': 'A marker shows when the quest runs and is the active quest, the objective is displayed and not '
                        'completed and in the player\'s list, the target ref exists and its conditions pass (run on the '
                        'target ref). Targets in other cells point at the first door of the path the game built.',
                'uses': ['TESQuest / BGSQuestObjective / target layouts (decomp)',
                         'PlayerCharacter active quest +0x6B8, objectives +0x6BC, target cache +0x6C4 (decomp)',
                         'target conditions run like AddMatchingTargetsToList (subject = target ref)']}

    # ================================================================== fnv_script_instances
    PROCESS_SCRIPTS = 0x0118C685   # byte read by Script::GetProcessScripts (0x005AC740, proven)
    DATA_HANDLER = 0x011C3F2C      # TESDataHandler* (xNVSE GameData.cpp g_dataHandler); checked by its quest list

    def script_instances(self, of=None, script=None, include_variables=True, limit=60):
        self.live.in_game()
        processing = self.live.reads([(self.PROCESS_SCRIPTS, 1)])[0]
        out = {'scriptsRunning': bool(processing[0]) if processing else None}
        if of:
            r = self.live.resolve(of)
            head = self.live.form_ids([r['ptr']])[0]
            kind = 'quest' if head and head['formType'] == 0x47 else 'ref'
            instance = self._instance(r['ptr'], kind, include_variables)
            effects = []
            if kind == 'ref' and self.live.rtti([r['ptr']])[r['ptr']]['class'] in ('Character', 'Creature', 'PlayerCharacter', 'Actor'):
                try:
                    for e in self.live.effects(of)['effects']:
                        if 'Script' in (e.get('class') or '') and e.get('ptr'):
                            raw = self.live.reads([(int(e['ptr'], 16) + 0x48, 8)])[0]
                            if raw:
                                sp, lp = struct.unpack('<II', raw)
                                effects.append({'effect': e.get('effect') or e.get('source'), 'script':
                                                (self.live.form_ids([sp])[0] or {}).get('formId'),
                                                'locals': '0x%08X' % lp if lp else None})
                except Exception:
                    pass
            out.update(instance=instance, effectScripts=effects)
            return out
        dh = self.live.u32s([self.DATA_HANDLER])[0]
        quests = self.live.walk_list(dh + 0x118, 4000) if dh else []
        sample = self.live.rtti(quests[:3]) if quests else {}
        if not quests or any((v or {}).get('class') != 'TESQuest' for v in sample.values()):
            # TESDataHandler has no vtable: it is checked through its quest list instead.
            raise LiveError('NO_DATA_HANDLER', 'The data handler quest list is not where xNVSE and the decomp say',
                            {'sample': sample})
        raws = self.live.reads([(p, 0x64) for p in quests])
        running = []
        for p, raw in zip(quests, raws):
            if not raw:
                continue
            flags, sp, lp = raw[0x3C], struct.unpack_from('<I', raw, 0x1C)[0], struct.unpack_from('<I', raw, 0x5C)[0]
            if flags & 1 and sp:
                delay = struct.unpack_from('<f', raw, 0x40)[0]
                running.append((p, sp, lp, delay))
        heads = self.live.form_ids([p for p, _, _, _ in running] + [s for _, s, _, _ in running])
        quest_heads, script_heads = heads[:len(running)], heads[len(running):]
        names = self.live.names([h['formId'] for h in quest_heads if h])
        quest_out = []
        for (p, sp, lp, delay), qh, sh in zip(running, quest_heads, script_heads):
            sid = sh['formId'] if sh else None
            if script and script.upper().removeprefix('0X').zfill(8) != sid and script.lower() not in \
                    (names.get(qh['formId'] if qh else '') or '').lower():
                continue
            quest_out.append({'quest': qh['formId'] if qh else None, 'name': names.get(qh['formId']) if qh else None,
                              'script': sid, 'hasLocals': bool(lp), 'delaySeconds': round(delay, 3) if delay else None})
        out['questScripts'] = {'running': len(running), 'shown': quest_out[:limit], 'truncated': len(quest_out) > limit}
        # Object scripts: the scripted-ref list of each loaded cell, the lists TES::RunScripts walks (the interior,
        # else the attached grid cells): TESObjectCELL loaded data +0xC4, scripted refs at +0x4C.
        cells = self.loaded_cells()
        datas = self.live.u32s([c + 0xC4 for c in cells]) if cells else []
        scripted = []
        for data_ptr in datas:
            if data_ptr:
                scripted += self.live.walk_list(data_ptr + 0x4C, 2000)
        counts = Counter()
        refs_out = []
        for p in scripted[:300]:
            inst = self._instance(p, 'ref', include_variables and len(refs_out) < limit)
            if not inst.get('script'):
                continue
            if script and script.upper().removeprefix('0X').zfill(8) != inst['script']:
                continue
            counts[inst['script']] += 1
            if len(refs_out) < limit:
                refs_out.append(inst)
        out['objectScripts'] = {'loadedCells': len(cells), 'scriptedRefs': len(scripted), 'shown': refs_out,
                                'instancesPerScript': [{'script': s, 'instances': n} for s, n in counts.most_common(20)]}
        out['note'] = ('scriptsRunning=false means the game runs no scripts at all (Script::GetProcessScripts). Object '
                       'scripts run only on refs in loaded, attached cells; many instances of one script can mean '
                       'spawned refs that are never deleted.')
        out['uses'] = ['TES::RunScripts walk (decomp): quest list TESDataHandler+0x118, enabled bit, quest script locals',
                       'ExtraScript (type 0x0D) script + locals', 'Script variable list for names']
        return out

    TES = 0x011DEA10               # TES* (JIP LN GameData.h TES::GetSingleton): grid cells +0x08, interior +0x34

    def loaded_cells(self):
        tes = self.live.u32s([self.TES])[0]
        if not tes:
            return []
        grid, interior = self.live.u32s([tes + 0x08, tes + 0x34])
        if interior:
            return [interior]
        if not grid:
            return []
        size, cells_ptr = self.live.u32s([grid + 0x0C, grid + 0x10])
        if not cells_ptr or not 0 < (size or 0) <= 11:
            return []
        raw = self.live.reads([(cells_ptr, 4 * size * size)])[0] or b''
        return [c for c in struct.unpack('<%dI' % (len(raw) // 4), raw) if c]

    def _instance(self, ptr, kind, include_variables):
        head = self.live.form_ids([ptr])[0]
        out = {'formId': head['formId'] if head else None, 'kind': kind}
        if kind == 'ref':
            v = self.live.values(['GetName', 'GetBaseObject', 'GetParentCell', 'GetDisabled'], ptr)
            out.update(name=v[0], base=v[1], cell=v[2], disabled=bool(v[3]))
        vars_ = self.live._variables(ptr, kind)
        out['script'] = vars_.get('script')
        if include_variables:
            out['variables'] = vars_.get('variables')
        return out


    # ================================================================== fnv_menu / fnv_menu_control
    INTERFACE_MANAGER = 0x011D8A80   # InterfaceManager* (read by InterfaceManager::Instance 0x004B7210, proven)
    MENU_VISIBLE = 0x011F308F        # visibility byte per menu id (IsMenuVisible 0x007027B0, probable)
    MESSAGE_MENU = 0x011DA4F0        # MessageMenu* (MessageMenu::Close 0x007A8DF0, proven, reads it)
    MESSAGE_CLICK = 0x007AA070       # MessageMenu::HandleTileSelection(int id, Tile *) (class_proven)
    MESSAGE_CLOSE = 0x007A8DF0       # MessageMenu::Close() cdecl (proven)
    TRAIT_STRING = 0xFC4

    def _tile_values(self, tiles):
        """{tile: {trait id: (number, string ptr)}} for many tiles (Tile values BSSimpleArray at +0x10)."""
        tiles = [t for t in tiles if t]
        heads = dict(zip(tiles, self.live.reads([(t + 0x14, 8) for t in tiles]))) if tiles else {}
        arrays = {t: struct.unpack('<II', h) for t, h in heads.items() if h}
        spans = [(t, data, min(size, 120)) for t, (data, size) in arrays.items() if data and size]
        ptr_lists = dict(zip([t for t, _, _ in spans], self.live.reads([(d, 4 * n) for _, d, n in spans]))) if spans else {}
        values = {t: [p for p in struct.unpack('<%dI' % (len(raw) // 4), raw) if p] for t, raw in ptr_lists.items() if raw}
        flat = [(t, v) for t, vs in values.items() for v in vs]
        raws = self.live.reads([(v, 0x10) for _, v in flat]) if flat else []
        out = {t: {} for t in tiles}
        for (t, _), raw in zip(flat, raws):
            if raw:
                trait, _, num, string = struct.unpack('<IIfI', raw)
                out[t][trait] = (num, string)
        return out

    def _tile_strings(self, tiles, trait=TRAIT_STRING):
        vals = self._tile_values(tiles)
        ptrs = [vals.get(t, {}).get(trait, (None, 0))[1] for t in tiles]
        return dict(zip(tiles, self.social._texts(ptrs)))

    def _children(self, tile):
        out, node, seen = [], self.live.u32s([tile + 4])[0], set()
        while node and node not in seen and len(out) < 64:
            seen.add(node)
            raw = self.live.reads([(node, 12)])[0]
            if not raw:
                break
            nxt, _, child = struct.unpack('<III', raw)
            if child:
                out.append(child)
            node = nxt
        return out

    def _tile_name(self, tiles):
        ptrs = self.live.u32s([t + 0x20 for t in tiles]) if tiles else []
        return dict(zip(tiles, self.social._texts(ptrs, 120)))

    def message_box(self):
        """The open message box: title, text, buttons with their labels and tiles; None when there is none."""
        menu = self.live.u32s([self.MESSAGE_MENU])[0]
        if not menu:
            return None
        raw = self.live.reads([(menu, 0x94)])[0]
        if not raw:
            return None
        tiles = [t for t in struct.unpack_from('<6I', raw, 0x28) if t]
        strings = self._tile_strings(tiles)
        by_name = {n: t for t, n in self._tile_name(tiles).items() if n}
        items = self.live.walk_list(menu + 0x44, 20)
        pairs = self.live.reads([(i, 8) for i in items]) if items else []
        buttons = []
        for k, pair in enumerate(pairs):
            if not pair:
                continue
            tile, value = struct.unpack('<Ii', pair)
            label = None
            if tile:
                names = self._tile_name(self._children(tile))
                text_tiles = [c for c, n in names.items() if n == 'ListItemText']
                label = self._tile_strings(text_tiles).get(text_tiles[0]) if text_tiles else None
                if label is None:
                    label = self._tile_strings([tile]).get(tile)
            buttons.append({'index': k, 'label': label, 'value': value, 'tile': tile})
        return {'menu': menu, 'title': strings.get(by_name.get('MM_Title')), 'text': strings.get(by_name.get('MM_MessageText')),
                'buttons': buttons,
                'queued': raw[0x90] if len(raw) > 0x90 else None}

    def menus(self):
        self.live.ready()
        im = self.live.u32s([self.INTERFACE_MANAGER])[0]
        if not im:
            raise LiveError('NO_INTERFACE', 'The interface manager does not exist yet')
        raw = self.live.reads([(im, 0x140), (self.MENU_VISIBLE + 1001, 90)])
        imraw, visible = raw
        mode, = struct.unpack_from('<I', imraw, 0x0C)
        active, = struct.unpack_from('<I', imraw, 0xD0)
        stack = [s for s in struct.unpack_from('<10I', imraw, 0x114) if s]
        button = imraw[0xE4]
        active_id = self.live.u32s([active + 0x20])[0] if active else None
        open_menus = [{'id': 1001 + k, 'name': MENU_NAMES.get(1001 + k, 'menu %d' % (1001 + k))}
                      for k, b in enumerate(visible or b'') if b]
        box = self.message_box() if any(m['id'] == 1001 for m in open_menus) or active_id == 1001 else None
        if box:
            for b in box['buttons']:
                b.pop('tile', None)
            box.pop('menu', None)
        loading = any(m['id'] == 1007 for m in open_menus)
        return {'mode': {1: 'game', 2: 'menu', 3: 'entering menu', 4: 'leaving menu'}.get(mode, mode),
                'inMenuMode': mode > 1, 'activeMenu': {'id': active_id, 'name': MENU_NAMES.get(active_id)} if active_id else None,
                'menuStack': [{'id': s, 'name': MENU_NAMES.get(s, 'pseudo id %d' % s)} for s in stack],
                'visible': open_menus, 'loading': loading, 'messageBox': box,
                'lastMessageButton': None if button == 0xFF else button,
                'uses': ['InterfaceManager (0x011D8A80, Instance proven): mode +0x0C, active menu +0xD0, stack +0x114',
                         'menu visibility bytes 0x011F308F[id]', 'MessageMenu tiles +0x28, button ListBox +0x40',
                         'Tile values (trait 0xFC4 = string)']}

    def menu_control(self, action, button=None, menu=None):
        """action: press (a message box button by index or label) | close (the active or a named menu) | close_all."""
        self.live.ready()
        if action == 'press':
            box = self.message_box()
            if not box or not box['buttons']:
                raise LiveError('NO_MESSAGE_BOX', 'No message box with buttons is open', {'menus': self.menus()})
            if button is None:
                button = 0
            if isinstance(button, int) or (isinstance(button, str) and button.isdigit()):
                k = int(button)
                pick = box['buttons'][k] if 0 <= k < len(box['buttons']) else None
            else:
                want = str(button).lower()
                pick = next((b for b in box['buttons'] if b['label'] and b['label'].lower() == want), None) or \
                    next((b for b in box['buttons'] if b['label'] and want in b['label'].lower()), None)
            if not pick:
                raise LiveError('NO_SUCH_BUTTON', 'No button %r' % button,
                                {'buttons': [{k: v for k, v in b.items() if k != 'tile'} for b in box['buttons']]})
            self.live.changing('message box button')
            result = self.link.one('call', word(self.MESSAGE_CLICK), 'v', word(box['menu']), '0', '7', word(pick['tile']))
            if not result.get('ok'):
                raise LiveError('CALL_REJECTED', 'The click call failed: %s' % result.get('error'), result)
            self.live.journal.add({'kind': 'menu_press', 'button': pick['index'], 'label': pick['label'], 'undo': None})
            time.sleep(0.5)
            after = self.message_box()
            if after and after['text'] == box['text'] and after['buttons'] == box['buttons']:
                time.sleep(0.5)  # still fading out
                after = self.message_box()
            return {'pressed': {k: v for k, v in pick.items() if k != 'tile'}, 'title': box['title'], 'text': box['text'],
                    'next': {'title': after['title'], 'text': after['text'],
                             'buttons': [b['label'] for b in after['buttons']]} if after and after['buttons'] else None,
                    'note': 'Pressed through MessageMenu::HandleTileSelection (the call a click makes): the script\'s '
                            'GetButtonPressed returns the button\'s value on its next run.'}
        if action == 'close':
            state = self.menus()
            # The active-menu field is empty for message boxes: the top of the menu stack is the open menu.
            top = (state['activeMenu'] or {}).get('id') or next(
                (m['id'] for m in reversed(state['menuStack']) if m['id'] >= 1001), None)
            target = menu or top
            if isinstance(target, str) and not target.isdigit():
                target = next((i for i, n in MENU_NAMES.items() if n.lower() == target.lower().replace(' ', '')), None)
            target = int(target) if target else None
            if not target:
                return {'closed': None, 'note': 'No menu is open', 'changed': False}
            self.live.changing('close menu %s' % target)
            if target == 1001:
                result = self.link.one('call', word(self.MESSAGE_CLOSE), 'v', '0', '0')
                how = 'MessageMenu::Close (proven)'
            elif target == top:
                result = self.live.console('CloseActiveMenu')
                how = 'JIP CloseActiveMenu'
            else:
                result = self.live.console('CloseAllMenus')
                how = 'CloseAllMenus (the menu was not on top)'
            time.sleep(0.4)
            return {'closed': {'id': target, 'name': MENU_NAMES.get(target)}, 'how': how,
                    'menusAfter': [m['name'] for m in self.menus()['visible']]}
        if action == 'close_all':
            self.live.changing('close all menus')
            self.live.console('CloseAllMenus')
            time.sleep(0.4)
            return {'menusAfter': [m['name'] for m in self.menus()['visible']], 'how': 'CloseAllMenus (proven handler)'}
        raise ValueError('action is press, close or close_all')


    # ================================================================== fnv_rng
    RNG_INSTANCE_FN = 0x00476C00   # BSRandom::QInstance() cdecl -> the one shared MT19937 (proven)
    CALENDAR = 0x011DE7B8          # Year, Month, Day, Hour, DaysPassed, TimeScale TESGlobal* (+0x1C dirty byte)
    WAIT_ONE_HOUR = 0x0094DF80     # one hour of Wait on the player (unnamed: catch-up of process lists + calendar)
    FORM_HEAP_ALLOCATE = 0x00401000  # FormHeap_Allocate (proven)
    LEVELED_LIST_OF = 0x00487BE0   # TESLeveledList::GetFormAsLeveledList(form) cdecl (proven)
    LEVELED_ROLL = 0x00487D00      # TESLeveledList::CalculateCurrentForm (proven) thiscall, ret 0x18

    def _rng_instance(self):
        result = self.link.one('call', word(self.RNG_INSTANCE_FN), 'i', '0', '0')
        if not result.get('ok'):
            raise LiveError('CALL_REJECTED', 'BSRandom::QInstance failed: %s' % result.get('error'), result)
        return int(result['eax'], 16)

    @staticmethod
    def _seed_state(seed):
        """MersenneTwister::Seed's end state: mt[0] = seed, mt[i] = mt[i-1] * 69069, mti = 624."""
        mt = [seed & 0xFFFFFFFF]
        for _ in range(623):
            mt.append((mt[-1] * 69069) & 0xFFFFFFFF)
        return struct.pack('<624I', *mt), struct.pack('<I', 0x270)

    def _seed_ops(self, instance, seed):
        state, mti = self._seed_state(seed)
        ops = []
        for k in range(0, len(state), 1024):  # 1 KB per write op
            ops.append(('wr', word(instance + 8 + k), state[k:k + 1024].hex()))
        ops.append(('wr', word(instance + 0x9C8), mti.hex()))
        return ops

    def rng(self, seed=None, action=None):
        """action: state (default without seed) | seed."""
        self.live.ready()
        instance = self._rng_instance()
        raw = self.live.reads([(instance, 8), (instance + 0x9C8, 4)])
        head, mti_raw = raw
        mti = struct.unpack('<I', mti_raw)[0] if mti_raw else None
        state = {'instance': '0x%08X' % instance, 'index': mti, 'seededYet': mti != 0x271,
                 'lastValue': struct.unpack_from('<I', head, 4)[0] if head else None}
        if seed is None and action != 'seed':
            return {**state, 'changed': False,
                    'note': 'One Mersenne Twister (BSRandom) serves leveled lists, VATS, combat, AI, lockpicking, '
                            'hacking and GetRandomPercent, on every thread.'}
        if seed is None:
            raise ValueError('seed is required')
        self.live.changing('rng seed')
        old = self.live.reads([(instance + 8, 624 * 4)])[0]
        results = self.link.batch(self._seed_ops(instance, int(seed)))
        if not all(r.get('ok') for r in results):
            raise LiveError('WRITE_FAILED', 'Could not write the RNG state', {'results': results})
        entry = self.live.journal.add({'kind': 'rng_seed', 'seed': int(seed), 'undo': {
            'op': 'write', 'address': '0x%08X' % (instance + 8), 'hex': old.hex()} if old else None})
        return {**state, 'seeded': int(seed), 'journalId': entry['id'],
                'note': 'Seeded like MersenneTwister::Seed (state written directly). Rolls made in the same request are '
                        'repeatable (fnv_leveled_roll does that). Across frames other systems keep drawing numbers, so '
                        'a seed at test start narrows but does not fix later rolls.'}

    # ================================================================== fnv_time
    def calendar(self):
        pointers = self.live.u32s([self.CALENDAR + 4 * k for k in range(6)])
        raws = self.live.reads([(p + 0x24, 4) for p in pointers if p])
        values = iter(struct.unpack('<f', r)[0] if r else None for r in raws)
        names = ('year', 'month', 'day', 'hour', 'daysPassed', 'timeScale')
        out = {n: (round(next(values), 4) if p else None) for n, p in zip(names, pointers)}
        out['month'] = int(out['month']) + 1 if out['month'] is not None else None   # GameMonth is 0-based
        return out

    def time_control(self, set_hour=None, advance_hours=None, timescale=None, mode='globals',
                     evaluate_packages=True, allow_unproven=False):
        self.live.in_game()
        before = self.calendar()
        changes = []
        self.live.changing('time')
        if timescale is not None:
            result = self.live.script_set('TimeScale', 'global', float(timescale))
            changes.append({'timeScale': float(timescale), 'journalId': result['journalId']})
        if set_hour is not None:
            hour = float(set_hour)
            if not 0 <= hour < 24:
                raise ValueError('set_hour is 0..24')
            delta = (hour - before['hour']) % 24
            advance_hours = (advance_hours or 0) + delta
        if advance_hours:
            hours = float(advance_hours)
            if not 0 < hours <= 24 * 30:
                raise ValueError('advance_hours is 0..720')
            if mode == 'engine_wait':
                changes.append(self._engine_wait(hours, allow_unproven))
            else:
                changes.append(self._advance_globals(before, hours))
        if evaluate_packages and (advance_hours or set_hour is not None):
            refs = self.live.refs_near(radius=8000.0, form_type=200, limit=200)['refs']  # 200: actors
            actors = [r for r in refs if not r['dead']]
            ptrs = []
            for r in actors[:80]:
                try:
                    ptrs.append(self.live.resolve(r['formId'])['ptr'])
                except Exception:
                    pass
            self.link.batch([('eval', hexs('EvaluatePackage'), word(p)) for p in ptrs]) if ptrs else None
            changes.append({'evaluatedPackages': len(ptrs)})
        return {'before': before, 'after': self.calendar(), 'changes': changes,
                'note': 'mode=globals moves the clock (GameHour, GameDaysPassed, day/month/year) and re-evaluates nearby '
                        'packages: schedules switch, NPCs then walk there in real time. mode=engine_wait runs the '
                        'game\'s own hour of waiting (process-list catch-up, healing, hardcore needs) per hour. Cell '
                        'respawns need the cell to reload. Time only moves forward: load a test save to go back.'}

    def _advance_globals(self, before, hours):
        """GameHour/GameDaysPassed forward, with day rollover into GameDay/Month/Year (days per month as the game)."""
        days_in = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        hour = before['hour'] + hours
        day, month, year = before['day'], before['month'] - 1, before['year']
        while hour >= 24:
            hour -= 24
            day += 1
            if day > days_in[int(month) % 12]:
                day = 1
                month += 1
                if month > 11:
                    month = 0
                    year += 1
        lines = ['set GameHour to %.4f' % hour, 'set GameDaysPassed to %.4f' % (before['daysPassed'] + hours / 24.0),
                 'set GameDay to %d' % day, 'set GameMonth to %d' % month, 'set GameYear to %d' % year]
        old = ['set GameHour to %.4f' % before['hour'], 'set GameDaysPassed to %.4f' % before['daysPassed'],
               'set GameDay to %d' % before['day'], 'set GameMonth to %d' % (before['month'] - 1),
               'set GameYear to %d' % before['year']]
        for line in lines:
            self.live.console(line)
        dirty = self.live.reads([(self.CALENDAR + 0x1C, 1)])[0]
        self.link.write(self.CALENDAR + 0x1C, b'\x01')  # the calendar resyncs its day counter
        # Not undoable on purpose: the game never runs its clock backwards, and rewinding it hands AI schedules and
        # low-process patrols negative time.
        entry = self.live.journal.add({'kind': 'time', 'hours': hours, 'lines': lines, 'previous': old, 'undo': None})
        return {'advancedHours': hours, 'how': 'globals', 'lines': lines, 'journalId': entry['id'],
                'dirtyFlagWas': dirty[0] if dirty else None,
                'undo': 'none: game time only moves forward (load a test save to go back)'}

    def _engine_wait(self, hours, allow_unproven):
        if not allow_unproven:
            raise LiveError('UNPROVEN_FUNCTION', 'engine_wait calls 0x0094DF80 (one hour of Wait: process-list catch-up '
                            'and Calendar::Update), which has no proven name yet: pass allow_unproven=true')
        raw = self.live.reads([(self.WAIT_ONE_HOUR, 16)])[0]
        clean = self.deep._clean_bytes(self.WAIT_ONE_HOUR, 16)
        if raw is None or (clean is not None and raw != clean):
            raise LiveError('PATCHED_ELSEWHERE', 'The wait function is not the expected code')
        player = self.live.resolve('player')['ptr']
        counter_raw = self.live.reads([(player + 0x654, 4)])[0]
        counter = struct.unpack('<i', counter_raw)[0] if counter_raw else 0
        n = int(round(hours))
        # The function counts player+0x654 (sleep/wait hours) down and full-heals when it reaches 0: keep it above.
        ops = [('wr', word(player + 0x654), struct.pack('<i', n + 2).hex())]
        ops += [('call', word(self.WAIT_ONE_HOUR), 'v', word(player), '0') for _ in range(n)]
        ops.append(('wr', word(player + 0x654), struct.pack('<i', counter).hex()))
        results = []
        for chunk in _chunks(ops, 26):
            results += self.link.batch(chunk, timeout=60)
        failed = [r for r in results if not r.get('ok')]
        entry = self.live.journal.add({'kind': 'time', 'hours': n, 'how': 'engine_wait', 'undo': None})
        return {'advancedHours': n, 'how': 'engine_wait (0x0094DF80 x %d)' % n, 'failedOps': len(failed),
                'journalId': entry['id']}

    # ================================================================== fnv_leveled_roll
    def _buffer(self, size=0x1000):
        pid = (self.game.process() or [None])[0]
        cached = getattr(self, '_roll_buffer', None)
        if cached and cached[0] == pid:
            return cached[1]
        result = self.link.one('call', word(self.FORM_HEAP_ALLOCATE), 'i', '0', '0', word(size))
        address = int(result['eax'], 16) if result.get('ok') else 0
        if not address:
            raise LiveError('NO_MEMORY', 'Could not allocate a buffer in game memory')
        self._roll_buffer = (pid, address)
        return address

    def leveled_roll(self, form, level=None, rolls=200, seed=None):
        self.live.in_game()
        if not 1 <= int(rolls) <= 5000:
            raise ValueError('rolls is 1..5000')
        f = self.live.resolve(form)
        lookup = self.link.one('call', word(self.LEVELED_LIST_OF), 'i', '0', '0', word(f['ptr']))
        lst = int(lookup.get('eax', '0'), 16) if lookup.get('ok') else 0
        if not lst:
            raise LiveError('NOT_LEVELED', '%s is not a leveled list (GetFormAsLeveledList returned null)' % form)
        if level is None:
            level = fnv_link.value_of(self.link.eval('player.GetLevel'))
        level = int(level)
        raw = self.live.reads([(lst, 0x1C)])[0]
        chance_none, flags = raw[0x0C], raw[0x0D]
        entries = self.live.walk_list(lst + 4, 255)
        eraws = self.live.reads([(e, 8) for e in entries]) if entries else []
        listed = [{'form': struct.unpack_from('<I', r, 0)[0], 'count': struct.unpack_from('<H', r, 4)[0],
                   'level': struct.unpack_from('<H', r, 6)[0]} for r in eraws if r]
        buf = self._buffer()
        out_form, out_count, out_extra = buf, buf + 4, buf + 8
        instance = self._rng_instance() if seed is not None else None
        results = []
        ops_per = [('wr', word(buf), '000000000000000000000000'),
                   ('call', word(self.LEVELED_ROLL), 'v', word(lst), '0', word(level), word(out_form), word(out_count),
                    word(out_extra), '1', '0'),
                   ('rd', word(buf), '8')]
        remaining = int(rolls)
        first = True
        while remaining > 0:
            n = min(remaining, 80)
            ops = self._seed_ops(instance, int(seed)) if first and seed is not None else []
            prefix = len(ops)
            ops += ops_per * n
            replies = self.link.batch(ops, timeout=60)[prefix:]
            for k in range(n):
                call, read = replies[3 * k + 1], replies[3 * k + 2]
                if not call.get('ok') or not read.get('ok'):
                    results.append(None)
                    continue
                form_ptr, count = struct.unpack('<IH', bytes.fromhex(read['hex'])[:6])
                results.append((form_ptr, count))
            remaining -= n
            first = False
        pointers = {r[0] for r in results if r and r[0]} | {e['form'] for e in listed}
        described = self.describe_pointers(pointers)
        tally = Counter((r[0], r[1]) for r in results if r)
        none = sum(1 for r in results if r and not r[0])
        total = len([r for r in results if r])
        outcomes = []
        for (ptr, count), n in tally.most_common():
            if not ptr:
                continue
            d = described.get(ptr, {})
            outcomes.append({'form': d.get('formId'), 'editorId': d.get('editorId'), 'name': d.get('name'),
                             'count': count, 'times': n, 'share': round(n / total, 4) if total else None})
        return {'list': f['formId'], 'level': level, 'rolls': total, 'seed': seed,
                'chanceNone': chance_none, 'flags': {'calcAllLevelsBelow': bool(flags & 1),
                                                     'calcEachItem': bool(flags & 2), 'useAll': bool(flags & 4)},
                'entries': [{**{k: v for k, v in (described.get(e['form']) or {}).items() if k in ('formId', 'editorId', 'name')},
                             'level': e['level'], 'count': e['count'], 'eligible': e['level'] <= level} for e in listed],
                'outcomes': outcomes, 'nothing': {'times': none, 'share': round(none / total, 4) if total else None},
                'note': 'Each roll is the game\'s own TESLeveledList::CalculateCurrentForm (proven) at this level, '
                        'nested lists resolved. With seed the RNG is seeded first in the same request, so the same '
                        'seed gives the same rolls.',
                'uses': ['TESLeveledList::GetFormAsLeveledList / CalculateCurrentForm (proven)',
                         'TESLeveledList layout (decomp): entries +4, chance none +0x0C, flags +0x0D',
                         'BSRandom::QInstance (proven) + MersenneTwister state (decomp) for seed']}


# ====================================================================================== helpers
def crash_signature(info):
    exc = (info.get('exception') or {}).get('code') or '?'
    frames = info.get('frames') or []
    parts = []
    for f in frames[:4]:
        fn = (f.get('function') or {}).get('name') if isinstance(f.get('function'), dict) else None
        if fn:
            parts.append(fn)
        elif f.get('module'):
            parts.append('%s+%s' % (f.get('module'), f.get('offset') or '?'))
        else:
            parts.append(f.get('absolute') or '?')
    return '%s | %s' % (exc, ' < '.join(parts[:3])), {'exception': exc, 'frames': parts}


def window_capture(game, path):
    """PNG of the game window (PrintWindow, then a screen copy); {'path', 'blank'} or {'error'}."""
    try:
        import ctypes
        from ctypes import wintypes
        from . import wincapture
        proc = game.process()
        if not proc:
            return None
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            buffer = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buffer, 256)
            if pid.value == proc[0] and buffer.value == 'Fallout: New Vegas' and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        user32.EnumWindows(callback, 0)
        if not found:
            return {'error': 'no visible game window'}
        shot = wincapture.capture({'hwnd': found[0]}, path)
        return {'path': shot['path'], 'blank': shot['blank']}
    except Exception as e:  # evidence is best effort
        return {'error': str(e)}


# Menu ids (xNVSE GameUI.h; MessageMenu::GetClass returns 1001)
MENU_NAMES = {1001: 'MessageMenu', 1002: 'InventoryMenu', 1003: 'StatsMenu', 1004: 'HUDMainMenu', 1007: 'LoadingMenu',
              1008: 'ContainerMenu', 1009: 'DialogMenu', 1012: 'SleepWaitMenu', 1013: 'StartMenu', 1014: 'LockPickMenu',
              1016: 'QuantityMenu', 1023: 'MapMenu', 1026: 'BookMenu', 1027: 'LevelUpMenu', 1035: 'RepairMenu',
              1036: 'RaceSexMenu', 1047: 'CreditsMenu', 1048: 'CharGenMenu', 1051: 'TextEditMenu', 1053: 'BarterMenu',
              1054: 'SurgeryMenu', 1055: 'HackingMenu', 1056: 'VATSMenu', 1057: 'ComputersMenu',
              1058: 'RepairServicesMenu', 1059: 'TutorialMenu', 1060: 'SpecialBookMenu', 1061: 'ItemModMenu',
              1074: 'LoveTesterMenu', 1075: 'CompanionWheelMenu', 1076: 'TraitSelectMenu', 1077: 'RecipeMenu',
              1080: 'SlotMachineMenu', 1081: 'BlackJackMenu', 1082: 'RouletteMenu', 1083: 'CaravanMenu',
              1084: 'TraitMenu'}


def _chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]
