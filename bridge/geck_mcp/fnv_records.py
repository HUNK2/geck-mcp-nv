"""Load-order analysis over plugin data (fnv-mcp phase 2, offline):

  fnv_record_users  what references a record: placed refs, scripts, leveled lists, containers, conditions,
                    packages, dialogue, form lists, any field holding its form id
  fnv_conflicts     records more than one plugin edits, the winner, and which fields each override changes
  fnv_asset_check   meshes, textures and sounds a plugin names that exist in no archive or loose folder
  fnv_profile_set   enable or disable mods and plugins in the MO2 dev profile (game closed), with a backup

The reverse-reference index lives in the same SQLite cache as the record index (state/fnv/records.sqlite,
table refs), built per plugin file once and rebuilt when the file changes. A field counts as a form reference
when the game reads it as one (known form fields, condition parameters, leveled list and container entries) or,
for other small fields, when its value names a record that exists in the plugin's masters or itself.
"""
import json
import shutil
import struct
import time
import zlib
from pathlib import Path

from .esp import assets
from .fnv_data import DataError, NOT_FORMS, STRING_TAGS, load_order, read_record

REFS_VERSION = 2
SKIP_REFS = {b'LAND', b'NAVM', b'NAVI', b'TES4'}  # terrain and navmesh hold no useful form references
# subrecord -> byte offsets of form ids the game reads there (beyond the generic small-field rule)
FORM_FIELDS = {
    'CNTO': (0,), 'LVLO': (4,), 'SCRO': (0,), 'NAME': (0,), 'XESP': (0,), 'XTEL': (0,), 'XOWN': (0,),
    'PKID': (0,), 'SPLO': (0,), 'SNAM': (0,), 'TPIC': (0,), 'QSTI': (0,), 'TCLT': (0,), 'TCLF': (0,),
    'XEZN': (0,), 'XLKR': (0, 4), 'XAPR': (0,), 'XMRC': (0,), 'XRGD': (), 'INAM': (0,), 'SCRI': (0,),
    'XLOC': (4,), 'XMBR': (0,), 'XRTM': (0,), 'XLRL': (0,), 'XNDP': (0,), 'PNAM': (0,),
    'XCWT': (0,), 'XCCM': (0,), 'XCIM': (0,), 'XCAS': (0,), 'XCMO': (0,), 'XCET': (0,), 'XCAC': (0,),
}
TYPED_FORM_FIELDS = {('FLST', 'LNAM'): (0,)}
# CTDA (28 bytes): u8 type, 3 pad, comparison value or global (4), u16 function (8), 2 pad, param 1 (12),
# param 2 (16), u32 run on (20), reference (24, when run on = 2). Function = command opcode - 0x1000.
NOT_FORM_PARAMS = {'Integer', 'Float', 'String', 'ActorValue', 'Axis', 'AnimationGroup', 'Sex', 'VariableName',
                   'QuestStage', 'CrimeType', 'FormType', 'MiscellaneousStat', 'CriticalStage', 'Alignment',
                   'EquipType', 'SoundFile', 'NonFormList', 'Unhandled2E'}
# subrecords naming asset files (relative to the folder in ASSET_ROOT)
ASSET_TAGS = {
    'MODL': 'meshes', 'MOD2': 'meshes', 'MOD3': 'meshes', 'MOD4': 'meshes', 'DMDL': 'meshes', 'MWD1': 'meshes',
    'MWD2': 'meshes', 'MWD3': 'meshes', 'MWD4': 'meshes', 'MWD5': 'meshes', 'MWD6': 'meshes', 'MWD7': 'meshes',
    'ICON': 'textures', 'MICO': 'textures', 'ICO2': 'textures', 'MIC2': 'textures',
    'TX00': 'textures', 'TX01': 'textures', 'TX02': 'textures', 'TX03': 'textures', 'TX04': 'textures',
    'TX05': 'textures',
}
ASSET_EXTENSIONS = {'nif', 'dds', 'kf', 'wav', 'mp3', 'ogg', 'egm', 'egt', 'tri', 'spt', 'lip', 'xml', 'psa', 'bmp',
                    'tga', 'png'}
