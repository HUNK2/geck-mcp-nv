"""Plugin data and authoring (fnv-mcp milestone 8): load order, records across it, script lint, in-game
compile and script hot-swap.

Offline (no game needed):
  fnv_loadorder     the MO2 dev profile's plugins in order, with the real file behind MO2's virtual folder
  fnv_record_find   records in the whole load order by EditorID, form id, type or name
  fnv_record_show   one record decoded, with every plugin that edits it and what each changed
  fnv_script_lint   script source checked against the game's command tables (vanilla, xNVSE, JIP LN)
Live (engine companion):
  fnv_compile_ingame  compile script source with the running game's compiler: errors, variables, bytecode
  fnv_script_hot_swap replace a running script's code with a new compile, keeping its variables (undoable)

The record index is a SQLite cache in state/fnv/records.sqlite, rebuilt per plugin file when its size or
time changes. Plugins are read with geck-mcp-nv's codec (esp/codec.py) for decoding single records; the index
scan reads record headers and the first subrecords only.
"""
import json
import re
import sqlite3
import struct
import time
import zlib
from pathlib import Path

from .esp import codec
from .fnv_facts import ACTOR_VALUES, SCRIPT_BLOCKS

KNOWN_NAMES = {v.lower() for v in ACTOR_VALUES} | {'x', 'y', 'z', 'player', 'playerref'}  # AVs, axes, the player
BLOCK_ALIASES = {'onpackageend'}  # short names in g_scriptBlocks (OnPackageDone = OnPackageEnd)

INDEX_VERSION = 1
SKIP_EDID = {b'LAND', b'NAVM', b'PGRE', b'PMIS', b'NAVI'}  # no EditorIDs worth reading, many of them
FORM_TYPES_FOR_PARAM = {
    # command parameter type -> record types an EditorID argument may name
    'Quest': {'QUST'}, 'Faction': {'FACT'}, 'Cell': {'CELL'}, 'Global': {'GLOB'}, 'Topic': {'DIAL'},
    'Perk': {'PERK'}, 'Race': {'RACE'}, 'Class': {'CLAS'}, 'Message': {'MESG'}, 'AIPackage': {'PACK'},
    'MagicEffect': {'MGEF'}, 'SpellItem': {'SPEL'}, 'FormList': {'FLST'}, 'Reputation': {'REPU'},
    'Challenge': {'CHAL'}, 'Casino': {'CSNO'}, 'Container': {'CONT', 'REFR'}, 'NPC': {'NPC_'},
    'ActorBase': {'NPC_', 'CREA'}, 'Actor': {'ACHR', 'ACRE'}, 'ObjectRef': {'REFR', 'ACHR', 'ACRE', 'PGRE', 'PMIS'},
    'MapMarker': {'REFR'}, 'Sound': {'SOUN'}, 'ImageSpaceModifier': {'IMAD'}, 'WorldSpace': {'WRLD'},
    'Note': {'NOTE'}, 'Weather': {'WTHR'}, 'Idle': {'IDLE'}, 'EncounterZone': {'ECZN'},
}
DECLARATIONS = ('short', 'int', 'long', 'float', 'ref', 'reference', 'array_var', 'string_var')
FLOW_WORDS = {'if', 'elseif', 'else', 'endif', 'set', 'let', 'begin', 'end', 'return', 'scn', 'scriptname',
              'while', 'loop', 'foreach', 'continue', 'break', 'call', 'to'}


