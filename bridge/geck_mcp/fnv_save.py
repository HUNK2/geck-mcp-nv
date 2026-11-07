"""Offline reader for Fallout: New Vegas save files (.fos): fnv_save_inspect and fnv_save_diff.

The format follows the game's own save code in the decomp (BGSSaveLoadGame::SaveGame 0x00847850, the header writer
0x0084d4b0, SavePluginList 0x00847590, the global-data dispatch 0x0084bcd0, the change-form writer 0x00865a30,
DetermineInitialDataType 0x0084e730, GetChangeFlagName 0x0083fef0, TESQuest's save writer 0x0060e810 and
ScriptLocals::SaveGame 0x005a9db0). Every value the game writes into a save buffer is followed by a '|' (0x7c) spacer.

Fully decoded: header, screenshot, plugins, player location, global variables, the change-form table (form, type,
change flags by name), reference positions (initial data) and quest bodies (stages, objectives, script variables).
Other bodies are kept as raw bytes and compared byte for byte.
"""
import struct
from pathlib import Path

MAGIC = b'FO3SAVEGAME'

# Compact change-form type index (DAT_011a2428) -> record signature.
CHANGE_TYPES = ['REFR', 'ACHR', 'ACRE', 'PMIS', 'PGRE', 'PBEA', 'PFLA', 'CELL', 'INFO', 'QUST', 'NPC_', 'CREA',
                'ACTI', 'TACT', 'TERM', 'ARMO', 'BOOK', 'CLOT', 'CONT', 'DOOR', 'INGR', 'LIGH', 'MISC', 'STAT',
                'MSTT', 'FURN', 'WEAP', 'AMMO', 'KEYM', 'ALCH', 'IDLM', 'NOTE', 'ECZN', 'CLAS', 'FACT', 'PACK',
                'NAVM', 'FLST', 'LVLC', 'LVLN', 'LVLI', 'WATR', 'IMOD', 'REPU', 'PCBE', 'RCPE', 'RCCT', 'CHIP',
                'CSNO', 'LSCT', 'CHAL', 'AMEF', 'CCRD', 'CMNY', 'CDCK']
REF_TYPES = {'REFR', 'ACHR', 'ACRE', 'PMIS', 'PGRE', 'PBEA', 'PFLA'}
ACTOR_TYPES = {'ACHR', 'ACRE'}

GLOBAL_TYPES = {0: 'misc stats', 1: 'player location', 2: 'TES (grid and cells)', 3: 'global variables',
                4: 'created objects', 5: 'combat', 6: 'interface', 7: 'image-space modifiers', 8: 'sky and weather',
                9: 'actor causes', 10: 'radio', 11: 'sound', 1000: 'process lists'}
GLOBAL_NAMES_UNCERTAIN = {4, 5, 6, 8, 9, 10, 11, 1000}

# Change flag names (GetChangeFlagName 0x0083fef0).
_REF_COMMON = {0x2: 'moved', 0x4: 'havok moved', 0x8: 'cell changed', 0x10: 'scale', 0x20: 'inventory',
               0x40: 'ownership', 0x4000000: 'activating children', 0x8000000: 'leveled inventory',
               0x10000000: 'animation', 0x20000000: 'encounter zone', 0x40000000: 'created only extra',
               0x80000000: 'game only extra'}
_REF_OBJECT = {0x400: 'item data', 0x800: 'ammo', 0x1000: 'lock', 0x20000: 'teleport', 0x200000: 'emptied',
               0x400000: 'open default state', 0x800000: 'open state'}
_REF_ACTOR = {0x400: 'life state', 0x800: 'package data', 0x1000: 'merchant container', 0x20000: 'dismembered limbs',
              0x40000: 'leveled actor', 0x80000: 'disposition modifiers', 0x100000: 'temporary modifiers',
              0x200000: 'damage modifiers', 0x400000: 'override modifiers', 0x800000: 'permanent modifiers'}