ASSET_TYPES_TAG = {('SOUN', 'FNAM'): 'sound', ('MUSC', 'FNAM'): 'music', ('IDLE', 'MODL'): 'meshes',
                   ('DIAL', 'MODL'): None, ('WTHR', 'DNAM'): 'textures', ('WTHR', 'CNAM'): 'textures',
                   ('CLMT', 'FNAM'): 'textures', ('CLMT', 'GNAM'): 'textures', ('LSCR', 'ICON'): 'textures'}
GENERIC_MAX = 64  # bytes; larger unknown subrecords are data blobs, not form lists


# ===================================================================================== reference scan
def scan_references(path, exists, condition_params=None):
    """[(from raw id, from type, record offset, tag, to raw id)] for a plugin. `exists(raw_id)` says whether a
    raw form id (as written in this plugin) names a record in its masters or itself."""
    data = Path(path).read_bytes()
    if data[:4] != b'TES4':
        raise DataError('NOT_A_PLUGIN', '%s is not a TES4 plugin' % path)
    header_size = struct.unpack_from('<I', data, 4)[0]
    out, pos, end = [], 24 + header_size, len(data)
    unpack = struct.Struct('<4sIII').unpack_from
    u32 = struct.Struct('<I').unpack_from
    u16 = struct.Struct('<H').unpack_from
    while pos + 24 <= end:
        tag, size, flags, form_id = unpack(data, pos)
        if tag == b'GRUP':
            pos += 24
            continue
        body_at = pos + 24
        if tag in SKIP_REFS or not size:
            pos = body_at + size
            continue
        body = data[body_at:body_at + size]
        if flags & 0x00040000:
            try:
                body = zlib.decompress(body[4:])
            except zlib.error:
                pos = body_at + size
                continue
        rtype = tag.decode('latin-1')
        p, n, big = 0, len(body), None
        while p + 6 <= n:
            stag, ssize = body[p:p + 4], u16(body, p + 4)[0]
            if big is not None:
                ssize, big = big, None
            if stag == b'XXXX':
                big = u32(body, p + 6)[0]
                p += 6 + 4
                continue
            sub = stag.decode('latin-1')
            start = p + 6
            if sub in STRING_TAGS or sub == 'EDID':
                p = start + ssize
                continue
            offsets = TYPED_FORM_FIELDS.get((rtype, sub)) or FORM_FIELDS.get(sub)
            known = offsets is not None
            if sub == 'CTDA':
                offsets, known = [], True
                if ssize >= 24:
                    if body[start] & 0x04:  # the comparison value is a global
                        offsets.append(4)
                    kinds = (condition_params or {}).get(u16(body, start + 8)[0])
                    for i, off in enumerate((12, 16)):
                        if kinds is None or (i < len(kinds) and kinds[i]):
                            offsets.append(off)
                    if ssize >= 28 and u32(body, start + 20)[0] == 2:
                        offsets.append(24)
            elif offsets is None and ssize <= GENERIC_MAX and ssize % 4 == 0 and sub not in NOT_FORMS:
                offsets = range(0, ssize, 4)
            for off in offsets or ():
                if off + 4 <= ssize:
                    value = u32(body, start + off)[0]
                    if value and (known or (value & 0xFFFFFF) >= 0x800) and exists(value):
                        out.append((form_id, rtype, pos, sub, value))
            if sub == 'SCDA' and ssize >= 0:  # compiled script: references come through SCRO, not bytecode
                pass
            p = start + ssize
        pos = body_at + size
    return out