class DataError(Exception):
    def __init__(self, code, message, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence


# ===================================================================================== load order
def load_order(project, live_plugins=None):
    """[{index, name, path, mod, active, masters}] from the MO2 dev profile (or the running game's list)."""
    fnv = project.fnv
    if not fnv or not fnv.mo2_root or not fnv.mo2_profile:
        raise DataError('NO_MO2_PROFILE', 'Set fnv.mo2_root and fnv.mo2_profile in the project file')
    root, profile = Path(fnv.mo2_root), Path(fnv.mo2_root) / 'profiles' / fnv.mo2_profile
    data_dir = Path(project.game_root) / 'Data'

    def lines(name):
        path = profile / name
        if not path.exists():
            return []
        return [l.strip() for l in path.read_text(encoding='utf-8', errors='replace').splitlines()
                if l.strip() and not l.startswith('#')]

    mods = lines('modlist.txt')
    enabled = [m[1:] for m in mods if m[:1] == '+']  # top = highest priority
    dlc = {m[1:].split(':', 1)[1].strip().lower() for m in mods if m[:1] == '*' and ':' in m}
    active = {p.lstrip('*').lower() for p in lines('plugins.txt')}
    order = lines('loadorder.txt') or [p.lstrip('*') for p in lines('plugins.txt')]

    def locate(name):
        for mod in enabled:
            candidate = root / 'mods' / mod / name
            if candidate.exists():
                return candidate, mod
        candidate = data_dir / name
        return (candidate, None) if candidate.exists() else (None, None)

    out, index = [], 0
    live = {p.lower(): i for p, i in (live_plugins or {}).items()} if live_plugins else None
    for name in order:
        stem = name.rsplit('.', 1)[0].lower()
        is_active = (name.lower() in active or stem in dlc or name.lower() == 'falloutnv.esm') if live is None \
            else name.lower() in live
        path, mod = locate(name)
        entry = {'name': name, 'active': is_active, 'path': str(path) if path else None, 'mod': mod}
        if is_active:
            entry['index'] = live[name.lower()] if live is not None else index
            entry['prefix'] = '%02X' % entry['index']
            index += 1
        if path:
            try:
                entry['masters'] = plugin_masters(path)
            except (OSError, codec.FormatError) as e:
                entry['error'] = str(e)
        else:
            entry['error'] = 'file not found in the MO2 mods or the game Data folder'
        out.append(entry)
    names = {e['name'].lower() for e in out if e['active']}
    for e in out:
        missing = [m for m in e.get('masters', []) if m.lower() not in names]
        if e['active'] and missing:
            e['missingMasters'] = missing
    return out


def plugin_masters(path):
    with open(path, 'rb') as f:
        head = f.read(24)
        if head[:4] != b'TES4':
            raise codec.FormatError('not a TES4 plugin', 0)
        size = struct.unpack_from('<I', head, 4)[0]
        data = f.read(size)
    return [codec.cstring(s.data) for s in codec.parse_subrecords(data) if s.tag == 'MAST']


# ===================================================================================== record index
class RecordIndex:
    def __init__(self, state_dir):
        path = Path(state_dir) / 'fnv' / 'records.sqlite'
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript('''
            create table if not exists files(id integer primary key, path text unique, name text, size integer,
                mtime real, version integer, masters text, records integer, built real);
            create table if not exists records(file integer, owner text, local integer, type text, edid text,
                full text, offset integer, flags integer);
            create index if not exists records_owner on records(owner, local);
            create index if not exists records_edid on records(edid collate nocase);
            create index if not exists records_file on records(file);
        ''')

    def ensure(self, path):
        """Index a plugin file unless the cached index matches its size and time. Returns its file id."""
        path = Path(path)
        st = path.stat()
        row = self.db.execute('select id, size, mtime, version from files where path = ?', (str(path),)).fetchone()
        if row and row[1] == st.st_size and abs(row[2] - st.st_mtime) < 1e-3 and row[3] == INDEX_VERSION:
            return row[0]
        if row:
            self.db.execute('delete from records where file = ?', (row[0],))
            self.db.execute('delete from files where id = ?', (row[0],))
        masters, rows = scan_plugin(path)
        cur = self.db.execute('insert into files(path, name, size, mtime, version, masters, records, built) '
                              'values (?, ?, ?, ?, ?, ?, ?, ?)', (str(path), path.name, st.st_size, st.st_mtime,
                                                                  INDEX_VERSION, json.dumps(masters), len(rows), time.time()))
        file_id = cur.lastrowid
        own = masters + [path.name]
        self.db.executemany('insert into records values (?, ?, ?, ?, ?, ?, ?, ?)',
                            ((file_id, own[min(fid >> 24, len(own) - 1)].lower(), fid & 0xFFFFFF, rtype, edid, full, off, flags)
                             for fid, rtype, edid, full, off, flags in rows))
        self.db.commit()
        return file_id

    def file(self, file_id):
        row = self.db.execute('select path, name, masters from files where id = ?', (file_id,)).fetchone()
        return {'path': row[0], 'name': row[1], 'masters': json.loads(row[2])}


def scan_plugin(path):
    """(masters, [(raw form id, type, EditorID, FULL, offset, flags)]) from a flat walk over record headers."""
    data = Path(path).read_bytes()
    if data[:4] != b'TES4':
        raise codec.FormatError('not a TES4 plugin', 0)
    header_size = struct.unpack_from('<I', data, 4)[0]
    masters = [codec.cstring(s.data) for s in codec.parse_subrecords(data[24:24 + header_size]) if s.tag == 'MAST']
    rows, pos, end = [], 24 + header_size, len(data)
    unpack = struct.Struct('<4sIII').unpack_from
    while pos + 24 <= end:
        tag, size, flags, form_id = unpack(data, pos)
        if tag == b'GRUP':
            pos += 24  # a group's contents follow its header: keep walking inside it
            continue
        body = pos + 24
        edid = full = None
        if tag not in SKIP_EDID and size:
            if flags & codec.COMPRESSED:
                try:
                    head = zlib.decompressobj().decompress(data[body + 4:body + size], 2048)
                except zlib.error:
                    head = b''
            else:
                head = data[body:body + min(size, 2048)]
            edid, full = _first_strings(head)
        rows.append((form_id, tag.decode('latin-1'), edid, full, pos, flags))
        pos = body + size
    return masters, rows


def _first_strings(head):
    edid = full = None
    pos = 0
    while pos + 6 <= len(head) and (edid is None or full is None):
        tag, size = head[pos:pos + 4], struct.unpack_from('<H', head, pos + 4)[0]
        value = head[pos + 6:pos + 6 + size]
        if tag == b'EDID':
            edid = value.rstrip(b'\0').decode('cp1252', 'replace')
        elif tag == b'FULL' and size and len(value) == size:
            full = value.rstrip(b'\0').decode('cp1252', 'replace')
        elif tag == b'XXXX':
            break
        pos += 6 + size
    return edid, full


# ===================================================================================== the service
class DataTools:
    def __init__(self, project, live=None):
        self.project = project
        self.live = live
        self._index = None

    @property
    def index(self):
        if self._index is None:
            self._index = RecordIndex(self.project.state_dir)
        return self._index

    def _live_plugins(self):
        if not self.live:
            return None
        try:
            game = self.live.game
            if not game.process() or game.ping().get('error'):
                return None
            plugins = game.loaded_plugins() or []
            return {p['name']: int(p['index'], 16) for p in plugins if p.get('name') and p.get('index')} or None
        except Exception:
            return None

    # ------------------------------------------------------------------ fnv_loadorder
    def loadorder(self):
        live = self._live_plugins()
        entries = load_order(self.project, live)
        return {'source': 'running game' if live else 'MO2 dev profile (loadorder.txt, plugins.txt)',
                'plugins': entries, 'active': sum(1 for e in entries if e['active']),
                'problems': [e for e in entries if e.get('missingMasters') or (e['active'] and e.get('error'))]}

    def _active(self):
        entries = [e for e in load_order(self.project, self._live_plugins()) if e['active'] and e.get('path')]
        started = time.monotonic()
        built = []
        for e in entries:
            before = self.index.db.total_changes
            e['fileId'] = self.index.ensure(e['path'])
            if self.index.db.total_changes != before:
                built.append(e['name'])
        return entries, {'indexed': built, 'seconds': round(time.monotonic() - started, 1)} if built else None

    @staticmethod
    def _global(entries, owner, local):
        for e in entries:
            if e['name'].lower() == owner:
                return '%02X%06X' % (e['index'], local)
        return None

    # ------------------------------------------------------------------ fnv_record_find
    def record_find(self, query=None, record_type=None, plugin=None, limit=25, offset=0):
        entries, built = self._active()
        by_file = {e['fileId']: e for e in entries}
        clauses, args = ['file in (%s)' % ','.join(str(i) for i in by_file) or '0'], []
        q = (query or '').strip()
        m = re.match(r'^(?:0x)?([0-9A-Fa-f]{8})$', q)
        local = re.match(r'^(.+\.es[mp]):(?:0x)?([0-9A-Fa-f]{1,6})$', q, re.I)
        if m:
            value = int(m.group(1), 16)
            owner = next((e['name'].lower() for e in entries if e['index'] == value >> 24), None)
            clauses.append('owner = ? and local = ?')
            args += [owner, value & 0xFFFFFF]
        elif local:
            clauses.append('owner = ? and local = ?')
            args += [local.group(1).lower(), int(local.group(2), 16)]
        elif q:
            clauses.append('(edid like ? or full like ?)')
            args += ['%' + q.replace('%', '') + '%'] * 2
        if record_type:
            clauses.append('type = ?')
            args.append(record_type.upper())
        if plugin:
            target = next((e for e in entries if e['name'].lower() == plugin.lower()), None)
            if not target:
                raise DataError('PLUGIN_NOT_ACTIVE', '%s is not in the active load order' % plugin)
            clauses.append('file = ?')
            args.append(target['fileId'])
        sql = ('select owner, local, type, edid, full, count(*) as versions, max(file) from records where %s '
               'group by owner, local order by edid is null, length(edid), edid limit ? offset ?' % ' and '.join(clauses))
        rows = self.index.db.execute(sql, args + [int(limit), int(offset)]).fetchall()
        order = {e['fileId']: e['index'] for e in entries}
        out = []
        for owner, local, rtype, edid, full, versions, _ in rows:
            files = [f for (f,) in self.index.db.execute('select file from records where owner = ? and local = ?',
                                                        (owner, local)) if f in by_file]
            files.sort(key=lambda f: order[f])
            out.append({'formId': self._global(entries, owner, local), 'type': rtype, 'editorId': edid, 'name': full,
                        'definedIn': by_file[files[0]]['name'] if files else owner,
                        'winner': by_file[files[-1]]['name'] if files else None, 'overrides': max(0, len(files) - 1)})
        return {'results': out, 'count': len(out), 'indexBuilt': built,
                'note': 'Searches EditorIDs and names in every active plugin of the load order.'}

    # ------------------------------------------------------------------ fnv_record_show
    def _resolve(self, entries, ref):
        ref = ref.strip()
        m = re.match(r'^(?:0x)?([0-9A-Fa-f]{8})$', ref)
        local = re.match(r'^(.+\.es[mp]):(?:0x)?([0-9A-Fa-f]{1,6})$', ref, re.I)
        if m:
            value = int(m.group(1), 16)
            owner = next((e['name'].lower() for e in entries if e['index'] == value >> 24), None)
            return owner, value & 0xFFFFFF
        if local:
            return local.group(1).lower(), int(local.group(2), 16)
        files = [e['fileId'] for e in entries]
        row = self.index.db.execute('select owner, local from records where edid = ? collate nocase and file in (%s) '
                                    'limit 1' % ','.join(map(str, files)), (ref,)).fetchone()
        if not row:
            raise DataError('RECORD_NOT_FOUND', 'No record %r in the active load order' % ref)
        return row

    def record_show(self, ref, all_versions=True, max_bytes=256):
        entries, built = self._active()
        owner, local = self._resolve(entries, ref)
        by_file = {e['fileId']: e for e in entries}
        rows = self.index.db.execute('select file, offset from records where owner = ? and local = ?',
                                     (owner, local)).fetchall()
        rows = sorted([r for r in rows if r[0] in by_file], key=lambda r: by_file[r[0]]['index'])
        if not rows:
            raise DataError('RECORD_NOT_FOUND', 'No record %s:%06X in the active load order' % (owner, local))
        versions = []
        for file_id, off in rows:
            e = by_file[file_id]
            record = read_record(e['path'], off)
            versions.append({'plugin': e['name'], 'record': record,
                             'subrecords': self._decode(record, e, entries, max_bytes)})
        winner = versions[-1]
        changes = []
        for older, newer in zip(versions, versions[1:]):
            changes.append({'plugin': newer['plugin'], 'over': older['plugin'],
                            'changed': _changed_tags(older['record'], newer['record'])})
        result = {'formId': self._global(entries, owner, local), 'type': winner['record'].type,
                  'editorId': winner['record'].editor_id, 'winner': winner['plugin'],
                  'definedIn': versions[0]['plugin'], 'versions': [v['plugin'] for v in versions],
                  'fields': winner['subrecords'], 'overrideChanges': changes, 'indexBuilt': built}
        if all_versions and len(versions) > 1:
            result['earlierVersions'] = [{'plugin': v['plugin'], 'fields': v['subrecords']} for v in versions[:-1]]
        return result

    def _form(self, entries, plugin_entry, raw):
        """A raw form id inside a plugin -> its load-order form id and EditorID (when indexed)."""
        own = plugin_entry.get('masters', []) + [plugin_entry['name']]
        high = raw >> 24
        if raw == 0 or high >= len(own) + 1:
            return None
        owner = own[min(high, len(own) - 1)].lower()
        row = self.index.db.execute('select type, edid from records where owner = ? and local = ? limit 1',
                                    (owner, raw & 0xFFFFFF)).fetchone()
        if not row:
            return None
        return {'formId': self._global(entries, owner, raw & 0xFFFFFF), 'type': row[0], 'editorId': row[1]}

    def _decode(self, record, plugin_entry, entries, max_bytes):
        out = []
        for sub in record.subrecords:
            item = {'tag': sub.tag, 'size': len(sub.data)}
            decoded = decode_subrecord(record.type, sub.tag, sub.data)
            if decoded is not None:
                item['value'] = decoded
            elif len(sub.data) == 4 and sub.tag not in NOT_FORMS:
                form = self._form(entries, plugin_entry, struct.unpack('<I', sub.data)[0])
                if form:
                    item['form'] = form
            if sub.tag == 'CNTO' and len(sub.data) == 8:
                raw, count = struct.unpack('<Ii', sub.data)
                item['value'] = {'item': self._form(entries, plugin_entry, raw) or '%08X' % raw, 'count': count}
            if sub.tag == 'SCRO' and len(sub.data) == 4:
                item['form'] = self._form(entries, plugin_entry, struct.unpack('<I', sub.data)[0])
            if 'value' not in item and 'form' not in item:
                item['hex'] = sub.data[:max_bytes].hex(' ') + (' ...' if len(sub.data) > max_bytes else '')
            out.append(item)
        return out

    def script_text(self, form_id):
        """A script's source text (SCTX) from the winning plugin record, or None."""
        try:
            record = self.record_show(form_id, all_versions=False)
        except DataError:
            return None
        return next((f['value'] for f in record['fields'] if f['tag'] == 'SCTX' and isinstance(f.get('value'), str)), None)

    # ------------------------------------------------------------------ fnv_scripts_compile
    def compile_spec_scripts(self, only=None):
        """Compile the project spec's scripts with the running game and store them in geck-mcp-nv's compiled-script
        cache (state/scripts/<project>.json), so its builder emits compiled scripts without GECK.

        Needs the last build of the plugin loaded in the game (its own forms must exist to be referenced). Each
        script's bytecode, variables and references come from the in-game compile: xNVSE writes a scriptname
        statement before compiling, so the first 4 bytes are dropped to get GECK's layout; runtime form ids are
        mapped back to the plugin's master-relative ids. Each entry is checked with the builder's own
        verify_script and carries an in-game provenance; the builder reuses it only while the sources, spec and
        build inputs stay the same (its compile context)."""
        from .authoring import Authoring, atomic_write
        from .esp import generic, scripts as esp_scripts
        live = self.live
        live.in_game()
        authoring = Authoring(self.project)
        spec = authoring.load_spec()
        receipt = authoring.receipt()
        plugin, masters = spec['plugin'], spec['masters']
        loaded = {p['name'].lower(): int(p['index'], 16) for p in (live.game.loaded_plugins() or []) if p.get('name')}
        if plugin.lower() not in loaded:
            raise DataError('PLUGIN_NOT_LOADED', '%s is not loaded in the game: install the build (fnv_install) and '
                            'restart, so the scripts can reference its forms' % plugin)
        by_index = {index: name for name, index in loaded.items()}
        own = [m.lower() for m in masters] + [plugin.lower()]

        def relative(form_id):
            value = int(form_id, 16)
            name = by_index.get(value >> 24)
            if name not in own:
                raise DataError('FOREIGN_FORM', 'The script references %s from %s, which is not a master of %s'
                                % (form_id, name, plugin))
            return (own.index(name) << 24) | (value & 0xFFFFFF)

        context = authoring.compile_context(spec)
        cache = esp_scripts.load_cache(self.project.state_dir, spec['project_id'])
        results, changed = {}, False
        for edid, rec in spec.get('records', {}).items():
            if rec.get('type') != 'SCPT' or (only and edid not in only):
                continue
            stype = generic.SCRIPT_TYPES[rec.get('script_type', 'object')]
            text = generic.script_text(authoring.spec_path.parent, rec, edid, 'records.%s' % edid)
            form_id = receipt['formIds'].get(edid)
            compiled = self.compile_ingame(text.decode('cp1252').replace('\r\n', '\n'), include_bytecode=True)
            if not compiled['compiled']:
                results[edid] = {'compiled': False, 'errors': compiled['errors'], 'hint': compiled.get('hint'),
                                 'lint': compiled.get('lint')}
                continue
            data = bytes.fromhex(compiled['bytecode'])
            if data[:8] != b'\x1d\0\0\0\x1d\0\0\0':
                results[edid] = {'compiled': False, 'errors': ['unexpected bytecode start %s' % data[:8].hex()]}
                continue
            scda = data[4:]
            variables = compiled['variables']
            references = compiled['references']
            subs = [('SCHR', struct.pack('<4sIIIHH', b'\0' * 4, len(references), len(scda), len(variables), stype, 1)),
                    ('SCDA', scda), ('SCTX', text)]
            kinds = {'float': 0, 'int': 1, 'string': 2, 'array': 3, 'ref': 0}
            for v in variables:
                subs.append(('SLSD', struct.pack('<I12sB7s', v['index'], b'\0' * 12, kinds.get(v['type'], 0), b'\0' * 7)))
                subs.append(('SCVR', v['name'].encode('cp1252') + b'\0'))
            for ref in references:
                if ref.get('kind') == 'variable':
                    subs.append(('SCRV', struct.pack('<I', ref['index'])))
                else:
                    subs.append(('SCRO', struct.pack('<I', relative(ref['formId']))))
            entry_compiled = [(tag, value.hex()) for tag, value in subs]
            candidate = {'formId': form_id, 'type': stype, 'text': text, 'compiledSize': len(scda), 'compiled': entry_compiled}
            try:
                proof = esp_scripts.verify_script(candidate, text, stype, form_id, prior=cache.get(edid))
            except ValueError as e:
                results[edid] = {'compiled': True, 'cached': False, 'reason': 'builder verification failed: %s' % e}
                continue
            cache[edid] = {'sourceSha256': generic.source_sha(text, stype), 'formId': form_id,
                           'compiledSize': len(scda), 'compiled': entry_compiled, 'verification': proof,
                           'provenance': {'context': context, 'kind': 'fnv_ingame_compile',
                                          'compiler': 'FalloutNV.exe 1.4.0.525 script compiler through xNVSE '
                                                      'CompileScript (fnvmcp engine companion)',
                                          'pid': (live.link.hello or {}).get('pid'), 'compiledAt': time.time(),
                                          'loadOrderIndex': '%02X' % loaded[plugin.lower()],
                                          'adjustments': ['dropped the 4-byte scriptname statement xNVSE writes first',
                                                          'runtime form ids mapped to master-relative ids']}}
            changed = True
            results[edid] = {'compiled': True, 'cached': True, 'bytes': len(scda), 'variables': len(variables),
                             'references': len(references)}
        if changed:
            path = esp_scripts.cache_path(self.project.state_dir, spec['project_id'])
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, (json.dumps(cache, indent=1, sort_keys=True) + '\n').encode())
        return {'scripts': results, 'cache': str(esp_scripts.cache_path(self.project.state_dir, spec['project_id'])),
                'next': 'Rebuild (geck_plugin_build) to include the compiled scripts, then fnv_install and restart.'
                if changed else None}

    # ------------------------------------------------------------------ fnv_script_lint
    def script_lint(self, source, plugin=None, check_forms=True):
        return lint_script(source, self.live.engine() if self.live else None,
                           self._edid_lookup if check_forms else None)

    def _edid_lookup(self, edid):
        if not hasattr(self, '_lint_entries'):
            self._lint_entries = self._active()[0]
        files = ','.join(str(e['fileId']) for e in self._lint_entries)
        rows = self.index.db.execute('select type from records where edid = ? collate nocase and file in (%s)' % files,
                                     (edid,)).fetchall()
        return {r[0] for r in rows}

    # ------------------------------------------------------------------ fnv_compile_ingame / hot swap
    def compile_ingame(self, source, include_bytecode=True):
        live = self.live
        live.ready()
        # The compiler prints its errors only while the console is visible or "verbose" is on (fnv_facts): set
        # both for the frame of the compile, as fnv_console does.
        from .fnv_facts import FACTS
        verbose, visible = FACTS['verbose_print_flag']['address'], FACTS['console_visible_flag']['address']

        def compile_once(text):
            old = live.link.batch([('rd', '%x' % verbose, '1'), ('rd', '%x' % visible, '1')])
            results = live.link.batch([('wr', '%x' % verbose, '01'), ('wr', '%x' % visible, '01'),
                                       ('compile', _hexs(text)), ('wr', '%x' % visible, old[1].get('hex', '00')),
                                       ('wr', '%x' % verbose, old[0].get('hex', '00'))])
            return results[2]

        # As written first (the game knows the EditorIDs of quests and persistent references); then with the
        # EditorIDs it does not keep (most base forms) replaced by their form ids.
        result, substituted = compile_once(source), {}
        if not result.get('ok'):
            text, substituted = live.substitute_editor_ids(source)
            if substituted:
                result = compile_once(text)
        output = [l for l in (result.get('output') or '').replace('\r', '').split('\n') if l.strip()]
        info = {'compiled': bool(result.get('ok')), 'errors': output if not result.get('ok') else [],
                'messages': output, 'substitutedEditorIds': substituted or None}
        if not result.get('ok') and not output:
            info['errors'] = [result.get('error') or 'the compiler rejected the script without printing why']
        if not result.get('ok') and re.search(r'\b[A-Za-z_]\w*\.[A-Za-z_]\w*', source) and \
                any('Unknown variable' in e for e in info['errors']):
            info['hint'] = ('The running game keeps no quest EditorIDs, so its compiler cannot resolve Quest.variable '
                            'references (GECK and the plugin builder can). Compile in game only scripts without them, '
                            'or read such variables through a reference variable set at runtime.')
        if not result.get('ok'):
            info['lint'] = self.script_lint(source, check_forms=False)
            return info
        script = int(result['script'], 16)
        info.update(read_script(live, script, include_bytecode))
        info['script'] = '0x%08X' % script
        info['note'] = ('Compiled by the running game (xNVSE CompileScript). The bytecode starts with a 4-byte '
                        'scriptname header xNVSE writes before compiling.')
        return info

    def hot_swap(self, script, source, force=False):
        from .fnv_deep import DeepTools
        live = self.live
        live.in_game()
        deep = getattr(live, 'deep', None) or DeepTools(live)
        target, target_info, owner = deep.script_form(script)
        compiled = self.compile_ingame(source, include_bytecode=False)
        if not compiled['compiled']:
            raise DataError('COMPILE_FAILED', 'The new source does not compile', compiled)
        fresh = int(compiled['script'], 16)
        old = read_script(live, target, False)
        new_vars = {v['name'].lower(): v['index'] for v in compiled['variables']}
        old_vars = {v['name'].lower(): v['index'] for v in old['variables']}
        moved = {n: (old_vars[n], new_vars[n]) for n in old_vars if n in new_vars and old_vars[n] != new_vars[n]}
        dropped = sorted(set(old_vars) - set(new_vars))
        added = sorted(set(new_vars) - set(old_vars))
        if (moved or dropped) and not force:
            raise DataError('VARIABLES_CHANGED', 'Running instances keep their variables by index: keep the old '
                            'variables in the same order (append new ones at the end) or pass force=true',
                            {'moved': moved, 'dropped': dropped, 'added': added})
        live.changing('script hot swap')
        swapped = swap_script_code(live, target, fresh)
        entry = live.journal.add({'kind': 'script_hot_swap', 'script': target_info.get('formId'), 'target': '0x%08X' % target,
                                  'holder': '0x%08X' % fresh, 'pid': (live.link.hello or {}).get('pid'),
                                  'undo': {'op': 'swap_script', 'target': '0x%08X' % target, 'holder': '0x%08X' % fresh}})
        return {'script': target_info, 'owner': owner, 'swapped': swapped, 'variablesAdded': added,
                'variablesMoved': moved or None, 'variablesDropped': dropped or None,
                'statements': len(compiled.get('statements') or []), 'journalId': entry['id'],
                'note': 'Running instances use the new code from their next run and keep their variable values. Variables '
                        'added by the new code do not exist in instances that were already running (reads give 0 and '
                        'the game logs "Variable ID not found"); instances created later have them. fnv_undo swaps the '
                        'old code back.'}