_ACTOR_BASE = {0x2: 'base data', 0x4: 'attributes', 0x8: 'AI data', 0x10: 'spell list', 0x20: 'full name'}
FLAG_NAMES = {
    'QUST': {0x2: 'quest flags', 0x4: 'script delay', 0x20000000: 'objectives', 0x40000000: 'script variables',
             0x80000000: 'stages'},
    'CELL': {0x2: 'cell flags', 0x4: 'full name', 0x8: 'ownership', 0x10000000: 'exterior short',
             0x20000000: 'exterior char', 0x40000000: 'detach time', 0x80000000: 'seen data'},
    'FACT': {0x2: 'faction flags', 0x4: 'reactions', 0x80000000: 'crime counts'},
    'NPC_': {**_ACTOR_BASE, **{0x200: 'skills', 0x400: 'class', 0x800: 'face', 0x1000000: 'gender',
                                 0x2000000: 'race'}},
    'CREA': {**_ACTOR_BASE, **{0x200: 'creature skills'}},
    'INFO': {0x80000000: 'said once'}, 'NOTE': {0x80000000: 'read'}, 'FLST': {0x80000000: 'added form'},
    'LVLC': {0x80000000: 'added object'}, 'LVLN': {0x80000000: 'added object'}, 'LVLI': {0x80000000: 'added object'},
    'WATR': {0x80000000: 'remapped'}, 'PACK': {0x40000000: 'waiting', 0x80000000: 'never run'},
    'ECZN': {0x2: 'zone flags', 0x80000000: 'game data'}, 'CLAS': {0x2: 'tag skills'},
    'BOOK': {0x2: 'value', 0x4: 'full name', 0x20: 'teaches skill'}, 'TACT': {0x800000: 'speaker'},
}
_DEFAULT_OBJECT = {0x2: 'value', 0x4: 'full name'}

# Initial-data sizes by type (DAT_011a2230).
INITIAL_SIZES = [0, 8, 10, 4, 27, 31, 34]
INITIAL_KINDS = {1: 'exterior cell (char)', 2: 'exterior cell (short)', 3: 'interior cell',
                 4: 'reference location', 5: 'created reference', 6: 'moved reference'}


class SaveFormatError(ValueError):
    code = 'SAVE_FORMAT'


def flag_names(rtype, flags):
    if rtype in REF_TYPES:
        table = {**_REF_COMMON, **(_REF_ACTOR if rtype in ACTOR_TYPES else _REF_OBJECT)}
    else:
        table = FLAG_NAMES.get(rtype, _DEFAULT_OBJECT)
    names = []
    if flags & 1:
        names.append('form flags')
    for bit, name in sorted(table.items()):
        if flags & bit:
            names.append(name)
    known = 1 | sum(table)
    rest = flags & ~known & 0x0000FFFF  # the game ORs 0xFFFF0000 into the stored mask of changed forms
    if rest:
        names.append('other 0x%X' % rest)
    return names


class Buffer:
    """A '|'-spaced save buffer, read the way BGSLoadGameBuffer::LoadRawData (0x00864820) reads it."""

    def __init__(self, data, ids=None):
        self.b, self.p, self.ids = data, 0, ids or []

    def raw(self, n, spacer=True):
        if self.p + n > len(self.b):
            raise SaveFormatError('buffer ended at byte %d' % self.p)
        value = self.b[self.p:self.p + n]
        self.p += n
        if spacer:
            if self.b[self.p:self.p + 1] != b'|':
                raise SaveFormatError('missing spacer at byte %d' % self.p)
            self.p += 1
        return value

    def u8(self):
        return self.raw(1)[0]

    def u16(self):
        return struct.unpack('<H', self.raw(2))[0]

    def u32(self):
        return struct.unpack('<I', self.raw(4))[0]

    def i32(self):
        return struct.unpack('<i', self.raw(4))[0]

    def f32(self):
        return struct.unpack('<f', self.raw(4))[0]

    def f64(self):
        return struct.unpack('<d', self.raw(8))[0]

    def form(self):
        return self.resolve(self.raw(3))

    def resolve(self, three):
        v = (three[0] << 16) | (three[1] << 8) | three[2]
        if v & 0x800000:
            return 0xFF000000 | (v & 0x7FFFFF)
        if v == 0:
            return 0
        return self.ids[v - 1] if v - 1 < len(self.ids) else None

    def count(self):
        if self.p >= len(self.b):
            raise SaveFormatError('buffer ended at byte %d' % self.p)
        size = (1, 2, 4)[self.b[self.p] & 3]
        return int.from_bytes(self.raw(size), 'little') >> 2

    def string(self):
        n = self.u16()
        return self.raw(n).decode('latin-1') if n else ''

    def done(self):
        return self.p >= len(self.b)


