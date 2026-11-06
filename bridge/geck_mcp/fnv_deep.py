"""Deep engine tools (fnv-mcp milestone 7): watch hooks and tools that read the game from outside.

Hook-based (installed through the engine companion's "hook" op on proven decomp functions only):
  fnv_script_profile  call counts and time per script        Script::Run, Script::RunScriptEffect*
  fnv_script_trace    statements of one script as they run   ScriptRunner::EvaluateLine
  fnv_damage_log      applied damage with the hit's parts     Actor::DamageHealthAndFatigue
  fnv_crash_context   which scripts were running at a fault   Script::Run (+ the companion's exception handler)
Outside the process (work while the game is stuck and without the companion):
  fnv_hang_doctor     thread stacks sampled and named with the engine map
  fnv_patch_map       which engine code other plugins changed, against your unpacked FalloutNV.exe

Watch hooks only observe: they record and pass every call on unchanged. They are listed by fnv_hooks and
removed with fnv_hooks action=remove (or when the game exits).
"""
import gzip
import json
import os
import struct
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import fnv_procmem
from .fnv_facts import SCRIPT_BLOCKS
from .fnv_link import hexs, word
from .fnv_live import LiveError, _fid, _num

HF_EXIT, HF_RECORD, HF_TABLE = 1, 2, 4
SELECTORS = {'none': 0, 'ecx': 1, 'caller': 10, 'edx': 11, 'caller_arg0': 12, 'caller_arg0_ebx': 13, **{'arg%d' % i: 2 + i for i in range(8)}}
RECORD_WORDS, RING = 48, 4096
SCRIPT_TYPE, QUEST_TYPE = 17, 71  # xNVSE GameForms.h kFormType_Script / kFormType_TESQuest

WATCHES = {
    'script_run': {'function': 'Script::Run', 'flags': HF_EXIT | HF_TABLE, 'key': 'ecx', 'nargs': 2},
    'script_effect_start': {'function': 'Script::RunScriptEffectStart', 'flags': HF_EXIT | HF_TABLE, 'key': 'ecx', 'nargs': 2},
    'script_effect_finish': {'function': 'Script::RunScriptEffectFinish', 'flags': HF_EXIT | HF_TABLE, 'key': 'ecx', 'nargs': 2},
    'script_effect_update': {'function': 'Script::RunScriptEffectUpdate', 'flags': HF_EXIT | HF_TABLE, 'key': 'ecx', 'nargs': 3},
    'script_line': {'function': 'ScriptRunner::EvaluateLine', 'flags': HF_RECORD | HF_EXIT, 'nargs': 8, 'filter': 'arg0'},
    # The hit data is the first argument of the caller, the actor hit handler FUN_0089a760, which has an
    # aligned-stack frame (53 8B DC 83 EC 08 83 E4 F0...): its arguments are at [ebx + 8].
    'damage': {'function': 'Actor::DamageHealthAndFatigue', 'flags': HF_RECORD | HF_EXIT, 'nargs': 3,
               'snap': ('caller_arg0_ebx', 0x60)},
}
PROFILE = ('script_run', 'script_effect_start', 'script_effect_finish', 'script_effect_update')

# ScriptRunner output codes (decomp include/layout_support.h SCRIPT_OUTPUT); 0x100+ are command opcodes.
FLOW = {16: 'begin', 17: 'end', 18: 'short', 19: 'long', 20: 'float', 21: 'set', 22: 'if', 23: 'else', 24: 'elseif',
        25: 'endif', 26: 'while', 27: 'endwhile', 28: 'reference function', 29: 'scriptname', 30: 'return', 31: 'ref'}
# xNVSE GameForms.h BGSBodyPartData part types; ActorHitData flags from xNVSE GameProcess.h.
BODY_PARTS = ['torso', 'head1', 'head2', 'left arm1', 'left arm2', 'right arm1', 'right arm2', 'left leg1', 'left leg2',
              'left leg3', 'right leg1', 'right leg2', 'right leg3', 'brain', 'weapon']
HIT_FLAGS = {0x4: 'critical', 0x10: 'fatal', 0x20: 'dismember', 0x40: 'explode', 0x80: 'cripple',
             0x400: 'sneak_attack_critical'}


class HookError(LiveError):
    pass


# ===================================================================================== prologue decoder
_MODRM = (set(range(0x00, 0x04)) | set(range(0x08, 0x0C)) | set(range(0x10, 0x14)) | set(range(0x18, 0x1C)) |
          set(range(0x20, 0x24)) | set(range(0x28, 0x2C)) | set(range(0x30, 0x34)) | set(range(0x38, 0x3C)) |
          {0x63, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89, 0x8A, 0x8B, 0x8C, 0x8D, 0x8E, 0xD0, 0xD1, 0xD2, 0xD3,
           0xD8, 0xD9, 0xDA, 0xDB, 0xDC, 0xDD, 0xDE, 0xDF, 0xFE})
_MODRM_IMM8 = {0x80, 0x82, 0x83, 0x6B, 0xC0, 0xC1, 0xC6}
_MODRM_IMM32 = {0x81, 0x69, 0xC7}
_IMM8 = {0x6A, 0x04, 0x0C, 0x14, 0x1C, 0x24, 0x2C, 0x34, 0x3C, 0xA8} | set(range(0xB0, 0xB8))
_IMM32 = {0x68, 0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D, 0xA9} | set(range(0xB8, 0xC0))
_PLAIN = set(range(0x40, 0x60)) | {0x90, 0x98, 0x99, 0xCC, 0xFC, 0xF8, 0xF9}
_0F_MODRM = {0xB6, 0xB7, 0xBE, 0xBF, 0xAF, 0x1F, 0x10, 0x11, 0x28, 0x29, 0x57, 0x6E, 0x7E, 0xD6} | \
    set(range(0x40, 0x50)) | set(range(0x90, 0xA0))


def _modrm_length(code, i):
    modrm = code[i]
    mod, rm, n = modrm >> 6, modrm & 7, 1
    if mod == 3:
        return 1
    if rm == 4:
        n += 1
        if mod == 0 and (code[i + 1] & 7) == 5:
            n += 4
    elif mod == 0 and rm == 5:
        n += 4
    return n + (1 if mod == 1 else 4 if mod == 2 else 0)