# ===================================================================================== helpers
def _hexs(text):
    from .fnv_link import hexs
    return hexs(text)


def read_record(path, offset):
    with open(path, 'rb') as f:
        f.seek(offset)
        head = f.read(24)
        tag, size, flags, form_id, vc1, version, vc2 = codec.RECORD_HEADER.unpack(head)
        data = f.read(size)
    record = codec.Record(tag.decode('latin-1'), form_id, flags, vc1, version, vc2)
    if flags & codec.COMPRESSED:
        record._compressed = data
    else:
        record._subrecords = codec.parse_subrecords(data, offset + 24)
    return record


def _changed_tags(old, new):
    def by_tag(record):
        out = {}
        for s in record.subrecords:
            out.setdefault(s.tag, []).append(s.data)
        return out
    a, b = by_tag(old), by_tag(new)
    return sorted(t for t in set(a) | set(b) if a.get(t) != b.get(t))


# 4-byte subrecords that hold numbers, never a form id
NOT_FORMS = {'DATA', 'XSCL', 'ETYP', 'XCNT', 'XLCM', 'QOBJ', 'INDX', 'FLTV', 'FNAM', 'XHLP', 'XRDS', 'XACT', 'XPRD',
             'ANAM', 'DNAM', 'BNAM', 'ENIT', 'VNAM', 'NNAM', 'RCLR', 'XCLW', 'XCLC', 'XAMC', 'XHLT', 'XLKR_COUNT'}