def fid(v):
    return None if v is None else '%08X' % v


class Save:
    def __init__(self, path):
        self.path = Path(path)
        data = self.path.read_bytes()
        if data[:11] != MAGIC:
            raise SaveFormatError('%s is not a Fallout New Vegas save (no FO3SAVEGAME magic)' % self.path.name)
        self.data = data
        size = struct.unpack_from('<I', data, 11)[0]
        h = Buffer(data[15:15 + size])
        self.header = {'version': h.u32(), 'language': h.raw(64).split(b'\0')[0].decode('latin-1')}
        self.width, self.height = h.u32(), h.u32()
        self.header.update({'saveNumber': h.u32(), 'playerName': h.string(), 'karmaTitle': h.string(),
                            'level': h.u32(), 'location': h.string(), 'playTime': h.string()})
        p = 15 + size
        self.shot_offset = p
        p += self.width * self.height * 3
        self.form_version = data[p]
        p += 1
        plist_size = struct.unpack_from('<I', data, p)[0]
        pl = Buffer(data[p + 4:p + 4 + plist_size])
        self.plugins = [pl.string() for _ in range(pl.u8())]
        p += 4 + plist_size
        (self.ofs_ids, self.ofs_history, self.ofs_globals, self.ofs_changes, self.ofs_globals2,
         self.n_globals, self.n_globals2, self.n_changes) = struct.unpack_from('<8I', data, p)
        n = struct.unpack_from('<I', data, self.ofs_ids)[0]
        self.ids = list(struct.unpack_from('<%dI' % n, data, self.ofs_ids + 4))
        self.globals = self._globals(self.ofs_globals, self.n_globals) + self._globals(self.ofs_globals2, self.n_globals2)
        self.changes = self._changes()

    # ------------------------------------------------------------------ pieces
    def _globals(self, p, n):
        out = []
        for _ in range(n):
            gtype, size = struct.unpack_from('<II', self.data, p)
            out.append((gtype, self.data[p + 8:p + 8 + size]))
            p += 8 + size
        return out

    def _changes(self):
        d, p, out = self.data, self.ofs_changes, []
        for _ in range(self.n_changes):
            three = d[p:p + 3]
            flags, tbyte, version = struct.unpack_from('<IBB', d, p + 3)
            p += 9
            width = (1, 2, 4)[tbyte >> 6]
            length = int.from_bytes(d[p:p + width], 'little')
            p += width
            index = tbyte & 0x3F
            rtype = CHANGE_TYPES[index] if index < len(CHANGE_TYPES) else 'type%d' % index
            out.append({'form': Buffer(b'', self.ids).resolve(three), 'type': rtype, 'flags': flags,
                        'version': version, 'body': d[p:p + length]})
            p += length
        return out

    def plugin_of(self, form):
        if form is None:
            return None
        if form >= 0xFF000000:
            return '(created in game)'
        index = form >> 24
        return self.plugins[index] if index < len(self.plugins) else None

    def screenshot_png(self, out_path):
        from PIL import Image
        raw = self.data[self.shot_offset:self.shot_offset + self.width * self.height * 3]
        Image.frombytes('RGB', (self.width, self.height), raw).save(out_path)
        return str(out_path)

    # ------------------------------------------------------------------ decoders
    def player_location(self):
        body = next((b for t, b in self.globals if t == 1), None)
        if body is None:
            return None
        r = Buffer(body, self.ids)
        try:
            out = {'nextCreatedId': fid(r.u32()), 'worldspace': fid(r.form()), 'gridX': r.i32(), 'gridY': r.i32(),
                   'cell': fid(r.form())}
            x, y, z = struct.unpack('<3f', r.raw(12))
            out['position'] = {'x': round(x, 2), 'y': round(y, 2), 'z': round(z, 2)}
            return out
        except SaveFormatError as e:
            return {'error': str(e)}

    def global_variables(self):
        body = next((b for t, b in self.globals if t == 3), None)
        if body is None:
            return {}
        r = Buffer(body, self.ids)
        out = {}
        for _ in range(r.count()):
            form = r.form()
            out[fid(form)] = r.f32()
        return out

    def initial_kind(self, change):
        rtype, flags, form = change['type'], change['flags'], change['form']
        if rtype in REF_TYPES:
            if form is not None and form >= 0xFF000000:
                return 5
            if flags & 0x8:
                return 6
            if flags & 0x6:
                return 4
            return 0
        if rtype == 'CELL' and flags & 0x40000000:
            if flags & 0x20000000:
                return 1
            if flags & 0x10000000:
                return 2
            return 3
        return 0

    def decode(self, change):
        """Decoded body: {'initial': ..., 'fields': ...}; raises SaveFormatError on a body it cannot read."""
        r = Buffer(change['body'], self.ids)
        out = {}
        kind = self.initial_kind(change)
        if kind:
            block = r.raw(INITIAL_SIZES[kind])
            out['initial'] = self._initial(kind, block, r)
        if change['type'] == 'QUST':
            out['quest'] = self._quest(r, change['flags'], change['version'])
            out['complete'] = r.done()
        elif change['type'] not in ACTOR_TYPES and change['type'] in REF_TYPES and change['flags'] & 1:
            out['formFlags'] = '%08X' % r.u32()
            if change['flags'] & 0x10:
                out['scale'] = round(r.f32(), 4)
        elif change['type'] not in REF_TYPES and change['flags'] & 1:
            out['formFlags'] = '%08X' % r.u32()
        return out

    def _initial(self, kind, block, r):
        info = {'kind': INITIAL_KINDS[kind]}
        if kind in (4, 5, 6):
            info['cell'] = fid(r.resolve(block[0:3]))
            px, py, pz, ax, ay, az = struct.unpack_from('<6f', block, 3)
            info['position'] = {'x': round(px, 2), 'y': round(py, 2), 'z': round(pz, 2)}
            info['rotation'] = {'x': round(ax, 4), 'y': round(ay, 4), 'z': round(az, 4)}
            if kind == 5:
                info['base'] = fid(r.resolve(block[27:30]))
                info['createdFlags'] = block[30]
            if kind == 6:
                info['originalCell'] = fid(r.resolve(block[27:30]))
                info['cellX'], info['cellY'] = struct.unpack_from('<hh', block, 30)
        elif kind == 3:
            info['detachTime'] = struct.unpack_from('<I', block, 0)[0]
        else:
            info['bytes'] = block.hex()
        return info

    def _quest(self, r, flags, version):
        q = {}
        if flags & 1:
            q['formFlags'] = '%08X' % r.u32()
        if flags & 2:
            value = r.u8()
            q['questFlags'] = value
            q['running'] = bool(value & 1)
            q['completed'] = bool(value & 2)
        if flags & 4:
            q['scriptDelay'] = round(r.f32(), 4)
        if flags & 0x80000000:
            stages = []
            for _ in range(r.count()):
                index, done = r.u8(), r.u8()
                entries = []
                for _ in range(r.count()):
                    entry, has_date = r.u8(), r.u8()
                    entries.append({'logEntry': entry, 'date': r.u32() if has_date else None})
                stages.append({'stage': index, 'done': bool(done), 'logEntries': entries})
            q['stages'] = stages
        if flags & 0x40000000:
            variables = {}
            for _ in range(r.count()):
                vid = r.u32()
                variables[vid & 0x7FFFFFFF] = {'ref': fid(r.form())} if vid & 0x80000000 else r.f64()
            if r.u8():
                r.f64()
            if version >= 0x15:
                r.u8()
            q['scriptVariables'] = variables
        if flags & 0x20000000:
            q['objectives'] = [{'index': r.u32(), 'state': r.u32()} for _ in range(r.count())]
        return q