def _instruction(code, i):
    """(length, offset of a rel32 field or None) of the x86 instruction at code[i]."""
    start, op16 = i, False
    while code[i] in (0x64, 0x65, 0x66, 0x2E, 0x3E, 0x26, 0x36, 0xF2, 0xF3):
        op16 = op16 or code[i] == 0x66
        i += 1
    op = code[i]
    i += 1
    imm = 2 if op16 else 4
    if op in _PLAIN:
        return i - start, None
    if op in _IMM8:
        return i + 1 - start, None
    if op in _IMM32:
        return i + imm - start, None
    if op in (0xA0, 0xA1, 0xA2, 0xA3):
        return i + 4 - start, None
    if op in (0xE8, 0xE9):
        return i + 4 - start, i - start
    if op == 0x0F:
        op2 = code[i]
        i += 1
        if 0x80 <= op2 <= 0x8F:
            return i + 4 - start, i - start
        if op2 in _0F_MODRM:
            return i + _modrm_length(code, i) - start, None
        raise HookError('UNSUPPORTED_PROLOGUE', 'Unsupported instruction 0F %02X in the prologue' % op2)
    if op in _MODRM:
        return i + _modrm_length(code, i) - start, None
    if op in _MODRM_IMM8:
        return i + _modrm_length(code, i) + 1 - start, None
    if op in _MODRM_IMM32:
        return i + _modrm_length(code, i) + imm - start, None
    if op in (0xF6, 0xF7):
        reg = (code[i] >> 3) & 7
        n = _modrm_length(code, i)
        return i + n + ((1 if op == 0xF6 else imm) if reg in (0, 1) else 0) - start, None
    if op == 0xFF:
        reg = (code[i] >> 3) & 7
        if reg in (4, 5):
            raise HookError('UNSUPPORTED_PROLOGUE', 'The function jumps away in its first bytes (FF /%d)' % reg)
        return i + _modrm_length(code, i) - start, None
    raise HookError('UNSUPPORTED_PROLOGUE', 'Unsupported or control-flow instruction %02X in the prologue' % op)


def decode_prologue(code, need=5):
    """Whole instructions covering at least `need` bytes: (stolen length, [rel32 offsets to relocate])."""
    i, relocs = 0, []
    while i < need:
        if i >= len(code) - 6:
            raise HookError('UNSUPPORTED_PROLOGUE', 'Prologue longer than the bytes read')
        if code[i] == 0xE9 and i:
            raise HookError('UNSUPPORTED_PROLOGUE', 'A jump inside the first %d bytes' % need)
        length, reloc = _instruction(code, i)
        if reloc is not None:
            relocs.append(i + reloc)
        i += length
        if code[i - length] == 0xE9:  # another module's hook jump at offset 0: move just that jump
            break
    if i > 16:
        raise HookError('UNSUPPORTED_PROLOGUE', 'Prologue instructions span more than 16 bytes')
    return i, relocs


# ===================================================================================== PE reference image
class CleanImage:
    """Your own unpacked FalloutNV.exe (Steamless output). Read locally; nothing is copied or shared."""

    def __init__(self, path):
        self.path = Path(path)
        data = self.path.read_bytes()
        pe = struct.unpack_from('<I', data, 0x3C)[0]
        if data[pe:pe + 4] != b'PE\0\0':
            raise HookError('CLEAN_EXE_INVALID', '%s is not a PE file' % path)
        count, opt_size = struct.unpack_from('<H', data, pe + 6)[0], struct.unpack_from('<H', data, pe + 20)[0]
        opt = pe + 24
        self.base = struct.unpack_from('<I', data, opt + 28)[0]
        directories = opt + 96
        self.iat = struct.unpack_from('<II', data, directories + 12 * 8)
        self.imports_dir = struct.unpack_from('<II', data, directories + 1 * 8)
        self.sections = []
        for k in range(count):
            o = opt + opt_size + 40 * k
            name = data[o:o + 8].rstrip(b'\0').decode('latin-1')
            vsize, va, rsize, raw = struct.unpack_from('<IIII', data, o + 8)
            self.sections.append({'name': name, 'va': self.base + va, 'size': vsize, 'raw': raw, 'rawSize': rsize})
        self.data = data

    def section(self, name):
        return next((s for s in self.sections if s['name'] == name), None)

    def read(self, address, length):
        for s in self.sections:
            if s['va'] <= address < s['va'] + s['size']:
                offset = address - s['va']
                chunk = self.data[s['raw'] + offset:s['raw'] + min(offset + length, s['rawSize'])]
                return chunk + b'\0' * (length - len(chunk))
        return None

    def imports(self):
        """{iat slot address: (dll, function)} from the import directory."""
        out = {}
        rva, _ = self.imports_dir
        if not rva:
            return out
        k = 0
        while True:
            entry = self.read(self.base + rva + 20 * k, 20)
            k += 1
            if not entry or entry == b'\0' * 20:
                break
            original, _, _, name_rva, first = struct.unpack('<IIIII', entry)
            dll = self.read(self.base + name_rva, 64).split(b'\0')[0].decode('latin-1')
            thunks = original or first
            n = 0
            while True:
                value = struct.unpack('<I', self.read(self.base + thunks + 4 * n, 4))[0]
                if not value:
                    break
                fn = ('#%d' % (value & 0xFFFF)) if value & 0x80000000 else \
                    self.read(self.base + value + 2, 80).split(b'\0')[0].decode('latin-1')
                out[self.base + first + 4 * n] = (dll, fn)
                n += 1
        return out