STRING_TAGS = {'EDID', 'FULL', 'DESC', 'MODL', 'ICON', 'MICO', 'SCTX', 'NNAM', 'TNAM', 'XATO', 'ITXT', 'CNAM_TEXT',
               'NAM1', 'RNAM', 'ONAM', 'BMCT', 'MOD2', 'MOD3', 'MOD4', 'SCVR'}


def decode_subrecord(rtype, tag, data):
    """A readable value for common subrecords, or None (shown as hex or as a form reference)."""
    if tag in STRING_TAGS or (tag == 'NAM1' and rtype == 'INFO'):
        if not data or (data.endswith(b'\0') and all(c >= 0x20 or c in (9, 10, 13) for c in data[:-1])) or tag == 'SCTX':
            return data.rstrip(b'\0').decode('cp1252', 'replace')
    if tag == 'SCHR' and len(data) >= 20:
        unused, refs, size, variables, stype, flags = struct.unpack_from('<IIIIHH', data)
        return {'references': refs, 'dataSize': size, 'variables': variables,
                'type': {0: 'object', 1: 'quest', 0x100: 'effect'}.get(stype, stype), 'flags': flags}
    if tag == 'SLSD' and len(data) >= 24:
        return {'index': struct.unpack_from('<I', data)[0], 'type': 'int' if data[16] & 1 else 'float'}
    if tag == 'SCDA':
        return {'bytecode': len(data), 'statements': len(_statement_offsets(data))}
    if rtype == 'WEAP' and tag == 'DATA' and len(data) >= 15:
        value, health, weight, damage, clip = struct.unpack_from('<iifhB', data)
        return {'value': value, 'health': health, 'weight': round(weight, 3), 'damage': damage, 'clipSize': clip}
    if rtype in ('MISC', 'KEYM', 'AMMO') and tag == 'DATA' and len(data) >= 8:
        value, weight = struct.unpack_from('<if', data)
        return {'value': value, 'weight': round(weight, 3)}
    if rtype == 'ARMO' and tag == 'DATA' and len(data) >= 12:
        value, health, weight = struct.unpack_from('<iif', data)
        return {'value': value, 'health': health, 'weight': round(weight, 3)}
    if rtype == 'GLOB' and tag == 'FNAM' and data:
        return {'type': {ord('s'): 'short', ord('l'): 'long', ord('f'): 'float'}.get(data[0], data[0])}
    if rtype == 'GLOB' and tag == 'FLTV' and len(data) == 4:
        return round(struct.unpack('<f', data)[0], 6)
    if rtype == 'QUST' and tag == 'DATA' and len(data) >= 2:
        return {'flags': data[0], 'priority': data[1], 'scriptDelay': round(struct.unpack_from('<f', data, 4)[0], 3)
                if len(data) >= 8 else None}
    if rtype == 'QUST' and tag == 'INDX' and len(data) >= 2:
        return {'stage': struct.unpack_from('<h', data)[0]}
    if tag in ('OBND',) and len(data) == 12:
        return list(struct.unpack('<6h', data))
    return None