class RecordsTools:
    """Offline load-order tools on top of DataTools' record index."""

    def __init__(self, data):
        self.data = data
        self._db_ready = False

    @property
    def db(self):
        db = self.data.index.db
        if not self._db_ready:
            db.executescript('''
                create table if not exists ref_files(file integer primary key, version integer, built real, refs integer);
                create table if not exists refs(file integer, from_owner text, from_local integer, from_type text,
                    offset integer, tag text, to_owner text, to_local integer);
                create index if not exists refs_to on refs(to_owner, to_local);
                create index if not exists refs_file on refs(file);
            ''')
            self._db_ready = True
        return db

    # ------------------------------------------------------------------ index
    def _condition_params(self):
        """{condition function index: [param is a form, ...]} from the game's command table (engine map)."""
        if getattr(self, '_cond', None) is None:
            self._cond = {}
            try:
                from . import fnv_engine
                e = fnv_engine.load(*fnv_engine.default_paths(self.data.project))
                for c in e.commands:
                    if c.get('source') != 'vanilla' or not c.get('opcode'):
                        continue
                    op = int(c['opcode'], 16)
                    if 0x1000 <= op < 0x1400:
                        self._cond[op - 0x1000] = [p.get('type') not in NOT_FORM_PARAMS for p in c.get('params') or []]
            except Exception:  # no engine map: fall back to treating both parameters as candidates
                self._cond = {}
        return self._cond

    def _ensure_refs(self, entries):
        """Make sure every active plugin has its references indexed. Returns the names built now."""
        built = []
        db = self.db
        for e in entries:
            row = db.execute('select version from ref_files where file = ?', (e['fileId'],)).fetchone()
            if row and row[0] == REFS_VERSION:
                continue
            info = self.data.index.file(e['fileId'])
            own = [m.lower() for m in info['masters']] + [info['name'].lower()]
            known = {}
            for owner in own:
                ids = {local for (local,) in db.execute('select local from records where owner = ?', (owner,))}
                known[owner] = ids
            hardcoded = own[0] == 'falloutnv.esm'

            def exists(raw, own=own, known=known, hardcoded=hardcoded):
                high = raw >> 24
                if high >= len(own):
                    return False
                local = raw & 0xFFFFFF
                if high == 0 and hardcoded and local < 0x800:
                    return True  # engine forms (player, PlayerRef, default objects) have no plugin record
                return local in known[own[high]]

            started = time.monotonic()
            rows = scan_references(e['path'], exists, self._condition_params())
            db.execute('delete from refs where file = ?', (e['fileId'],))
            db.executemany('insert into refs values (?, ?, ?, ?, ?, ?, ?, ?)',
                           ((e['fileId'], own[min(f >> 24, len(own) - 1)], f & 0xFFFFFF, t, off, tag,
                             own[min(v >> 24, len(own) - 1)], v & 0xFFFFFF) for f, t, off, tag, v in rows))
            db.execute('insert or replace into ref_files values (?, ?, ?, ?)',
                       (e['fileId'], REFS_VERSION, time.time(), len(rows)))
            db.commit()
            built.append({'plugin': e['name'], 'references': len(rows), 'seconds': round(time.monotonic() - started, 1)})
        return built

    # ------------------------------------------------------------------ fnv_record_users
    def record_users(self, ref, types=None, limit=100, offset=0):
        entries, built = self.data._active()
        owner, local = self.data._resolve(entries, ref)
        refs_built = self._ensure_refs(entries)
        by_file = {e['fileId']: e for e in entries}
        rows = self.db.execute('select file, from_owner, from_local, from_type, offset, tag from refs '
                               'where to_owner = ? and to_local = ?', (owner, local)).fetchall()
        rows = [r for r in rows if r[0] in by_file]
        wanted = {t.upper() for t in types} if types else None
        # the winning version of each referencing record decides whether the reference still exists
        latest = {}
        for (fid, from_owner, from_local) in self.db.execute(
                'select file, owner, local from records where (owner, local) in (select from_owner, from_local from refs '
                'where to_owner = ? and to_local = ?)', (owner, local)):
            if fid in by_file:
                cur = latest.get((from_owner, from_local))
                if cur is None or by_file[fid]['index'] > by_file[cur]['index']:
                    latest[(from_owner, from_local)] = fid
        users = {}
        for fid, from_owner, from_local, from_type, off, tag in rows:
            if wanted and from_type not in wanted:
                continue
            key = (from_owner, from_local)
            u = users.setdefault(key, {'formId': self.data._global(entries, from_owner, from_local), 'type': from_type,
                                       'fields': set(), 'plugins': set(), 'inWinner': False})
            u['fields'].add(tag)
            u['plugins'].add(by_file[fid]['name'])
            if latest.get(key) == fid:
                u['inWinner'] = True
        names = {}
        if users:
            for o, l, edid, full in self.db.execute(
                    'select owner, local, edid, full from records where (owner, local) in (%s)' %
                    ','.join('(?, ?)' for _ in users), [x for k in users for x in k]):
                names.setdefault((o, l), (edid, full))
        out = []
        for key, u in users.items():
            edid, full = names.get(key, (None, None))
            out.append({'formId': u['formId'], 'type': u['type'], 'editorId': edid, 'name': full,
                        'how': sorted(_how(u['type'], t) for t in u['fields']), 'fields': sorted(u['fields']),
                        'plugins': sorted(u['plugins']),
                        'stillInWinner': u['inWinner']})
        out.sort(key=lambda x: (not x['stillInWinner'], x['type'], x['editorId'] or ''))
        for x in out[offset:offset + limit]:
            if x['type'] != 'INFO':
                continue
            key = next((k for k, u in users.items() if u['formId'] == x['formId']), None)
            fid = latest.get(key)
            row = fid and self.db.execute('select offset from records where file = ? and owner = ? and local = ?',
                                          (fid, key[0], key[1])).fetchone()
            if row:
                try:
                    record = read_record(by_file[fid]['path'], row[0])
                    text = next((s.data for s in record.subrecords if s.tag == 'NAM1'), None)
                    if text:
                        x['text'] = text.rstrip(b'\0').decode('cp1252', 'replace')[:160]
                except Exception:
                    pass
        by_type = {}
        for x in out:
            by_type[x['type']] = by_type.get(x['type'], 0) + 1
        target = self.db.execute('select type, edid from records where owner = ? and local = ? limit 1',
                                 (owner, local)).fetchone()
        return {'record': {'formId': self._global(entries, owner, local), 'type': target[0] if target else None,
                           'editorId': target[1] if target else None},
                'count': len(out), 'byType': by_type, 'users': out[offset:offset + limit],
                'more': len(out) > offset + limit, 'indexBuilt': (built or {}).get('indexed') if built else None,
                'referencesIndexed': refs_built or None,
                'note': 'stillInWinner false = only an overridden version of that record references it. '
                        'Scripts reference forms through their SCRO list; console commands and xNVSE string '
                        'lookups at run time are not visible offline.'}

    def _global(self, entries, owner, local):
        return self.data._global(entries, owner, local)

    # ------------------------------------------------------------------ fnv_conflicts
    def conflicts(self, plugin=None, record_type=None, limit=50, offset=0, include_identical=False, scan_cap=20000):
        entries, built = self.data._active()
        by_file = {e['fileId']: e for e in entries}
        files = ','.join(str(f) for f in by_file) or '0'
        clauses, args = ['file in (%s)' % files], []
        if record_type:
            clauses.append('type = ?')
            args.append(record_type.upper())
        sql = ('select owner, local, type, group_concat(file), count(*) from records where %s group by owner, local '
               'having count(*) > 1' % ' and '.join(clauses))
        groups = self.db.execute(sql, args).fetchall()
        target = None
        if plugin:
            target = next((e for e in entries if e['name'].lower() == plugin.lower()), None)
            if not target:
                raise DataError('PLUGIN_NOT_ACTIVE', '%s is not in the active load order' % plugin)
        rows = []
        for owner, local, rtype, file_list, count in groups:
            fids = sorted({int(f) for f in file_list.split(',')}, key=lambda f: by_file[f]['index'])
            if target and target['fileId'] not in fids:
                continue
            if len(fids) < 2:
                continue
            # a record overridden only by its own master chain's DLCs is still a conflict; plain one-plugin
            # duplicates (two records with one id in one file) are not
            rows.append((owner, local, rtype, fids))
        capped = len(rows) > scan_cap
        items, identical = [], 0
        for owner, local, rtype, fids in rows[:scan_cap]:
            item = {'formId': self._global(entries, owner, local), 'type': rtype,
                    'plugins': [by_file[f]['name'] for f in fids], 'winner': by_file[fids[-1]]['name'],
                    '_key': (owner, local)}
            item.update(self._compare(fids, owner, local, by_file, target))
            lost = any(o.get('lostInWinner') for o in item['overrides'])
            item['severity'] = 'lost' if lost else ('override' if item['winnerChanges'] else 'identical')
            if item['severity'] == 'identical':
                identical += 1
                if not include_identical:
                    continue
            items.append(item)
        rank = {'lost': 0, 'override': 1, 'identical': 2}
        items.sort(key=lambda x: (rank[x['severity']], -len(x['plugins']), x['type'], x['formId'] or ''))
        summary, severity = {}, {}
        for x in items:
            summary[x['type']] = summary.get(x['type'], 0) + 1
            severity[x['severity']] = severity.get(x['severity'], 0) + 1
        out = items[offset:offset + limit]
        for item in out:
            owner, local = item.pop('_key')
            edid = self.db.execute('select edid from records where owner = ? and local = ? and edid is not null limit 1',
                                   (owner, local)).fetchone()
            item['editorId'] = edid[0] if edid else None
        return {'count': len(items), 'bySeverity': severity, 'byType': summary, 'identicalSkipped':
                identical if not include_identical else 0, 'scanCapped': capped, 'conflicts': out,
                'more': len(items) > offset + limit,
                'plugin': target['name'] if target else None, 'indexBuilt': (built or {}).get('indexed') if built else None,
                'note': 'Plugins are in load order; the last one wins. severity lost = an earlier override changed '
                        'fields the winner sets back or differently (lostInWinner): the usual cause of a mod "not '
                        'working"; override = the winner changes the record; identical = overrides that change '
                        'nothing (skipped unless include_identical).'}

    def _compare(self, fids, owner, local, by_file, target):
        versions = []
        for fid in fids:
            row = self.db.execute('select offset from records where file = ? and owner = ? and local = ?',
                                  (fid, owner, local)).fetchone()
            try:
                versions.append((by_file[fid]['name'], _fields(read_record(by_file[fid]['path'], row[0]))))
            except Exception as e:  # unreadable record: report, keep going
                versions.append((by_file[fid]['name'], {'__error__': [str(e).encode()]}))
        base_name, base = versions[0]
        winner_name, winner = versions[-1]
        overrides = []
        for name, fields in versions[1:]:
            changed = sorted(t for t in set(base) | set(fields) if base.get(t) != fields.get(t))
            entry = {'plugin': name, 'changes': changed, 'identicalToMaster': not changed}
            if name != winner_name and changed:
                lost = [t for t in changed if winner.get(t) != fields.get(t)]
                if lost:
                    entry['lostInWinner'] = lost
            overrides.append(entry)
        return {'overrides': overrides,
                'winnerChanges': sorted(t for t in set(base) | set(winner) if base.get(t) != winner.get(t))}

    # ------------------------------------------------------------------ fnv_asset_check
    def asset_check(self, plugin, include_nif_textures=True, limit=200):
        project = self.data.project
        entries = load_order(project, self.data._live_plugins())
        target = next((e for e in entries if e['name'].lower() == plugin.lower()), None)
        if target is None:
            path = Path(plugin)
            if not path.is_file():
                raise DataError('PLUGIN_NOT_FOUND', '%s is neither in the load order nor a file' % plugin)
            target = {'name': path.name, 'path': str(path), 'mod': None}
        if not target.get('path'):
            raise DataError('PLUGIN_NOT_FOUND', '%s: file not found' % plugin)
        index, roots = self._asset_index(entries)
        names = _plugin_assets(target['path'])
        missing, found, nif_missing = [], 0, []
        checked_nifs = set()
        for rel, users in sorted(names.items()):
            hit = index.find(rel)
            if hit:
                found += 1
                if include_nif_textures and rel.lower().endswith('.nif') and rel.lower() not in checked_nifs:
                    checked_nifs.add(rel.lower())
                    nif_missing.extend(self._nif_missing(index, rel, hit))
                continue
            missing.append({'path': rel, 'usedBy': users[:5], 'users': len(users)})
        return {'plugin': target['name'], 'referenced': len(names), 'found': found, 'missing': missing[:limit],
                'missingCount': len(missing), 'nifTexturesMissing': nif_missing[:limit],
                'searched': {'looseRoots': [str(r) for r in roots], 'archives': index.archives},
                'note': 'Paths are relative to Data. Loose files are searched in the enabled MO2 mods (highest '
                        'priority first) and Data; archives are every BSA in those folders. NIF textures are read '
                        'from the meshes this plugin names (loose meshes only).'}

    def _asset_index(self, entries):
        project = self.data.project
        fnv = project.fnv
        data_dir = Path(project.game_root) / 'Data'
        roots = []
        if fnv and fnv.mo2_root and fnv.mo2_profile:
            mods_dir = Path(fnv.mo2_root) / 'mods'
            modlist = Path(fnv.mo2_root) / 'profiles' / fnv.mo2_profile / 'modlist.txt'
            if modlist.exists():
                for line in modlist.read_text(encoding='utf-8', errors='replace').splitlines():
                    if line.startswith('+') and (mods_dir / line[1:]).is_dir():
                        roots.append(mods_dir / line[1:])
        bsas = []
        for root in roots + [data_dir]:
            bsas.extend(sorted(root.glob('*.bsa')))
        index = assets.AssetIndex(None, roots + [data_dir], bsas)
        return index, roots + [data_dir]

    @staticmethod
    def _nif_missing(index, rel, hit):
        if hit.get('where') != 'loose':
            return []
        try:
            data = Path(hit['path']).read_bytes()
            textures = assets.nif_textures(data)
        except (OSError, assets.AssetError):
            return []
        return [{'texture': t, 'mesh': rel} for t in textures if not index.find(t)]

    # ------------------------------------------------------------------ fnv_profile_set
    def profile_set(self, mods=None, plugins=None, game=None, dry_run=False):
        """mods / plugins: {name: true|false}. Refuses while the game runs."""
        project = self.data.project
        fnv = project.fnv
        if not fnv or not fnv.mo2_root or not fnv.mo2_profile:
            raise DataError('NO_MO2_PROFILE', 'Set fnv.mo2_root and fnv.mo2_profile in the project file')
        if game is not None and game.process():
            raise DataError('GAME_RUNNING', 'Quit the game first: MO2 reads the profile when the game starts')
        profile = Path(fnv.mo2_root) / 'profiles' / fnv.mo2_profile
        mods_dir = Path(fnv.mo2_root) / 'mods'
        files = {n: profile / n for n in ('modlist.txt', 'plugins.txt', 'loadorder.txt')}
        text = {n: (p.read_text(encoding='utf-8').splitlines() if p.exists() else []) for n, p in files.items()}
        changes, errors = [], []
        modlist = text['modlist.txt']
        for name, on in (mods or {}).items():
            idx = next((i for i, l in enumerate(modlist) if l[:1] in '+-' and l[1:].lower() == name.lower()), None)
            if idx is None:
                if not (mods_dir / name).is_dir():
                    errors.append({'mod': name, 'error': 'no such mod folder in %s' % mods_dir})
                    continue
                header = 1 if modlist and modlist[0].startswith('#') else 0
                modlist.insert(header, ('+' if on else '-') + name)
                changes.append({'mod': name, 'now': 'enabled' if on else 'disabled', 'was': 'not listed'})
                continue
            was = modlist[idx][0] == '+'
            if was != bool(on):
                modlist[idx] = ('+' if on else '-') + modlist[idx][1:]
                changes.append({'mod': name, 'now': 'enabled' if on else 'disabled',
                                'was': 'enabled' if was else 'disabled'})
        active, order = text['plugins.txt'], text['loadorder.txt']
        for name, on in (plugins or {}).items():
            low = name.lower()
            if low in ('falloutnv.esm',) and not on:
                errors.append({'plugin': name, 'error': 'FalloutNV.esm cannot be disabled'})
                continue
            in_active = next((i for i, l in enumerate(active) if l.strip().lstrip('*').lower() == low), None)
            if on and in_active is None:
                active.append(name)
                if not any(l.strip().lstrip('*').lower() == low for l in order):
                    order.append(name)
                changes.append({'plugin': name, 'now': 'active', 'was': 'inactive'})
            elif not on and in_active is not None:
                del active[in_active]
                changes.append({'plugin': name, 'now': 'inactive', 'was': 'active'})
        backup = None
        if changes and not dry_run:
            backup = Path(project.state_dir) / 'fnv' / 'profile-backups' / time.strftime('%Y%m%d-%H%M%S')
            backup.mkdir(parents=True, exist_ok=True)
            for n, p in files.items():
                if p.exists():
                    shutil.copy2(p, backup / n)
            for n, p in files.items():
                if text[n] or p.exists():
                    p.write_text('\n'.join(text[n]) + '\n', encoding='utf-8')
        after = load_order(project) if changes and not dry_run else None
        problems = [e for e in (after or []) if e.get('missingMasters')]
        return {'changed': changes, 'errors': errors, 'dryRun': bool(dry_run), 'backup': str(backup) if backup else None,
                'missingMasters': problems or None,
                'note': 'Takes effect on the next launch. The backup folder holds the three profile files as they were.'}