class Namer:
    """Editor IDs and script variable names for a save's forms, from the bridge's plugin record index
    (fnv_data). A save's form ids carry the save's own plugin index, mapped through its plugin list."""

    def __init__(self, data):
        self.data = data
        self._vars = {}
        self.ready = False
        try:
            data._active()  # make sure the active plugins are indexed
            self.ready = True
        except Exception:
            pass

    def edid(self, save, form):
        if not self.ready or form is None or form >= 0xFF000000:
            return None
        high = form >> 24
        if high >= len(save.plugins):
            return None
        row = self.data.index.db.execute('select edid from records where owner = ? and local = ? limit 1',
                                         (save.plugins[high].lower(), form & 0xFFFFFF)).fetchone()
        return row[0] if row else None

    def variables(self, form):
        """{index: name} of the quest's script, from its SCRI script record (SLSD index, then SCVR name)."""
        if form in self._vars:
            return self._vars[form]
        names = {}
        try:
            quest = self.data.record_show('%08X' % form, all_versions=False)
            script = next((f['form']['formId'] for f in quest['fields'] if f['tag'] == 'SCRI' and f.get('form')), None)
            if script:
                index = None
                for f in self.data.record_show(script, all_versions=False)['fields']:
                    if f['tag'] == 'SLSD' and isinstance(f.get('value'), dict):
                        index = f['value']['index']
                    elif f['tag'] == 'SCVR' and index is not None:
                        names[index] = f.get('value')
                        index = None
        except Exception:
            pass
        self._vars[form] = names
        return names