def _statement_offsets(data):
    from .fnv_deep import statement_offsets
    return statement_offsets(data)


# ------------------------------------------------------------------ live script reading and swapping
SCRIPT_HEADER = 0x18     # ScriptHeaderData (0x14 bytes): unused, refCount, dataSize, varCount, type, flags
SCRIPT_DATA = 0x30
SCRIPT_REFS = 0x44       # BSSimpleList<ScriptReferencedObject*>
SCRIPT_VARS = 0x4C       # BSSimpleList<ScriptVariableInfo*>


def read_script(live, script, include_bytecode=True):
    header, data_ptr = live.reads([(script + SCRIPT_HEADER, 0x14)])[0], live.u32s([script + SCRIPT_DATA])[0]
    _, refs, size, variables, stype, flags = struct.unpack('<IIIIHH', header)
    var_ptrs = live.walk_list(script + SCRIPT_VARS, 512)
    raws = live.reads([(p, 0x20) for p in var_ptrs]) if var_ptrs else []
    names = live.link.batch([('rs', '%x' % struct.unpack_from('<I', r, 0x18)[0], '80') for r in raws if r]) if raws else []
    out_vars = []
    for raw, name in zip([r for r in raws if r], names):
        index, = struct.unpack_from('<I', raw, 0)
        out_vars.append({'index': index, 'name': name.get('text') if name.get('ok') else None,
                         'type': {0: 'float', 1: 'int', 2: 'string', 3: 'array', 4: 'ref'}.get(raw[0x10], raw[0x10])})
    ref_ptrs = live.walk_list(script + SCRIPT_REFS, 512)
    ref_raw = [r for r in (live.reads([(p, 0x10) for p in ref_ptrs]) if ref_ptrs else []) if r]
    # ScriptReferencedObject: name String (8), form (4), variable index (4): a form, or a local ref variable
    ref_forms = live.form_ids([struct.unpack_from('<I', r, 8)[0] for r in ref_raw]) if ref_raw else []
    references = []
    for raw, form in zip(ref_raw, ref_forms):
        if form:
            references.append(dict(form, kind='form'))
        else:
            references.append({'kind': 'variable', 'index': struct.unpack_from('<I', raw, 12)[0]})
    data = live.reads([(data_ptr, min(size, 0xFF00))])[0] if data_ptr and size else b''
    out = {'type': {0: 'object', 1: 'quest', 0x100: 'effect'}.get(stype, stype), 'dataSize': size,
           'variables': sorted(out_vars, key=lambda v: v['index']),
           'references': references, 'statements': _statement_offsets(data) if data else []}
    if include_bytecode:
        out['bytecode'] = data.hex()
    return out