# ===================================================================================== the tools
class DeepTools:
    def __init__(self, live):
        self.live = live
        self._reverse = None
        self._baseline = {}

    @property
    def link(self):
        return self.live.link

    # ------------------------------------------------------------------ hook plumbing
    def hooks(self):
        result = self.link.one('hooks')
        if not result.get('ok'):
            raise HookError('HOOKS_UNAVAILABLE', 'The engine companion has no hook support: reinstall it with '
                            'fnv_companion_install (game closed)', result)
        return [h for h in result['hooks'] if h.get('stolen')]

    def hook(self, label):
        return next((h for h in self.hooks() if h['label'] == label), None)

    def _function(self, name):
        engine = self.live.engine()
        address = engine.resolve(name)
        record = engine.summary(address)
        if record.get('status') not in ('proven', 'class proven'):
            raise HookError('UNPROVEN_FUNCTION', '%s is %s: watch hooks go on proven functions only' % (name, record.get('status')))
        return address, record

    def ensure(self, watch, filter_value=None):
        """Install a named watch hook (or return the installed one)."""
        spec = WATCHES[watch]
        label = watch
        installed = self.hooks()
        for h in installed:
            if h['label'] == label:
                if filter_value is not None and (h.get('filter') != SELECTORS[spec['filter']] or
                                                 int(h.get('filterValue', '0'), 16) != filter_value):
                    result = self.link.one('hookfilter', word(h['id']), word(SELECTORS[spec['filter']]), word(filter_value))
                    if not result.get('ok'):
                        raise HookError('HOOK_REFUSED', 'Cannot change the filter: %s' % result.get('error'), result)
                    h = result['hook']
                return h
        address, record = self._function(spec['function'])
        for h in installed:
            if int(h['target'], 16) == address:  # installed under another label: replace
                self.remove(h['id'])
        live = self.live.reads([(address, 24)])[0]
        if live is None:
            raise HookError('UNREADABLE', 'Cannot read %s' % spec['function'])
        clean = self._clean_bytes(address, 24)
        if clean is not None and live[:8] != clean[:8] and live[0] != 0xE9:
            raise HookError('PATCHED_ELSEWHERE', '%s was changed by another module in a way the hook cannot chain onto'
                            % spec['function'], {'live': live[:16].hex(' '), 'clean': clean[:16].hex(' ')})
        stolen, relocs = decode_prologue(live)
        snap_sel, snap_len = (SELECTORS[spec['snap'][0]], spec['snap'][1]) if spec.get('snap') else (0, 0)
        result = self.link.one('hook', word(address), word(stolen), ','.join('%x' % r for r in relocs) or '-',
                               word(spec['flags']), word(SELECTORS[spec['filter']] if filter_value is not None else 0),
                               word(filter_value or 0), word(SELECTORS.get(spec.get('key', 'none'), 0)), word(spec['nargs']),
                               '0', '0', word(snap_sel), word(snap_len), hexs(label))
        if not result.get('ok'):
            raise HookError('HOOK_REFUSED', 'The companion refused the hook on %s: %s' % (spec['function'], result.get('error')),
                            result)
        h = result['hook']
        h['function'] = record
        h['chained'] = live[0] == 0xE9
        return h

    def remove(self, hook_id):
        result = self.link.one('unhook', word(hook_id))
        if not result.get('ok'):
            raise HookError('UNHOOK_FAILED', 'Hook %s: %s' % (hook_id, result.get('error')), result)
        return result

    def records(self, h, since=0, limit=4096):
        """Ring records with sequence numbers in (since, current]."""
        seq = self.live.u32s([int(h['seqAddress'], 16)])[0] or 0
        first = max(since + 1, seq - RING + 1, 1)
        if seq - first + 1 > limit:
            first = seq - limit + 1
        if first > seq:
            return seq, [], 0
        base, size = int(h['records'], 16), RECORD_WORDS * 4
        spans, s = [], first
        while s <= seq:
            index = (s - 1) % RING
            count = min(seq - s + 1, RING - index, 0xFF00 // size)
            spans.append((s, base + index * size, count))
            s += count
        raws = self.live.reads([(addr, count * size) for _, addr, count in spans])
        out, lost = [], 0
        for (s0, _, count), raw in zip(spans, raws):
            for k in range(count):
                words = struct.unpack_from('<%dI' % RECORD_WORDS, raw, k * size) if raw else None
                if not words or words[0] != s0 + k:
                    lost += 1
                    continue
                out.append(words)
        return seq, out, lost

    def table(self, h):
        raw = self.live.reads([(int(h['table'], 16), h['tableSize'] * 24)])[0] or b''
        out = []
        for k in range(len(raw) // 24):
            key, count, total, mx, _ = struct.unpack_from('<IIQII', raw, k * 24)
            if count:
                out.append({'key': key, 'count': count, 'ticks': total, 'maxTicks': mx})
        return out

    # ------------------------------------------------------------------ naming helpers
    def _clean_bytes(self, address, length):
        image = self._image(required=False)
        return image.read(address, length) if image else None

    def _image(self, required=True):
        fnv = getattr(self.live.project, 'fnv', None)
        path = getattr(fnv, 'clean_exe', None) or os.environ.get('FNV_CLEAN_EXE')
        if not path or not Path(path).is_file():
            if required:
                raise HookError('CLEAN_EXE_MISSING', 'Set fnv.clean_exe to your own unpacked FalloutNV.exe 1.4.0.525 '
                                '(the Steam exe keeps its code encrypted on disk; Steamless unpacks it). It is read '
                                'locally only.')
            return None
        if getattr(self, '_image_cache', None) is None or self._image_cache.path != Path(path):
            self._image_cache = CleanImage(path)
        return self._image_cache

    def _reverse_index(self):
        """{master: {local id: (EditorID, type, name)}} from the offline indexes."""
        if self._reverse is None:
            self._reverse = {}
            for path in sorted((Path(self.live.project.state_dir) / 'index').glob('*.json.gz')):
                try:
                    data = json.load(gzip.open(path))
                except (OSError, ValueError):
                    continue
                table = {}
                for edid, entry in data.get('entries', {}).items():
                    for e in (entry if entry and isinstance(entry[0], list) else [entry]):
                        table.setdefault(int(e[1], 16) & 0xFFFFFF, (edid, e[0], e[2]))
                self._reverse[data.get('master') or path.name[:-8]] = table
        return self._reverse

    def _masters_by_index(self):
        """{load-order index: master file} for the masters with an offline index, from the running game."""
        masters = list(self._reverse_index())
        values = self.live.values(['GetModIndex "%s"' % m for m in masters]) if masters else []
        return {int(v): m for m, v in zip(masters, values) if v is not None and 0 <= v < 255}

    def describe_forms(self, form_ids, names=True):
        """{form id: {editorId?, type?, name?}} using the offline indexes and GetName."""
        ids = sorted({f for f in form_ids if f})
        try:
            masters = self._masters_by_index()
        except Exception:  # game gone: only FalloutNV.esm (index 0) can be named
            masters = {0: 'FalloutNV.esm'} if 'FalloutNV.esm' in self._reverse_index() else {}
        out = {}
        for f in ids:
            value = int(f, 16)
            master = masters.get(value >> 24)
            hit = self._reverse_index().get(master, {}).get(value & 0xFFFFFF) if master else None
            out[f] = {'editorId': hit[0], 'type': hit[1], 'name': hit[2] or None, 'master': master} if hit else {}
        if names:
            try:
                for f, n in self.live.names([f for f in ids if not out[f].get('name')]).items():
                    if n and n != '<no name>':
                        out[f]['name'] = n
            except Exception:
                pass
        return out

    def _forms(self, pointers):
        infos = self.live.form_ids(list(pointers))
        return dict(zip(pointers, infos))

    # ------------------------------------------------------------------ fnv_script_profile
    def script_profile(self, action='read', seconds=3.0, top=25):
        self.live.in_game()
        if action == 'stop':
            removed = []
            keep = self._crash_context_on()
            for h in self.hooks():
                if h['label'] in PROFILE and not (keep and h['label'] in PROFILE):
                    self.remove(h['id'])
                    removed.append(h['label'])
            return {'removed': removed, 'kept': list(PROFILE) if keep else [],
                    'note': 'crash context is on: its script hooks stay (fnv_crash_context action=disable)' if keep else None}
        installed = [self.ensure(w) for w in PROFILE]
        if action == 'start' or not self._baseline.get('profile'):
            for h in installed:
                self.link.one('hookctl', word(h['id']), '1', 'clear')
            ping = self.link.ping()
            self._baseline['profile'] = {'frame': ping.get('frame'), 'time': time.time()}
            if action == 'start':
                return {'started': True, 'hooks': [self._hook_out(h) for h in installed],
                        'note': 'Play or wait, then call again with action=read.'}
            time.sleep(max(0.2, min(float(seconds), 60.0)))
        ping = self.link.ping()
        base = self._baseline['profile']
        frames = max(1, (ping.get('frame') or 0) - (base.get('frame') or 0))
        freq = installed[0]['qpcFrequency'] or 1
        per_script = defaultdict(lambda: {'calls': 0, 'ticks': 0, 'maxTicks': 0, 'kinds': set()})
        for h in installed:
            for row in self.table(h):
                entry = per_script[row['key']]
                entry['calls'] += row['count']
                entry['ticks'] += row['ticks']
                entry['maxTicks'] = max(entry['maxTicks'], row['maxTicks'])
                entry['kinds'].add({'script_run': 'run', 'script_effect_start': 'effect start',
                                    'script_effect_finish': 'effect finish',
                                    'script_effect_update': 'effect update'}[h['label']])
        ranked = sorted(per_script.items(), key=lambda kv: -kv[1]['ticks'])
        total_ticks = sum(e['ticks'] for _, e in ranked)
        shown = ranked[:top]
        forms = self._forms([p for p, _ in shown])
        quests = dict(zip([p for p, _ in shown], self.live.u32s([p + 0x40 for p, _ in shown]))) if shown else {}
        quest_forms = self._forms([q for q in quests.values() if q])
        described = self.describe_forms([(forms.get(p) or {}).get('formId') for p, _ in shown] +
                                        [(quest_forms.get(q) or {}).get('formId') for q in quests.values() if q])
        rows = []
        for ptr, e in shown:
            info = forms.get(ptr) or {}
            fid = info.get('formId')
            quest = quest_forms.get(quests.get(ptr)) if quests.get(ptr) else None
            rows.append({'script': {'formId': fid, 'ptr': '0x%08X' % ptr, **described.get(fid, {})},
                         'quest': {'formId': quest['formId'], **described.get(quest['formId'], {})} if quest else None,
                         'kinds': sorted(e['kinds']), 'calls': e['calls'], 'callsPerFrame': round(e['calls'] / frames, 2),
                         'totalMs': round(e['ticks'] * 1000 / freq, 3), 'msPerFrame': round(e['ticks'] * 1000 / freq / frames, 4),
                         'avgUs': round(e['ticks'] * 1e6 / freq / e['calls'], 2), 'maxUs': round(e['maxTicks'] * 1e6 / freq, 1),
                         'share': round(e['ticks'] / total_ticks, 3) if total_ticks else None})
        return {'frames': frames, 'seconds': round(time.time() - base['time'], 2), 'scripts': len(per_script),
                'totalScriptMs': round(total_ticks * 1000 / freq, 3),
                'scriptMsPerFrame': round(total_ticks * 1000 / freq / frames, 4), 'top': rows,
                'measured': 'inclusive wall time of each call, hook overhead included (about a microsecond per call)',
                'hooks': [self._hook_out(h) for h in installed]}

    # ------------------------------------------------------------------ fnv_script_trace
    def script_form(self, spec):
        """A Script form pointer for a script EditorID/form id, or the script of a quest or reference."""
        target = self.live.resolve(spec)
        info = self.live.form_ids([target['ptr']])[0] or {}
        if info.get('formType') == SCRIPT_TYPE:
            return target['ptr'], info, None
        if info.get('formType') == QUEST_TYPE:
            script = self.live.u32s([target['ptr'] + 0x1C])[0]  # TESQuest: TESScriptableForm at +0x18, script at +0x1C
        else:
            result = self.link.eval('GetScript %s' % target['expr'])
            element = result.get('result') or {}
            script = int(element['ptr'], 16) if element.get('type') == 'form' and element.get('ptr') else 0
        if not script:
            raise HookError('NO_SCRIPT', '%s has no script' % spec)
        return script, self.live.form_ids([script])[0], {'owner': spec, 'ownerFormId': target.get('formId')}

    def script_source(self, script_ptr):
        """The script's source text if the game kept it (Script::m_pText, +0x2C); usually it does not."""
        text_ptr = self.live.u32s([script_ptr + 0x2C])[0]
        if not text_ptr:
            return None
        result = self.link.one('rs', word(text_ptr), word(0xF000))
        return result.get('text') if result.get('ok') else None

    def script_trace(self, script, action='read', source=None, limit=200):
        self.live.in_game()
        ptr, info, owner = self.script_form(script)
        label = 'trace@%08X' % ptr
        if action == 'stop':
            h = self.hook('script_line')
            if h:
                self.remove(h['id'])
            return {'removed': bool(h), 'script': info}
        current = self.hook('script_line')
        switching = not current or int(current.get('filterValue', '0'), 16) != ptr
        h = self.ensure('script_line', ptr)
        state = self._baseline.setdefault(label, {'since': h['seq']})
        if action == 'start' or switching:
            state['since'] = self.live.u32s([int(h['seqAddress'], 16)])[0] or 0
        if action == 'start':
            return {'started': True, 'script': info, 'owner': owner, 'hook': self._hook_out(h),
                    'note': 'Let the script run, then call again with action=read.'}
        seq, recs, lost = self.records(h, state['since'], limit=max(1, min(limit, RING)))
        state['since'] = seq
        source_from = 'argument' if source else None
        if not source:
            source = self.script_source(ptr)
            source_from = 'game memory' if source else None
        if not source and info.get('formId'):
            from .fnv_data import DataTools
            source = DataTools(self.live.project, self.live).script_text(info['formId'])
            source_from = 'plugin record (SCTX)' if source else None
        lines = statement_lines(source) if source else None
        commands = {int(c['opcode'], 16): c['name'] for c in self.live.engine().commands if c.get('opcode')}
        refs = self._forms(sorted({r[14] for r in recs if r[14]}))
        # The engine's line counter (EvaluateLine's lineNumber) advances once for a whole skipped block, so it is
        # not a statement number. Walk the compiled data instead: statement k starts at the offset EvaluateLine
        # gets (after the opcode, reference and length fields).
        data_ptr, size = self.live.u32s([ptr + 0x30, ptr + 0x20])
        data = self.live.reads([(data_ptr, min(size, 0xFF00))])[0] if data_ptr and size else None
        starts = statement_offsets(data) if data else []
        index = {offset: k + 1 for k, offset in enumerate(starts)}
        steps, executed = [], Counter()
        for r in recs:
            output, offset = r[13], r[16]
            number = index.get(offset)
            executed[number] += 1
            step = {'frame': r[22], 'statement': number, 'kind': FLOW.get(output) or commands.get(output) or '0x%04X' % output,
                    'offset': offset, 'ref': (refs.get(r[14]) or {}).get('formId') if r[14] else None,
                    'ok': bool(r[8] & 0xFF) if r[1] == 2 else None, 'us': round(((r[11] << 32) | r[10]) * 1e6 / h['qpcFrequency'], 1)
                    if r[1] == 2 else None}
            if output == 16 and data and offset + 2 <= len(data):  # BEGIN: the block id is the first short
                block = struct.unpack_from('<H', data, offset)[0]
                step['block'] = SCRIPT_BLOCKS[block] if block < len(SCRIPT_BLOCKS) else block
            if lines and number and number <= len(lines):
                step['line'], step['text'] = lines[number - 1]
            steps.append(step)
        # BEGIN / IF / ELSEIF: taken when the next statement run in the same frame is the next one in the script.
        for k, step in enumerate(steps):
            if step['kind'] in ('begin', 'if', 'elseif') and step['statement']:
                nxt = steps[k + 1] if k + 1 < len(steps) else None
                step['taken'] = None if nxt is None else (nxt['frame'] == step['frame'] and nxt['statement'] == step['statement'] + 1)
        if lines and starts and len(lines) != len(starts):
            lines_note = ('the source has %d statements but the compiled script %d: line numbers may be off'
                          % (len(lines), len(starts)))
        else:
            lines_note = None
        out = {'script': {**info, **self.describe_forms([info.get('formId')], names=False).get(info.get('formId'), {})},
               'owner': owner, 'steps': steps, 'lost': lost, 'seq': seq, 'statements': len(starts), 'sourceFrom': source_from,
               'sourceMapped': bool(lines), 'note': lines_note}
        if lines:
            out['notRun'] = [{'statement': k + 1, 'line': ln, 'text': t} for k, (ln, t) in enumerate(lines)
                             if (k + 1) not in executed and t.split()[0].lower() not in ('scn', 'scriptname')][:100]
        else:
            out['note'] = ('The game does not keep this script\'s source; pass source= (the .gek/.txt text) to map '
                           'statement numbers to lines.')
        if any(s['ok'] is False for s in steps):
            out['stopped'] = 'A statement returned failure: the game stopped this run of the script there.'
        return out

    # ------------------------------------------------------------------ fnv_damage_log
    HIT_CALLER = 0x0089A760  # HActor::FUN_0089a760(hit, mode): the actor hit handler (probable name)

    def damage_log(self, action='read', limit=50):
        """Hits from a watch hook on Actor::DamageHealthAndFatigue (proven). When the caller is the actor hit
        handler (FUN_0089a760, which takes the hit data as its first argument), the hook also copies that hit
        data (0x60 bytes) from the caller's frame, so each applied damage comes with its breakdown."""
        self.live.in_game()
        if action == 'stop':
            removed = []
            for h in self.hooks():
                if h['label'] == 'damage':
                    self.remove(h['id'])
                    removed.append(h['label'])
            return {'removed': removed}
        hook = self.ensure('damage')
        state = self._baseline.setdefault('damage', {'since': hook['seq']})
        if action == 'start':
            state['since'] = self.live.u32s([int(hook['seqAddress'], 16)])[0] or 0
            return {'started': True, 'hooks': [self._hook_out(hook)]}
        seq, recs, lost = self.records(hook, state['since'], limit)
        state['since'] = seq
        engine = self.live.engine()
        handler = engine.containing(self.HIT_CALLER)
        hit_end = self.HIT_CALLER + (engine.functions.get(self.HIT_CALLER, {}).get('size') or 0x1000)
        hits, pointers = [], set()
        for r in recs:
            entry = {'frame': r[22], 'actor': r[3], 'health': _f(r[12]), 'fatigue': _f(r[13]), 'attacker': r[14],
                     'returned': bool(r[8] & 0xFF) if r[1] == 2 else None, 'caller': engine.symbolize(r[5]).get('name'),
                     'callerAddress': '0x%08X' % r[5]}
            if self.HIT_CALLER <= r[5] < hit_end:
                hit = decode_hit(r)
                hit.pop('frame', None)
                entry['hit'] = hit
                pointers |= {p for p in (hit['source'], hit['target'], hit['weapon'], hit['projectile']) if p}
            pointers |= {p for p in (entry['actor'], entry['attacker']) if p}
            hits.append(entry)
        forms = self._forms(sorted(pointers))
        names = self.describe_forms([(forms.get(p) or {}).get('formId') for p in pointers])

        def form(p):
            info = forms.get(p) if p else None
            if not info:
                return None
            return {'formId': info['formId'], **{k: v for k, v in names.get(info['formId'], {}).items() if k in ('editorId', 'name')}}

        for entry in hits:
            entry['actor'], entry['attacker'] = form(entry['actor']), form(entry['attacker'])
            if entry.get('hit'):
                for key in ('source', 'target', 'weapon', 'projectile'):
                    entry['hit'][key] = form(entry['hit'][key])
        return {'hits': hits[-limit:], 'lost': lost, 'seq': seq, 'handler': handler and engine.summary(handler[0]),
                'fields': 'health/fatigue: what Actor::DamageHealthAndFatigue applied. hit: the hit data the handler '
                          'passed on, decoded with the xNVSE ActorHitData layout (GameProcess.h, offsets 0x00-0x5C; the '
                          'decomp copies the same 0x60 bytes in HitDataEx::FUN_009b4ec0). The name of the handler is '
                          'probable, not proven; the hook itself sits on the proven function.'}

    # ------------------------------------------------------------------ fnv_crash_context
    def _crash_context_on(self):
        return bool(self._baseline.get('crash_context'))

    def crash_context(self, action='latest', limit=3):
        if action in ('enable', 'disable', 'status'):
            self.live.ready()
            if action == 'enable':
                installed = [self.ensure(w) for w in PROFILE]
                self._baseline['crash_context'] = True
                return {'enabled': True, 'hooks': [self._hook_out(h) for h in installed],
                        'note': 'The companion\'s exception handler is always on; these hooks add the running scripts.'}
            if action == 'disable':
                self._baseline['crash_context'] = False
                removed = []
                if not self._baseline.get('profile'):
                    for h in self.hooks():
                        if h['label'] in PROFILE:
                            self.remove(h['id'])
                            removed.append(h['label'])
                return {'enabled': False, 'removed': removed}
            info = self.link.info()
            return {'enabled': self._crash_context_on(), 'handler': True, 'folder': info.get('crashContextDir'),
                    'hooks': [self._hook_out(h) for h in self.hooks() if h['label'] in PROFILE]}
        files = context_files()[:max(1, min(int(limit), 20))]
        return {'files': [self.enrich_context(load_context(f)) for f in files], 'folder': str(context_folder())}

    def enrich_context(self, ctx):
        engine = self.live.engine()
        ctx = dict(ctx)
        ctx['faultSite'] = engine.symbolize(int(ctx['address'], 16))
        ids = []
        for call in ctx.get('hookedCalls', []):
            call['function'] = engine.symbolize(int(call['target'], 16))
            call['callerSite'] = engine.symbolize(int(call['caller'], 16))
            ids += [call.get(k) for k in ('thisFormId', 'arg0FormId', 'arg1FormId') if call.get(k)]
        named = self.describe_forms(ids, names=False) if ids else {}
        for call in ctx.get('hookedCalls', []):
            for k in ('this', 'arg0', 'arg1'):
                fid = call.get(k + 'FormId')
                if fid and named.get(fid):
                    call[k + 'Form'] = named[fid]
        if ctx.get('hookedCalls'):
            top = ctx['hookedCalls'][0]
            ctx['verdict'] = 'Fault while %s was running script %s on %s' % (
                top['label'], (top.get('thisForm') or {}).get('editorId') or top.get('thisFormId') or top['this'],
                (top.get('arg0Form') or {}).get('editorId') or top.get('arg0FormId') or top.get('arg0'))
        ctx['note'] = 'Written by the companion at the first-chance fault; a fault a handler recovered from leaves a file too.'
        return ctx

    # ------------------------------------------------------------------ fnv_hooks
    def hooks_tool(self, action='list', hook_id=None):
        self.live.ready()
        if action == 'remove':
            targets = [h for h in self.hooks() if hook_id in (None, 'all') or str(h['id']) == str(hook_id)]
            for h in targets:
                self.remove(h['id'])
            self._baseline = {}
            return {'removed': [h['label'] for h in targets]}
        engine = self.live.engine()
        return {'hooks': [dict(self._hook_out(h), function=engine.summary(int(h['target'], 16)).get('name'))
                          for h in self.hooks()]}

    @staticmethod
    def _hook_out(h):
        return {'id': h['id'], 'label': h['label'], 'target': h['target'], 'calls': h['calls'], 'records': h['seq'],
                'active': h['active'], 'chained': h.get('chained')}

    # ------------------------------------------------------------------ fnv_hang_doctor
    def hang_doctor(self, samples=5, interval_ms=250, all_threads=False):
        found = self.live.game.process()
        if not found:
            raise HookError('GAME_NOT_RUNNING', 'FalloutNV.exe is not running')
        pid = found[0]
        ping = self.live.game.ping(timeout=2.0)
        engine = self.live.engine()
        with fnv_procmem.GameProcess(pid) as proc:
            modules = proc.modules()
            threads = proc.threads()
            main = ping.get('mainThreadId') or (threads[0] if threads else None)
            wanted = threads if all_threads else [main]
            stacks = defaultdict(list)
            for k in range(max(1, min(int(samples), 20))):
                if k:
                    time.sleep(max(0.05, min(interval_ms, 5000)) / 1000)
                for tid in wanted:
                    regs = proc.thread_context(tid)
                    if regs:
                        frames, scanned = fnv_procmem.walk_stack(proc, regs, modules)
                        stacks[tid].append((frames, scanned))
        modmap = [{'name': m['name'], 'base': m['base'], 'size': m['size'], 'path': m['path']} for m in modules]

        def name(address):
            sym = engine.symbolize(address, modmap)
            if sym.get('kind') == 'function':
                return {'address': '0x%08X' % address, 'function': sym.get('name'), 'status': sym.get('status'),
                        'offset': sym.get('offset')}
            if sym.get('kind') == 'module':
                return {'address': '0x%08X' % address, 'module': sym['module'], 'offset': sym['offset']}
            return {'address': '0x%08X' % address, 'kind': sym.get('kind')}

        threads_out = []
        for tid, runs in stacks.items():
            tops = Counter()
            sampled = []
            for frames, scanned in runs:
                named = [name(f['address']) for f in frames[:24]]
                sampled.append({'frames': named, 'stackScan': [dict(name(s['address']), at=s['stackOffset']) for s in scanned[:12]]})
                game = next((n for n in named if n.get('function')), None)
                tops[(game or named[0]).get('function') or (game or named[0]).get('module') or named[0]['address']] += 1
            stuck, hits = tops.most_common(1)[0] if tops else (None, 0)
            threads_out.append({'threadId': tid, 'main': tid == main, 'topGameFunction': stuck,
                                'sameInSamples': '%d/%d' % (hits, len(runs)), 'samples': sampled})
        main_out = next((t for t in threads_out if t['main']), None)
        since = ping.get('msSinceFrame')
        if ping.get('error'):
            state = 'companion unreachable'
        elif since is not None and since > 3000:
            state = 'hung: no frame for %.1f s' % (since / 1000)
        else:
            state = 'running (last frame %s ms ago)' % since
        verdict = None
        if main_out:
            verdict = 'Main thread %s; in %s of %s samples it was in %s' % (
                state, main_out['sameInSamples'].split('/')[0], main_out['sameInSamples'].split('/')[1],
                main_out['topGameFunction'])
        return {'pid': pid, 'state': state, 'verdict': verdict, 'mainThreadId': main, 'threads': threads_out,
                'threadCount': len(threads), 'ping': {k: ping.get(k) for k in ('frame', 'msSinceFrame', 'busy', 'error')},
                'note': 'Frames come from the EBP chain (MSVC frame pointers); stackScan lists other return addresses '
                        'found on the stack top (heuristic). Names marked status=proven are evidence.'}

    # ------------------------------------------------------------------ fnv_patch_map
    def patch_map(self, include_bytes=False, limit=400):
        found = self.live.game.process()
        if not found:
            raise HookError('GAME_NOT_RUNNING', 'FalloutNV.exe is not running')
        image = self._image()
        engine = self.live.engine()
        ours = []
        try:
            ours = self.hooks()
        except Exception:
            pass
        with fnv_procmem.GameProcess(found[0]) as proc:
            modules = proc.modules()
            text = image.section('.text')
            regions, unreadable, compared = [], 0, 0
            for start, live in proc.read_span(text['va'], text['size']):
                clean = image.read(start, len(live) if live else 0x10000)
                if live is None:
                    unreadable += 1
                    continue
                compared += len(live)
                if live == clean:
                    continue
                k = 0
                while k < len(live):
                    if live[k:k + 64] == clean[k:k + 64]:
                        k += 64
                        continue
                    end = k + 64
                    for j in range(k, min(end, len(live))):
                        if live[j] != clean[j]:
                            if regions and start + j - regions[-1][1] <= 8:
                                regions[-1][1] = start + j + 1
                            else:
                                regions.append([start + j, start + j + 1])
                    k = end
            if compared and sum(e - s for s, e in regions) > compared * 0.05:
                raise HookError('CLEAN_EXE_MISMATCH', 'More than 5%% of the code differs: %s is not an unpacked '
                                'FalloutNV.exe 1.4.0.525' % image.path)
            hook_stubs = {int(h['target'], 16): h for h in ours}
            patches = [self._classify(proc, image, modules, engine, s, e, hook_stubs, include_bytes) for s, e in regions]
            imports = self._import_patches(proc, image, modules)
            vtables = self._rdata_patches(proc, image, modules, engine)
        by_owner = Counter(p['owner'] for p in patches + imports + vtables)
        functions = defaultdict(set)
        for p in patches:
            if p.get('function', {}).get('functionAddress'):
                functions[(p['function']['functionAddress'], p['function'].get('name'))].add(p['owner'])
        shared = [{'function': name, 'address': addr, 'owners': sorted(owners)}
                  for (addr, name), owners in functions.items() if len(owners) > 1]
        return {'pid': found[0], 'patchCount': len(patches), 'byOwner': dict(by_owner.most_common()),
                'sharedFunctions': shared, 'chained': [p for p in patches if p.get('chain') and len(p['chain']) > 1][:50],
                'patches': patches[:limit], 'importHooks': imports[:100], 'pointerPatches': vtables[:200],
                'truncated': len(patches) > limit, 'unreadableChunks': unreadable,
                'reference': {'path': str(image.path), 'note': 'your unpacked FalloutNV.exe, read locally'},
                'note': 'Owner = the module the patched code jumps or points into. A function patched by several '
                        'owners, or a jump that leads through several modules, is where plugin conflicts happen.'}

    def _owner(self, modules, address, hook_stubs):
        m = fnv_procmem.module_at(modules, address)
        if m:
            return m['name']
        return 'allocated memory (trampoline or stub)'

    def _classify(self, proc, image, modules, engine, start, end, hook_stubs, include_bytes):
        live = proc.read(start - 1, end - start + 6) or b''
        clean = image.read(start - 1, end - start + 6)
        out = {'address': '0x%08X' % start, 'length': end - start}
        sym = engine.symbolize(start)
        if sym.get('kind') == 'function':
            out['function'] = {'functionAddress': sym['functionAddress'], 'name': sym.get('name'),
                               'status': sym.get('status'), 'offset': sym.get('offset')}
        kind, dest = 'bytes', None
        for p in (1, 0):  # the opcode may be unchanged (a call whose target changed) or new
            if p < len(live) and live[p] in (0xE8, 0xE9) and p + 5 <= len(live):
                dest = (start - 1 + p + 5 + struct.unpack_from('<i', live, p + 1)[0]) & 0xFFFFFFFF
                kind = 'jump' if live[p] == 0xE9 else 'call'
                break
        if dest is None and end - start <= 4 and len(live) >= 5:
            value = struct.unpack_from('<I', live, 1)[0]  # an absolute pointer written over code (call [x], push x)
            if fnv_procmem.module_at(modules, value) and not (0x00400000 <= value < 0x01400000):
                dest, kind = value, 'pointer'
        if start in hook_stubs:
            out['owner'] = 'fnvmcp engine companion (watch hook %s)' % hook_stubs[start]['label']
        elif dest is not None:
            out['owner'] = self._owner(modules, dest, hook_stubs)
            chain, at = [], dest
            for _ in range(4):
                chain.append({'address': '0x%08X' % at, 'owner': self._owner(modules, at, hook_stubs)})
                head = proc.read(at, 5)
                if not head or head[0] != 0xE9:
                    break
                at = (at + 5 + struct.unpack_from('<i', head, 1)[0]) & 0xFFFFFFFF
            out['destination'] = '0x%08X' % dest
            out['chain'] = chain
        else:
            out['owner'] = 'unknown (bytes changed in place)'
        out['kind'] = kind
        if include_bytes or kind == 'bytes':
            out['live'] = live[1:1 + min(end - start, 16)].hex(' ')
            out['original'] = clean[1:1 + min(end - start, 16)].hex(' ') if clean else None
        return out

    def _import_patches(self, proc, image, modules):
        out = []
        system = os.environ.get('SystemRoot', 'C:\\Windows').lower()
        for slot, (dll, fn) in image.imports().items():
            value = proc.u32(slot)
            m = fnv_procmem.module_at(modules, value) if value else None
            if not m or m['path'].lower().startswith(system):
                continue
            out.append({'slot': '0x%08X' % slot, 'import': '%s!%s' % (dll, fn), 'owner': m['name'],
                        'destination': '0x%08X' % value})
        return out

    def _rdata_patches(self, proc, image, modules, engine):
        out = []
        rdata = image.section('.rdata')
        if not rdata:
            return out
        iat_lo = image.base + image.iat[0]
        iat_hi = iat_lo + image.iat[1]
        for start, live in proc.read_span(rdata['va'], rdata['size']):
            if live is None:
                continue
            clean = image.read(start, len(live))
            if live == clean:
                continue
            for k in range(0, len(live) - 3, 4):
                if live[k:k + 4] == clean[k:k + 4] or iat_lo <= start + k < iat_hi:
                    continue
                value = struct.unpack_from('<I', live, k)[0]
                m = fnv_procmem.module_at(modules, value)
                entry = {'slot': '0x%08X' % (start + k), 'owner': m['name'] if m else 'unknown',
                         'value': '0x%08X' % value, 'original': '0x%08X' % struct.unpack_from('<I', clean, k)[0]}
                vt = engine.vtable_at(start + k)
                if vt:
                    entry['vtable'] = vt
                out.append(entry)
        return out


def _f(word_value):
    value = struct.unpack('<f', struct.pack('<I', word_value))[0]
    return round(value, 3) if value == value else None


def decode_hit(r):
    snap = struct.pack('<24I', *r[24:48])
    (source, target, projectile, unk0c, location, health, base, fatigue, limb, block_dt, armor, f2c, weapon, health_pct) = \
        struct.unpack_from('<IIIIifffffffIf', snap, 0)
    flags, mult = struct.unpack_from('<If', snap, 0x58)
    return {'frame': r[22], 'source': source, 'target': target, 'projectile': projectile, 'weapon': weapon,
            'hitLocation': BODY_PARTS[location] if 0 <= location < len(BODY_PARTS) else location,
            'healthDamage': _rf(health), 'weaponBaseDamage': _rf(base), 'fatigueDamage': _rf(fatigue),
            'limbDamage': _rf(limb), 'blockDTMod': _rf(block_dt), 'armorDamage': _rf(armor), 'field2C': _rf(f2c),
            'healthPercent': _rf(health_pct), 'damageMultiplier': _rf(mult),
            'flags': [n for bit, n in HIT_FLAGS.items() if flags & bit], 'rawFlags': '0x%X' % flags,
            'impact': [_rf(v) for v in struct.unpack_from('<3f', snap, 0x38)]}


def _rf(value):
    return round(value, 3) if value == value else None


def statement_offsets(data):
    """Offsets where each compiled statement's body starts (what ScriptRunner::GetNextLine leaves for
    EvaluateLine): u16 opcode, [u16 reference index + u16 opcode for a reference function], u16 length, body."""
    out, pos = [], 0
    while pos + 4 <= len(data):
        opcode = struct.unpack_from('<H', data, pos)[0]
        pos += 2
        if opcode == 28:  # reference function: ref index, then the real opcode
            pos += 4
        if pos + 2 > len(data):
            break
        length = struct.unpack_from('<H', data, pos)[0]
        pos += 2
        out.append(pos)
        pos += length
    return out


def statement_lines(source):
    """[(line number, text)] of the source lines that compile to a statement, in order: what
    ScriptRunner::EvaluateLine numbers 1, 2, 3... (variable declarations and comments compile to none)."""
    out = []
    for n, raw in enumerate(source.replace('\r', '').split('\n'), 1):
        text = raw.split(';', 1)[0].strip()
        if not text:
            continue
        first = text.split()[0].lower()
        if first in ('short', 'int', 'long', 'float', 'ref', 'reference', 'array_var', 'string_var'):
            continue
        out.append((n, text))
    return out


# ------------------------------------------------------------------ crash context files
def context_folder():
    return Path(os.environ.get('LOCALAPPDATA', '')) / 'fnvmcp'


def context_files():
    folder = context_folder()
    return sorted(folder.glob('crash-context-*.json'), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []


def load_context(path):
    data = json.loads(Path(path).read_text(encoding='utf-8', errors='replace'))
    data['file'] = str(path)
    data['modified'] = Path(path).stat().st_mtime
    return data


def match_context(exception_address, log_mtime, window=180):
    """The companion's crash-context file for a crash log: same exception address, written near the log."""
    try:
        address = int(str(exception_address), 16)
    except (TypeError, ValueError):
        return None
    for path in context_files()[:20]:
        try:
            data = load_context(path)
        except (OSError, ValueError):
            continue
        if int(data.get('address', '0'), 16) == address and (not log_mtime or abs(data['modified'] - log_mtime) < window):
            return data
    return None