def _name_variables(out, names):
    quest = out.get('quest') or {}
    if names and quest.get('scriptVariables'):
        quest['scriptVariables'] = {(names[i] if i in names else 'var%d' % i): v
                                    for i, v in quest['scriptVariables'].items()}


def summarise_change(save, change, decode=True, namer=None):
    out = {'form': fid(change['form']), 'type': change['type'], 'plugin': save.plugin_of(change['form']),
           'flags': '%08X' % change['flags'], 'changed': flag_names(change['type'], change['flags']),
           'version': change['version'], 'bytes': len(change['body'])}
    if namer:
        out['editorId'] = namer.edid(save, change['form'])
    if decode:
        try:
            out.update(save.decode(change))
        except SaveFormatError as e:
            out['decodeError'] = str(e)
        if namer and change['type'] == 'QUST':
            _name_variables(out, namer.variables(change['form']))
    return out


def _form_filter(forms):
    if not forms:
        return None
    return {f.upper().removeprefix('0X').zfill(8) for f in forms}


def inspect(path, types=None, forms=None, limit=100, offset=0, screenshot_to=None, include_globals=True, namer=None):
    save = Save(path)
    wanted_types = {t.upper() for t in types} if types else None
    wanted_forms = _form_filter(forms)
    counts = {}
    for c in save.changes:
        counts[c['type']] = counts.get(c['type'], 0) + 1
    selected = [c for c in save.changes
                if (not wanted_types or c['type'] in wanted_types) and
                (not wanted_forms or fid(c['form']) in wanted_forms)]
    out = {'file': str(save.path), 'bytes': len(save.data), 'header': save.header,
           'screenshot': {'width': save.width, 'height': save.height}, 'formVersion': save.form_version,
           'plugins': [{'index': '%02X' % i, 'name': n} for i, n in enumerate(save.plugins)],
           'playerLocation': save.player_location(),
           'globalRecords': [{'type': t, 'name': GLOBAL_TYPES.get(t, '?'), 'bytes': len(b),
                              'nameUncertain': t in GLOBAL_NAMES_UNCERTAIN} for t, b in save.globals],
           'changeFormCounts': dict(sorted(counts.items(), key=lambda kv: -kv[1])),
           'changeFormsTotal': len(save.changes), 'matched': len(selected), 'offset': offset,
           'changeForms': [summarise_change(save, c, namer=namer) for c in selected[offset:offset + limit]]}
    if include_globals:
        values = save.global_variables()
        if namer:
            out['globalVariables'] = {k: {'value': v, 'editorId': namer.edid(save, int(k, 16))} for k, v in values.items()}
        else:
            out['globalVariables'] = values
    if screenshot_to:
        out['screenshot']['path'] = save.screenshot_png(screenshot_to)
    out['source'] = ('format from the game save code in the decomp: BGSSaveLoadGame::SaveGame 0x00847850, '
                     'change forms 0x00865a30, quest bodies 0x0060e810')
    return out