def swap_script_code(live, a, b):
    """Exchange two Script objects' compiled code: header counts (not the type and flags), data, reference
    and variable lists. Both objects stay alive, so swapping again restores the first."""
    spans = [(SCRIPT_HEADER, 0x10), (SCRIPT_DATA, 4), (SCRIPT_REFS, 8), (SCRIPT_VARS, 8)]
    ra = live.reads([(a + off, n) for off, n in spans])
    rb = live.reads([(b + off, n) for off, n in spans])
    if None in ra or None in rb:
        raise DataError('UNREADABLE', 'A script object is unreadable')
    ops = []
    for (off, _), va, vb in zip(spans, ra, rb):
        ops += [('wr', '%x' % (a + off), vb.hex()), ('wr', '%x' % (b + off), va.hex())]
    results = live.link.batch(ops)  # one request: the swap happens within one frame
    if not all(r.get('ok') for r in results):
        raise DataError('SWAP_FAILED', 'Writing the script fields failed', results)
    return {'fields': ['header counts', 'data', 'references', 'variables'], 'target': '0x%08X' % a, 'holder': '0x%08X' % b}


# ------------------------------------------------------------------ lint
TOKEN = re.compile(r'"[^"]*"|[A-Za-z_][\w.]*|0x[0-9A-Fa-f]+|\d+\.?\d*|==|!=|<=|>=|&&|\|\||[-+*/()<>,{}:=!]')