def _fields(record):
    out = {}
    for s in record.subrecords:
        out.setdefault(s.tag, []).append(bytes(s.data))
    return out


HOW = {'NAME': 'placed reference of it', 'SCRO': 'script references it', 'CNTO': 'holds it (container/inventory)',
       'LVLO': 'leveled list entry', 'CTDA': 'condition names it', 'PKID': 'AI package', 'SCRI': 'script attached',
       'XOWN': 'owner', 'XESP': 'enable parent', 'XTEL': 'door teleport target', 'SPLO': 'spell or ability',
       'TCLT': 'dialogue choice', 'TCLF': 'dialogue link from', 'QSTI': 'dialogue quest', 'TPIC': 'topic',
       'XLKR': 'linked reference', 'XEZN': 'encounter zone', 'INAM': 'item/info'}


def _how(rtype, tag):
    if rtype == 'FLST' and tag == 'LNAM':
        return 'form list entry'
    if rtype in ('INFO', 'DIAL') and tag == 'CTDA':
        return 'dialogue condition'
    return HOW.get(tag, '%s field' % tag)


def _plugin_assets(path):
    """{relative path: [record EditorIDs or ids]} for every asset file a plugin names."""
    from .esp import codec
    data = Path(path).read_bytes()
    out = {}
    for record, _groups in codec.iter_records(data):
        rtype = record.type
        if rtype in ('LAND', 'NAVM'):
            continue
        name = record.editor_id or '%08X' % record.form_id
        for s in record.subrecords:
            folder = ASSET_TYPES_TAG.get((rtype, s.tag), ASSET_TAGS.get(s.tag))
            if folder is None:
                continue
            if rtype == 'NPC_' and s.tag == 'NAM1':
                continue
            text = codec.cstring(s.data).strip().replace('/', '\\')
            if not text or text.rsplit('.', 1)[-1].lower() not in ASSET_EXTENSIONS or len(text) > 260:
                continue
            prefix = folder
            rel = text.lstrip('\\')
            if prefix and not rel.lower().startswith(prefix + '\\'):
                rel = prefix + '\\' + rel
            if folder == 'sound' and rel.lower().endswith('.wav'):
                pass
            out.setdefault(rel, []).append(name)
    return out