def _diff_values(a, b, path=''):
    """Leaf differences between two decoded structures."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b), key=str):
            out += _diff_values(a.get(key), b.get(key), '%s.%s' % (path, key) if path else str(key))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += _diff_values(x, y, '%s[%d]' % (path, i))
        return out
    return [] if a == b else [{'field': path, 'before': a, 'after': b}]


def diff(path_a, path_b, types=None, forms=None, limit=200, namer=None):
    a, b = Save(path_a), Save(path_b)
    wanted_types = {t.upper() for t in types} if types else None
    wanted_forms = _form_filter(forms)

    def keyed(save):
        return {(fid(c['form']), c['type']): c for c in save.changes
                if (not wanted_types or c['type'] in wanted_types) and (not wanted_forms or fid(c['form']) in wanted_forms)}
    ka, kb = keyed(a), keyed(b)
    added, removed, changed = [], [], []
    for key in kb.keys() - ka.keys():
        added.append(summarise_change(b, kb[key], namer=namer))
    for key in ka.keys() - kb.keys():
        removed.append(summarise_change(a, ka[key], decode=False, namer=namer))
    for key in ka.keys() & kb.keys():
        ca, cb = ka[key], kb[key]
        if ca['flags'] == cb['flags'] and ca['body'] == cb['body']:
            continue
        entry = {'form': key[0], 'type': key[1], 'plugin': b.plugin_of(cb['form'])}
        if namer:
            entry['editorId'] = namer.edid(b, cb['form'])
        if ca['flags'] != cb['flags']:
            entry['flagsBefore'], entry['flagsAfter'] = '%08X' % ca['flags'], '%08X' % cb['flags']
            entry['newlyChanged'] = [n for n in flag_names(key[1], cb['flags']) if n not in flag_names(key[1], ca['flags'])]
        try:
            body_a, body_b = a.decode(ca), b.decode(cb)
            if namer and key[1] == 'QUST':
                names = namer.variables(cb['form'])
                _name_variables(body_a, names)
                _name_variables(body_b, names)
            fields = _diff_values(body_a, body_b)
        except SaveFormatError as e:
            fields, entry['decodeError'] = [], str(e)
        if fields:
            entry['fields'] = fields
        if ca['body'] != cb['body']:
            same = sum(1 for x, y in zip(ca['body'], cb['body']) if x == y)
            entry['bodyBytes'] = {'before': len(ca['body']), 'after': len(cb['body']),
                                  'bytesDiffering': max(len(ca['body']), len(cb['body'])) - same}
            entry['bodyDecoded'] = bool(fields) and key[1] == 'QUST'
        changed.append(entry)
    order = lambda e: (e['type'], e['form'] or '')  # noqa: E731
    ga, gb = a.global_variables(), b.global_variables()
    globals_changed = [{'global': k, 'editorId': namer.edid(b, int(k, 16)) if namer else None,
                        'before': ga.get(k), 'after': gb.get(k)}
                       for k in sorted(set(ga) | set(gb)) if ga.get(k) != gb.get(k)]
    other_globals = []
    da, db = dict(a.globals), dict(b.globals)
    for t in sorted(set(da) | set(db)):
        if t != 3 and da.get(t) != db.get(t):
            other_globals.append({'type': t, 'name': GLOBAL_TYPES.get(t, '?'),
                                  'bytesBefore': len(da.get(t) or b''), 'bytesAfter': len(db.get(t) or b'')})
    return {
        'before': {'file': str(a.path), 'header': a.header},
        'after': {'file': str(b.path), 'header': b.header},
        'header': _diff_values(a.header, b.header),
        'pluginsAdded': [p for p in b.plugins if p not in a.plugins],
        'pluginsRemoved': [p for p in a.plugins if p not in b.plugins],
        'playerLocation': _diff_values(a.player_location(), b.player_location()),
        'globalVariables': globals_changed,
        'otherGlobalData': other_globals,
        'counts': {'added': len(added), 'removed': len(removed), 'changed': len(changed)},
        'changedByType': _by_type(changed), 'addedByType': _by_type(added), 'removedByType': _by_type(removed),
        'added': sorted(added, key=order)[:limit],
        'removed': sorted(removed, key=order)[:limit],
        'changed': sorted(changed, key=order)[:limit],
        'note': 'Quest bodies and reference positions are compared field by field; other bodies byte for byte '
                '(their layouts are not decoded yet). Identical saves give empty lists.',
    }


def _by_type(entries):
    out = {}
    for e in entries:
        out[e['type']] = out.get(e['type'], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def resolve_path(name, saves_dir):
    """A save by path, file name, name without .fos, or test-save name."""
    p = Path(name)
    if p.is_absolute() or p.exists():
        return p
    for candidate in (name, name + '.fos', 'fnvmcp_test_' + name + '.fos'):
        q = Path(saves_dir) / candidate
        if q.exists():
            return q
    raise FileNotFoundError('No save %r in %s' % (name, saves_dir))