def lint_script(source, engine, edid_lookup=None):
    """Static checks against the game's command tables: structure (scn, begin/end, if/endif), block types,
    variables, unknown commands, argument counts, and EditorIDs that no active plugin defines."""
    problems = []

    def add(line, severity, code, message):
        problems.append({'line': line, 'severity': severity, 'code': code, 'message': message})

    commands = {}
    if engine is not None:
        for c in engine.commands:
            for key in (c['name'], c.get('short')):
                if key:
                    commands.setdefault(key.lower(), c)
    blocks = {b.lower() for b in SCRIPT_BLOCKS} | BLOCK_ALIASES | {'function'}
    variables, stack, in_block, seen_scn = {}, [], None, False
    lines = source.replace('\r', '').split('\n')
    for n, raw in enumerate(lines, 1):
        text = raw.split(';', 1)[0].strip()
        if not text:
            continue
        head = re.match(r'([A-Za-z_][\w.]*)(.*)', text)
        words = ([head.group(1)] + head.group(2).split()) if head else text.split()
        first = words[0].lower()
        if not seen_scn:
            if first not in ('scn', 'scriptname'):
                add(n, 'error', 'NO_SCRIPTNAME', 'The first statement must be "scn <ScriptName>"')
            seen_scn = True
            if first in ('scn', 'scriptname'):
                if len(words) != 2:
                    add(n, 'error', 'BAD_SCRIPTNAME', 'scn takes one name')
                continue
        if first in DECLARATIONS:
            if in_block:
                add(n, 'warning', 'DECLARATION_IN_BLOCK', 'Declare variables before the first begin block')
            for name in words[1:]:
                key = name.lower().rstrip(',')
                if key in variables:  # vanilla scripts do this and compile
                    add(n, 'warning', 'DUPLICATE_VARIABLE', 'Variable %s declared twice' % name)
                if key in commands:
                    add(n, 'warning', 'VARIABLE_SHADOWS_COMMAND', '%s is also a command name' % name)
                variables[key] = first
            continue
        if first == 'begin':
            if in_block:
                add(n, 'error', 'NESTED_BEGIN', 'begin inside a block: close the previous one with end')
            block = words[1] if len(words) > 1 else ''
            if block.lower().rstrip('{') not in blocks:
                add(n, 'error', 'UNKNOWN_BLOCK', 'Unknown block type %r (known: %s...)' % (block, ', '.join(SCRIPT_BLOCKS[:6])))
            in_block = (block, n)
            continue
        if first == 'end':
            if stack:
                add(stack[-1][1], 'error', 'UNCLOSED_IF', 'if without endif before the end of the block')
                stack.clear()
            if not in_block:
                add(n, 'error', 'END_WITHOUT_BEGIN', 'end without begin')
            in_block = None
            continue
        if not in_block:
            add(n, 'error', 'OUTSIDE_BLOCK', 'Statement outside a begin/end block')
        if first == 'if':
            stack.append(('if', n))
        elif first in ('elseif', 'else'):
            if not stack:  # after a stray endif; the compiler accepts it
                add(n, 'warning', 'ELSE_WITHOUT_IF', '%s without if' % first)
        elif first == 'endif':
            if not stack:  # the game's compiler accepts a stray endif (GoodspringsRadioScript has one)
                add(n, 'warning', 'ENDIF_WITHOUT_IF', 'endif without if (accepted by the compiler, but a mistake)')
            else:
                stack.pop()
        elif first == 'while':
            stack.append(('while', n))
        elif first == 'loop':
            if stack and stack[-1][0] == 'while':
                stack.pop()
        elif first in ('set', 'let'):
            target = words[1] if len(words) > 1 else ''
            if first == 'set' and (len(words) < 4 or words[2].lower() != 'to'):
                add(n, 'error', 'BAD_SET', 'set <variable> to <expression>')
            name = target.split('.')[-1].lower() if '.' not in target else None
            if name is not None and name not in variables and not edid_lookup_hit(edid_lookup, target, {'GLOB'}):
                add(n, 'error', 'UNDECLARED_VARIABLE', 'Variable %s is not declared' % target)
        if first in ('if', 'elseif', 'set', 'let', 'return', 'else', 'endif', 'while', 'loop'):
            expression = words[1:] if first not in ('set', 'let') else words[3:]
            _check_tokens(n, ' '.join(expression), commands, variables, edid_lookup, add)
            continue
        _check_call(n, text, commands, variables, edid_lookup, add, statement=True)
    if in_block:
        add(in_block[1], 'error', 'UNCLOSED_BLOCK', 'begin %s without end' % in_block[0])
    if stack:
        add(stack[-1][1], 'error', 'UNCLOSED_IF', '%s without its end' % stack[-1][0])
    errors = [p for p in problems if p['severity'] == 'error']
    return {'ok': not errors, 'errors': len(errors), 'warnings': len(problems) - len(errors), 'problems': problems,
            'variables': variables, 'checkedCommands': len(commands),
            'note': 'Static checks only; fnv_compile_ingame runs the game\'s own compiler.' +
                    ('' if engine is not None else ' (no engine map: commands not checked)')}


def edid_lookup_hit(lookup, name, types=None):
    if not lookup:
        return True  # cannot tell: do not report
    found = lookup(name)
    return bool(found and (types is None or found & types))


def _split_args(text):
    out, depth, current, quote = [], 0, '', False
    for ch in text:
        if ch == '"':
            quote = not quote
        if not quote and ch in '([{':
            depth += 1
        elif not quote and ch in ')]}':
            depth -= 1
        if not quote and depth == 0 and ch in ' ,\t':
            if current:
                out.append(current)
            current = ''
            continue
        current += ch
    if current:
        out.append(current)
    return out


def _check_call(n, text, commands, variables, lookup, add, statement=False):
    words = _split_args(text)
    if not words:
        return
    head = words[0]
    ref, _, name = head.rpartition('.')
    command = commands.get(name.lower())
    if not command:
        if statement and commands:
            add(n, 'error', 'UNKNOWN_COMMAND', 'Unknown command %r (not in the vanilla, xNVSE or JIP LN tables)' % name)
        return
    if ref and ref.lower() not in variables and lookup and not lookup(ref):
        add(n, 'warning', 'UNKNOWN_REFERENCE', '%s is not a variable or an EditorID in the active plugins' % ref)
    params = command.get('params') or []
    args = words[1:]
    required = sum(1 for p in params if not p.get('optional'))
    if not command.get('paramsUnresolved') and params is not None and command.get('source') == 'vanilla':
        if len(args) < required:
            add(n, 'error', 'TOO_FEW_ARGUMENTS', '%s needs %d argument(s), got %d' % (command['name'], required, len(args)))
        elif len(args) > len(params):
            add(n, 'warning', 'TOO_MANY_ARGUMENTS', '%s takes %d argument(s), got %d' % (command['name'], len(params), len(args)))
    for arg, param in zip(args, params):
        types = FORM_TYPES_FOR_PARAM.get(param.get('type'))
        if not types or not re.match(r'^[A-Za-z_]\w*$', arg) or arg.lower() in variables or arg.lower() in KNOWN_NAMES:
            continue
        if lookup:
            found = lookup(arg)
            if not found:
                add(n, 'warning', 'UNKNOWN_EDITOR_ID', '%s (%s argument of %s) is not an EditorID in the active plugins'
                    % (arg, param.get('type'), command['name']))
            elif not found & types and not ({'REFR', 'ACHR', 'ACRE'} & types and found & {'REFR', 'ACHR', 'ACRE'}):
                add(n, 'warning', 'WRONG_FORM_TYPE', '%s is a %s, %s expects %s' % (arg, '/'.join(sorted(found)),
                                                                                    command['name'], param.get('type')))


def _check_tokens(n, expression, commands, variables, lookup, add):
    skip_until = -1
    for match in re.finditer(r'(?<![\w."])([A-Za-z_][\w]*\.)?([A-Za-z_]\w*)\s*', expression):
        if match.start() < skip_until:
            continue  # an argument of the command call checked just before
        ref, name = match.group(1), match.group(2)
        key = name.lower()
        if key in commands:
            # the call's arguments end at the first operator or closing parenthesis
            call = re.split(r'==|!=|<=|>=|<|>|&&|\|\||\)|\+|(?<=\s)-|\*|/', expression[match.start():])[0]
            _check_call(n, call.strip(' ('), commands, variables, lookup, add)
            skip_until = match.start() + len(call)
            continue
        if key in variables or key in FLOW_WORDS or key in KNOWN_NAMES or ref:
            continue  # quest.variable references are not checked offline
        if lookup and not lookup(name):
            add(n, 'warning', 'UNKNOWN_NAME', '%s is not a declared variable, a command or an EditorID' % name)
