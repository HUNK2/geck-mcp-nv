"""Live fnv_* tools on top of the engine companion: inspection (milestone 5) and full control (milestone 6).

Reads use xNVSE script expressions (evaluated by the running game) and engine memory decoded with the
decomp's layouts (fnv_facts, engine map). Act tools change the running game; every change is journaled
in state/fnv/journal.jsonl with what is needed to undo it, and saves are backed up before the first change
of a game session. Act tools refuse unless the project sets fnv.engine_control = "full".
"""
import json
import math
import re
import struct
import time
import uuid
from pathlib import Path

from . import fnv_engine, fnv_facts, fnv_link
from .fnv_facts import FACTS
from .fnv_game import Game, GameError
from .fnv_link import LinkError, element_value, hexs, word

FORM_ID = re.compile(r'^(?:0x)?([0-9A-Fa-f]{1,8})$')
PLUGIN_LOCAL = re.compile(r'^([A-Za-z0-9_. \'-]{1,80}\.es[mp]):(?:0x)?([0-9A-Fa-f]{1,6})$', re.I)
EDITOR_ID = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')
SIMPLE_TYPES = {'bool': ('<?', 1), 'char': ('<b', 1), 'signed char': ('<b', 1), 'unsigned char': ('<B', 1),
                'UInt8': ('<B', 1), 'SInt8': ('<b', 1), 'short': ('<h', 2), 'unsigned short': ('<H', 2),
                'UInt16': ('<H', 2), 'SInt16': ('<h', 2), 'int': ('<i', 4), 'unsigned int': ('<I', 4),
                'long': ('<i', 4), 'unsigned long': ('<I', 4), 'UInt32': ('<I', 4), 'SInt32': ('<i', 4),
                'float': ('<f', 4), 'double': ('<d', 8), 'u8': ('<B', 1), 'i8': ('<b', 1), 'u16': ('<H', 2),
                'i16': ('<h', 2), 'u32': ('<I', 4), 'i32': ('<i', 4), 'f32': ('<f', 4), 'f64': ('<d', 8),
                'ptr': ('<I', 4)}


class LiveError(Exception):
    def __init__(self, code, message, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence


def _num(value):
    """A number as console text: integers without a decimal point (some parameters take integers only)."""
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def _fid(value):
    return '%08X' % value


class Journal:
    """Append-only record of every change, with what undoes it. One file per bridge state folder."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def entries(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def add(self, entry):
        entry = {'id': uuid.uuid4().hex[:12], 'time': time.time(), 'undone': False, **entry}
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
        return entry

    def mark_undone(self, ids):
        entries = self.entries()
        for e in entries:
            if e['id'] in ids:
                e['undone'] = True
                e['undoneAt'] = time.time()
        self.path.write_text(''.join(json.dumps(e) + '\n' for e in entries), encoding='utf-8')


class LiveService:
    def __init__(self, project):
        self.project = project
        self._game = None
        self._engine = None
        self._rtti = {}
        self.journal = Journal(Path(project.state_dir) / 'fnv/journal.jsonl')

    # ================================================================= plumbing
    @property
    def game(self):
        if self._game is None:
            self._game = Game(self.project)
        return self._game

    @property
    def link(self):
        return self.game.link

    def engine(self):
        if self._engine is None:
            path, decomp = fnv_engine.default_paths(self.project)
            self._engine = fnv_engine.load(path, decomp)
        return self._engine

    def run(self, name, action, kind='read'):
        started = time.monotonic()
        operation_id = str(uuid.uuid4())
        try:
            if kind == 'act' and getattr(self.project.fnv, 'engine_control', 'read') != 'full':
                raise LiveError('CONTROL_DISABLED', 'Act tools are off for this project: set fnv.engine_control to '
                                '"full" in the project file to allow changes to the running game')
            data = action()
            changed = kind != 'read' and not (isinstance(data, dict) and data.get('changed') is False)
            return {'ok': True, 'operationId': operation_id, 'operation': name, 'projectId': self.project.project_id,
                    'outcome': 'acknowledged' if changed else 'unchanged', 'data': data, 'error': None,
                    'game': self.identity(), 'elapsedMs': round((time.monotonic() - started) * 1000)}
        except Exception as e:  # reported, never raised to the client
            code = getattr(e, 'code', 'INVALID_ARGUMENT' if isinstance(e, ValueError) else 'FNV_ERROR')
            uncertain = code in ('COMPANION_TIMEOUT', 'COMPANION_DISCONNECTED') and kind != 'read'
            return {'ok': False, 'operationId': operation_id, 'operation': name, 'projectId': self.project.project_id,
                    'outcome': 'outcome_unknown' if uncertain else 'failed_before_change',
                    'data': getattr(e, 'evidence', None),
                    'error': {'code': code, 'message': str(e), 'retrySafe': not uncertain,
                              'recovery': 'Check fnv_status / fnv_journal before retrying' if uncertain else
                              'Fix the input or the game state (fnv_status) and call again.'},
                    'game': self.identity(), 'elapsedMs': round((time.monotonic() - started) * 1000)}

    def identity(self):
        hello = self._game.link.hello if self._game else None
        if not hello:
            return None
        return {'pid': hello.get('pid'), 'started': hello.get('started'), 'frame': hello.get('frame')}

    def ready(self):
        """The game runs and the companion answers; returns the ping."""
        ping = self.link.ping()
        if not ping.get('deferredInit'):
            raise LiveError('GAME_STARTING', 'The game is still starting; wait or call fnv_launch')
        return ping

    def in_game(self):
        ping = self.ready()
        if not ping.get('loaded'):
            phase, _ = self.game.phase(ping)
            if phase != 'in_game' and phase != 'in_menu' and phase != 'paused_menu':
                raise LiveError('NO_GAME_LOADED', 'No game is loaded (phase %s): use fnv_launch with a save or cell' % phase)
        return ping

    def changing(self, reason):
        """Before the first change of a game session: back up the dev profile's saves."""
        return self.game.backup_saves(reason)

    # ================================================================= low-level helpers
    def evals(self, expressions, ref=0):
        ops = [('eval', hexs(e), word(ref)) for e in expressions]
        return self.link.batch(ops)

    def values(self, expressions, ref=0):
        return [fnv_link.value_of(r) for r in self.evals(expressions, ref)]

    def console(self, line, ref=0):
        """Run one console line with its printed output captured (see fnv_facts console flags)."""
        verbose, visible = FACTS['verbose_print_flag']['address'], FACTS['console_visible_flag']['address']
        old = self.link.batch([('rd', word(verbose), '1'), ('rd', word(visible), '1')])
        if not all(r.get('ok') for r in old):
            raise LiveError('ENGINE_MISMATCH', 'Console flags unreadable: is this FalloutNV 1.4.0.525?', old)
        results = self.link.batch([('wr', word(verbose), '01'), ('wr', word(visible), '01'), ('con', hexs(line), word(ref)),
                                   ('wr', word(visible), old[1]['hex']), ('wr', word(verbose), old[0]['hex'])])
        con = results[2]
        output = (con.get('output') or '').replace('\r', '').rstrip('\n')
        lines = output.split('\n') if output else []
        # The console compiler reports a bad line by printing and still returns success to RunScriptLine.
        compile_error = any('Compiled script not saved' in l or "Script 'SysWindowCompileAndRun'" in l for l in lines)
        return {'line': line, 'accepted': bool(con.get('accepted')) and not compile_error, 'compileError': compile_error,
                'output': lines, 'scriptErrors': con.get('scriptErrors', 0), 'outputCaptured': con.get('captured'),
                'frame': con.get('frame')}

    def u32s(self, addresses):
        results = self.link.batch([('rd', word(a), '4') for a in addresses])
        return [struct.unpack('<I', bytes.fromhex(r['hex']))[0] if r.get('ok') else None for r in results]

    def reads(self, spans):
        results = self.link.batch([('rd', word(a), word(n)) for a, n in spans])
        return [bytes.fromhex(r['hex']) if r.get('ok') else None for r in results]

    def walk_list(self, head_address, limit=500):
        """BSSimpleList / tList at an address: nodes {data, next}; returns the data pointers (one frame with
        the companion's ls op, node by node with older companions)."""
        if not head_address:
            return []
        result = self.link.one('ls', word(head_address), word(min(limit, 4096)))
        if result.get('ok'):
            return [int(p, 16) for p in result.get('items', [])][:limit]
        out, node, seen = [], head_address, set()
        while node and node not in seen and len(out) < limit:
            seen.add(node)
            raw = self.reads([(node, 8)])[0]
            if raw is None:
                break
            data, nxt = struct.unpack('<II', raw)
            if data:
                out.append(data)
            node = nxt
        return out

    def rtti(self, pointers):
        """Class names of polymorphic objects from MSVC RTTI (vtable[-1] -> complete object locator)."""
        pointers = [p for p in pointers if p]
        vtables = dict(zip(pointers, self.u32s(pointers))) if pointers else {}
        unknown = sorted({v for v in vtables.values() if v and v not in self._rtti})
        if unknown:
            locators = dict(zip(unknown, self.u32s([v - 4 for v in unknown])))
            raw = dict(zip(unknown, self.reads([(locators[v] or 0x10, 0x14) for v in unknown])))
            names = {}
            descriptors = {}
            for v in unknown:
                if raw[v] is None:
                    self._rtti[v] = (None, 0)
                    continue
                _, offset, _, td, _ = struct.unpack('<IIIII', raw[v])
                descriptors[v] = (td, offset)
            texts = self.link.batch([('rs', word(td + 8), '100') for td, _ in descriptors.values()]) if descriptors else []
            for (v, (td, offset)), text in zip(descriptors.items(), texts):
                name = text.get('text') if text.get('ok') else None
                if name and name.startswith('.?AV') and name.endswith('@@'):
                    name = name[4:-2]
                names[v] = name
                self._rtti[v] = (name, offset)
        return {p: {'class': self._rtti.get(v, (None, 0))[0], 'subobjectOffset': self._rtti.get(v, (None, 0))[1]}
                for p, v in vtables.items()}

    def form_ids(self, pointers):
        """TESForm refID and type for form pointers (0 -> None)."""
        live = [p for p in pointers if p]
        raw = dict(zip(live, self.reads([(p + 4, 12) for p in live]))) if live else {}
        out = []
        for p in pointers:
            data = raw.get(p)
            if not p or data is None:
                out.append(None)
                continue
            form_type, flags, ref_id = data[0], struct.unpack('<I', data[4:8])[0], struct.unpack('<I', data[8:12])[0]
            out.append({'formId': _fid(ref_id), 'formType': form_type, 'ptr': '0x%08X' % p, 'flags': flags})
        return out

    # ================================================================= references
    def resolve(self, ref):
        """A reference/form spec -> {'ptr', 'formId', 'expr'}.

        Accepts: player | crosshair | form id hex (0x00000014, 14, 000A3E7C) | Plugin.esp:LOCALID |
        an EditorID the running game knows (persistent references, quests, globals...)."""
        ref = (ref or '').strip()
        if not ref:
            raise ValueError('ref is required')
        if ref.lower() in ('player', 'playerref'):
            expr = 'player'
        elif ref.lower() == 'crosshair':
            expr = 'GetCrosshairRef'
        elif PLUGIN_LOCAL.match(ref):
            plugin, local = PLUGIN_LOCAL.match(ref).groups()
            expr = '(BuildRef (GetModIndex "%s") %d)' % (plugin, int(local, 16))
        elif FORM_ID.match(ref) and (ref.lower().startswith('0x') or len(ref) == 8 or not EDITOR_ID.match(ref)):
            value = int(FORM_ID.match(ref).group(1), 16)
            expr = '(BuildRef %d %d)' % (value >> 24, value & 0xFFFFFF)
        elif EDITOR_ID.match(ref):
            expr = ref
        else:
            raise ValueError('Not a reference: %r (use player, crosshair, a form id or an EditorID)' % ref)
        result = self.link.eval(expr)
        element = result.get('result') or {}
        if (not result.get('ok') or element.get('type') != 'form' or not element.get('ptr')) and expr == ref:
            found = self.editor_id(ref)
            if found:
                expr = self.form_expr(found['formId'])
                result = self.link.eval(expr)
                element = result.get('result') or {}
        if not result.get('ok') or element.get('type') != 'form' or not element.get('ptr'):
            raise LiveError('REF_NOT_FOUND', 'No form for %r in the running game' % ref,
                            {'expression': expr, 'result': result})
        return {'ptr': int(element['ptr'], 16), 'formId': element.get('formId'), 'expr': expr, 'spec': ref}

    PREFERRED = ('QUST', 'REFR', 'ACHR', 'ACRE', 'NPC_', 'CREA', 'GLOB', 'WEAP', 'ARMO', 'ALCH', 'MISC', 'KEYM',
                 'AMMO', 'BOOK', 'CONT', 'DOOR', 'ACTI', 'STAT', 'CELL', 'PACK', 'SPEL', 'PERK', 'FACT')

    def editor_id(self, edid):
        """EditorID -> runtime form id via the offline master indexes (state/index) and the live load order:
        the running game keeps few EditorIDs in memory."""
        if not hasattr(self, '_indexes'):
            import gzip
            self._indexes = {}
            for path in sorted((Path(self.project.state_dir) / 'index').glob('*.json.gz')):
                try:
                    data = json.load(gzip.open(path))
                    self._indexes[data.get('master') or path.name[:-8]] = data.get('entries', {})
                except (OSError, ValueError):
                    continue
        hits = []
        for master, entries in self._indexes.items():
            found = entries.get(edid)
            if not found:
                continue
            for entry in (found if isinstance(found[0], list) else [found]):
                hits.append((master, entry))
        if not hits:
            return self._editor_id_from_plugins(edid)
        hits.sort(key=lambda h: self.PREFERRED.index(h[1][0]) if h[1][0] in self.PREFERRED else 99)
        master, (rtype, form_id, name, _model) = hits[0]
        index = fnv_link.value_of(self.link.eval('GetModIndex "%s"' % master))
        if index is None or index < 0 or index > 254:
            return None
        local = int(form_id, 16) & 0xFFFFFF
        return {'formId': _fid((int(index) << 24) | local), 'type': rtype, 'name': name, 'master': master,
                'candidates': len(hits)}

    def _editor_id_from_plugins(self, edid):
        """An EditorID defined in any active plugin (mods, DLC), from the load-order record index."""
        cache = self.__dict__.setdefault('_plugin_edids', {})
        if edid.lower() in cache:
            row = cache[edid.lower()]
        else:
            from .fnv_data import DataTools
            data = DataTools(self.project, self)
            try:
                entries = data._active()[0]
                files = ','.join(str(e['fileId']) for e in entries)
                row = data.index.db.execute('select owner, local, type, full from records where edid = ? collate nocase '
                                            'and file in (%s) limit 1' % files, (edid,)).fetchone()
            except Exception:
                row = None
            finally:
                if data._index is not None:
                    data._index.db.close()  # never keep the SQLite file open between calls
            cache[edid.lower()] = row
        if not row:
            return None
        owner, local, rtype, name = row
        index = fnv_link.value_of(self.link.eval('GetModIndex "%s"' % owner))
        if index is None or index < 0 or index > 254:
            return None
        return {'formId': _fid((int(index) << 24) | local), 'type': rtype, 'name': name, 'master': owner, 'candidates': 1}

    def form_expr(self, form_id):
        value = int(form_id, 16)
        return '(BuildRef %d %d)' % (value >> 24, value & 0xFFFFFF)

    def names(self, form_ids):
        ids = [f for f in form_ids if f]
        vals = self.values(['GetName %s' % self.form_expr(f) for f in ids]) if ids else []
        return dict(zip(ids, vals))

    # ================================================================= milestone 5: reads
    def player(self):
        self.in_game()
        p = self.resolve('player')
        exprs = ['GetPos X', 'GetPos Y', 'GetPos Z', 'GetAngle Z', 'GetParentCell', 'GetName (player.GetParentCell)',
                 'GetAV Health', 'GetBaseAV Health', 'GetAV ActionPoints', 'GetBaseAV ActionPoints', 'GetLevel',
                 'GetAV XP', 'GetAV Karma', 'GetAV RadiationRads', 'IsInCombat', 'GetDead', 'IsPlayerSwimming']
        exprs += ['GetAV %s' % n for n in fnv_facts.SPECIAL + fnv_facts.SKILLS]
        v = self.values(exprs, p['ptr'])
        n = 17
        return {'formId': p['formId'], 'position': {'x': v[0], 'y': v[1], 'z': v[2]}, 'heading': v[3],
                'cell': {'formId': v[4], 'name': v[5]}, 'health': {'current': v[6], 'base': v[7]},
                'actionPoints': {'current': v[8], 'base': v[9]}, 'level': v[10], 'xp': v[11], 'karma': v[12],
                'rads': v[13], 'inCombat': bool(v[14]), 'dead': bool(v[15]), 'swimming': bool(v[16]),
                'special': dict(zip(fnv_facts.SPECIAL, v[n:n + 7])), 'skills': dict(zip(fnv_facts.SKILLS, v[n + 7:])),
                'effects': self.effects('player', _checked=True)['effects'],
                'uses': ['xNVSE expressions (GetPos, GetAV, GetParentCell...)', 'fnv_effects engine read']}

    def ref(self, ref):
        self.in_game()
        r = self.resolve(ref)
        exprs = ['GetBaseObject', 'GetName', 'GetPos X', 'GetPos Y', 'GetPos Z', 'GetAngle X', 'GetAngle Y',
                 'GetAngle Z', 'GetScale', 'GetParentCell', 'GetDisabled', 'GetOwner', 'GetLocked', 'GetLockLevel',
                 'GetDead', 'GetAV Health', 'GetIsReference player']
        v = self.values(exprs, r['ptr'])
        head = self.form_ids([r['ptr']])[0]
        base = self.form_ids([self.u32s([r['ptr'] + 0x20])[0]])[0]
        info = {'formId': r['formId'], 'ptr': '0x%08X' % r['ptr'], 'formType': head['formType'] if head else None,
                'flags': [name for bit, name in fnv_facts.FORM_FLAGS.items() if head and head['flags'] & bit],
                'class': self.rtti([r['ptr']])[r['ptr']]['class'],
                'base': {'formId': v[0] or (base or {}).get('formId'), 'formType': (base or {}).get('formType'), 'name': v[1]},
                'position': {'x': v[2], 'y': v[3], 'z': v[4]}, 'rotation': {'x': v[5], 'y': v[6], 'z': v[7]},
                'scale': v[8], 'cell': v[9], 'disabled': bool(v[10]), 'owner': v[11], 'locked': bool(v[12]),
                'lockLevel': v[13], 'isPlayer': bool(v[16])}
        if info['class'] and ('Character' in info['class'] or 'Creature' in info['class'] or info['class'] == 'Actor'):
            info.update(dead=bool(v[14]), health=v[15])
        info['extraData'] = self.extra_data(ref, _resolved=r)['entries']
        info['uses'] = ['xNVSE expressions', 'TESForm/TESObjectREFR layout (decomp, checked offsets)', 'MSVC RTTI']
        return info

    def extra_data(self, ref, _resolved=None):
        if not _resolved:
            self.in_game()
        r = _resolved or self.resolve(ref)
        head = self.u32s([r['ptr'] + FACTS['TESObjectREFR.extraList']['offset'] + FACTS['BaseExtraList.head']['offset']])[0]
        entries, node, seen = [], head, set()
        while node and node not in seen and len(entries) < 200:
            seen.add(node)
            raw = self.reads([(node, 0x14)])[0]
            if raw is None:
                break
            kind, nxt = raw[4], struct.unpack('<I', raw[8:12])[0]
            entry = {'type': kind, 'name': fnv_facts.EXTRA_TYPES.get(kind, 'Unknown_%02X' % kind), 'ptr': '0x%08X' % node}
            field = fnv_facts.EXTRA_FIELDS.get(kind)
            if field:
                label, ftype = field
                if ftype in ('form', 'ptr'):
                    value = struct.unpack('<I', raw[12:16])[0]
                    entry[label] = ('0x%08X' % value) if ftype == 'ptr' else value
                else:
                    entry[label] = struct.unpack(SIMPLE_TYPES[ftype][0], raw[12:12 + SIMPLE_TYPES[ftype][1]])[0]
            entries.append(entry)
            node = nxt
        forms = [(e, k) for e in entries for k, v in list(e.items()) if isinstance(v, int) and k not in ('type',)
                 and fnv_facts.EXTRA_FIELDS.get(e['type'], (None, ''))[1] == 'form']
        decoded = self.form_ids([e[k] for e, k in forms])
        for (e, k), d in zip(forms, decoded):
            e[k] = d['formId'] if d else None
        for e in entries:
            if e['type'] == 0x2A and e.get('lockData') not in (None, '0x00000000'):
                lock = self.reads([(int(e['lockData'], 16), 12)])[0]
                if lock:
                    level, key, flags = struct.unpack('<IIB', lock[:9])
                    e['lock'] = {'level': level, 'key': (self.form_ids([key])[0] or {}).get('formId'), 'flags': flags}
        return {'formId': r['formId'], 'entries': entries,
                'uses': ['BaseExtraList/BSExtraData layout (decomp, checked)', 'xNVSE ExtraDataType names and fields']}

    def refs_near(self, radius=2000.0, form_type=0, limit=50, base=None):
        self.in_game()
        p = self.resolve('player')
        result = self.link.eval('GetRefs %d 1 0 %.1f' % (int(form_type), float(radius)), p['ptr'])
        element = result.get('result') or {}
        if not result.get('ok') or element.get('type') != 'array':
            raise LiveError('GETREFS_FAILED', 'GetRefs returned no array', result)
        items = [i for i in element.get('items', []) if i.get('type') == 'form' and i.get('ptr')]
        total = element.get('size', len(items))
        out = []
        ptrs = [int(i['ptr'], 16) for i in items]
        per = ['GetBaseObject', 'GetName', 'GetDistance player', 'GetPos X', 'GetPos Y', 'GetPos Z', 'GetDisabled', 'GetDead']
        ops = [('eval', hexs(e), word(ptr)) for ptr in ptrs for e in per]
        values = [fnv_link.value_of(r) for r in self.link.batch(ops)] if ops else []
        for k, item in enumerate(items):
            v = values[k * len(per):(k + 1) * len(per)]
            if base and v[0] != base.upper().removeprefix('0X').zfill(8):
                continue
            out.append({'formId': item['formId'], 'base': v[0], 'name': v[1], 'distance': v[2],
                        'position': {'x': v[3], 'y': v[4], 'z': v[5]}, 'disabled': bool(v[6]), 'dead': bool(v[7])})
        out.sort(key=lambda e: e['distance'] if e['distance'] is not None else 1e12)
        return {'radius': radius, 'formType': form_type, 'count': len(out), 'arrayTruncated': total > len(items),
                'refs': out[:limit], 'truncated': len(out) > limit, 'uses': ['xNVSE GetRefs', 'xNVSE expressions']}

    def actor_ai(self, ref):
        self.in_game()
        r = self.resolve(ref)
        exprs = ['GetCurrentAIPackage', 'GetCurrentAIProcedure', 'GetCurrentPackage', 'IsInCombat', 'GetCombatTarget',
                 'GetDetected player', 'player.GetDetected %s' % r['expr'] if r['expr'] != 'player' else '0',
                 'GetDead', 'GetUnconscious', 'GetSitting', 'GetSleeping', 'GetKnockedState', 'GetRestrained',
                 'GetPlayerTeammate', 'GetIsCreature', 'GetAV Aggression', 'GetAV Confidence', 'GetAV Assistance',
                 'GetAV Responsibility', 'GetAV Mood']
        v = self.values(exprs, r['ptr'])
        process = self.u32s([r['ptr'] + FACTS['MobileObject.baseProcess']['offset']])[0]
        level = self.u32s([process + FACTS['BaseProcess.processType']['offset']])[0] if process else None
        package = v[2]
        return {'formId': r['formId'], 'package': {'formId': package, 'type': v[0], 'procedure': v[1]},
                'processLevel': {0: 'high', 1: 'middle_high', 2: 'middle_low', 3: 'low'}.get(level, level),
                'processPtr': '0x%08X' % process if process else None,
                'combat': {'inCombat': bool(v[3]), 'target': v[4]},
                'detection': {'detectsPlayer': bool(v[5]), 'detectedByPlayer': bool(v[6])},
                'state': {'dead': bool(v[7]), 'unconscious': bool(v[8]), 'sitting': v[9], 'sleeping': v[10],
                          'knocked': v[11], 'restrained': bool(v[12]), 'teammate': bool(v[13]), 'creature': bool(v[14])},
                'aiValues': dict(zip(['Aggression', 'Confidence', 'Assistance', 'Responsibility', 'Mood'], v[15:20])),
                'uses': ['vanilla/xNVSE AI functions (GetCurrentAIPackage, GetCurrentAIProcedure, GetCurrentPackage)',
                         'MobileObject.m_pBaseProcess, BaseProcess.m_eProcessType (decomp, checked)']}

    def quest(self, quest):
        self.in_game()
        q = self.resolve(quest)
        v = self.values(['GetQuestRunning %s' % q['expr'], 'GetStage %s' % q['expr'], 'GetQuestCompleted %s' % q['expr'],
                         'GetName %s' % q['expr']])
        stage_ptrs = self.walk_list(q['ptr'] + FACTS['TESQuest.stages']['offset'], 300)
        stages = [{'index': raw[0], 'done': bool(raw[1])} for raw in self.reads([(p, 2) for p in stage_ptrs]) if raw] \
            if stage_ptrs else []
        objectives = []
        objs = self.walk_list(q['ptr'] + FACTS['TESQuest.objectives']['offset'], 200)
        raws = self.reads([(o, 0x24) for o in objs])
        texts = self.link.batch([('rs', word(struct.unpack('<I', raw[8:12])[0]), '400') for raw in raws if raw]) if objs else []
        for raw, text in zip([r for r in raws if r], texts):
            index, state = struct.unpack('<I', raw[4:8])[0], struct.unpack('<I', raw[0x20:0x24])[0]
            objectives.append({'index': index, 'text': text.get('text') if text.get('ok') else None,
                               'displayed': bool(state & 1), 'completed': bool(state & 2), 'state': state})
        return {'formId': q['formId'], 'name': v[3], 'running': bool(v[0]), 'stage': v[1], 'completed': bool(v[2]),
                'stages': stages, 'objectives': objectives,
                'variables': self._variables(q['ptr'], 'quest')['variables'],
                'uses': ['vanilla quest functions', 'TESQuest stage/objective lists (decomp, checked)',
                         'BGSQuestObjective layout (decomp); state bit 1 displayed, 2 completed (xNVSE)']}

    def _variables(self, ptr, kind):
        if kind == 'quest':
            script = self.u32s([ptr + FACTS['TESQuest.script']['offset']])[0]
            local = self.u32s([ptr + FACTS['TESQuest.locals']['offset']])[0]
        else:
            script = local = None
            for e in self.extra_data(None, _resolved={'ptr': ptr, 'formId': None})['entries']:
                if e['type'] == 0x0D:
                    raw = self.reads([(int(e['ptr'], 16) + 0x0C, 8)])[0]
                    if raw:
                        script, local = struct.unpack('<II', raw)
        if not script:
            return {'script': None, 'variables': []}
        infos = self.walk_list(script + FACTS['Script.variables']['offset'], 500)
        raws = self.reads([(i, 0x20) for i in infos])
        names = self.link.batch([('rs', word(struct.unpack('<I', raw[0x18:0x1C])[0]), '200') for raw in raws if raw]) if infos else []
        values = {}
        if local:
            vars_list = self.u32s([local + FACTS['ScriptLocals.variables']['offset']])[0]
            var_ptrs = self.walk_list(vars_list, 500) if vars_list else []
            for raw in self.reads([(v, 16) for v in var_ptrs]) if var_ptrs else []:
                if raw:
                    values[struct.unpack('<I', raw[:4])[0]] = raw[8:16]
        out = []
        for raw, name in zip([r for r in raws if r], names):
            idx, vtype = struct.unpack('<I', raw[:4])[0], raw[0x10]
            data = values.get(idx)
            if data is None:
                value = None
            elif vtype == 4:
                value = _fid(struct.unpack('<I', data[:4])[0])
            else:
                value = struct.unpack('<d', data)[0]
            out.append({'index': idx, 'name': name.get('text') if name.get('ok') else None,
                        'type': {0: 'float', 1: 'int', 2: 'string', 3: 'array', 4: 'ref'}.get(vtype, vtype), 'value': value})
        return {'script': (self.form_ids([script])[0] or {}).get('formId'), 'variables': out}

    def variables(self, ref):
        self.in_game()
        r = self.resolve(ref)
        head = self.form_ids([r['ptr']])[0]
        kind = 'quest' if head and head['formType'] == 0x47 else 'ref'
        result = self._variables(r['ptr'], kind)
        return {'formId': r['formId'], 'kind': kind, **result,
                'uses': ['Script variable list and ScriptLocals (decomp layouts; xNVSE VariableInfo)']}

    def actor_values(self, ref, names=None):
        self.in_game()
        r = self.resolve(ref)
        names = names or fnv_facts.ACTOR_VALUES
        bad = [n for n in names if n not in fnv_facts.ACTOR_VALUES]
        if bad:
            raise ValueError('Unknown actor values: %s' % ', '.join(bad))
        exprs = [e % n for n in names for e in ('GetAV %s', 'GetBaseAV %s', 'GetPermanentActorValue %s')]
        v = self.values(exprs, r['ptr'])
        effects = self.effects(ref, _checked=True).get('effects', [])
        out = {}
        for k, name in enumerate(names):
            cur, base, perm = v[3 * k:3 * k + 3]
            code = fnv_facts.ACTOR_VALUES.index(name)
            entry = {'current': cur, 'base': base, 'permanent': perm,
                     'permanentModifier': None if None in (perm, base) else perm - base,
                     'temporaryModifier': None if None in (cur, perm) else cur - perm,
                     'sources': [{'effect': e['effect'], 'source': e.get('sourceName') or e.get('source'),
                                  'magnitude': e['magnitude']} for e in effects if e.get('actorValue') == code]}
            out[name] = entry
        return {'formId': r['formId'], 'values': out,
                'uses': ['vanilla GetAV/GetBaseAV/GetPermanentActorValue', 'fnv_effects for the contributing effects']}

    def effects(self, ref, _checked=False):
        if not _checked:
            self.in_game()
        r = self.resolve(ref)
        target = r['ptr'] + FACTS['Actor.magicTarget']['offset']
        vtable = self.u32s([target])[0]
        if not vtable:
            raise LiveError('NOT_AN_ACTOR', '%s has no magic target' % ref)
        fn = self.u32s([vtable + 4 * FACTS['MagicTarget.GetEffectList']['vtableSlot']])[0]
        call = self.link.one('call', word(fn), 'i', word(target), '0')
        if not call.get('ok'):
            raise LiveError('CALL_FAILED', 'MagicTarget::GetEffectList failed', call)
        lst = int(call['eax'], 16)
        effects = []
        eff_ptrs = self.walk_list(lst, 300) if lst else []
        raws = self.reads([(e, 0x48) for e in eff_ptrs]) if eff_ptrs else []
        effect_items = [struct.unpack('<I', raw[0x0C:0x10])[0] if raw else 0 for raw in raws]
        wanted = sorted({p for p in effect_items if p})
        eis = dict(zip(wanted, self.reads([(p, 0x18) for p in wanted]))) if wanted else {}
        for eff, raw in zip(eff_ptrs, raws):
            if raw is None:
                continue
            elapsed, item, effect_item = struct.unpack('<fII', raw[4:16])
            active, applied, terminated = raw[0x10], raw[0x11], raw[0x13]
            magnitude, duration = struct.unpack('<ff', raw[0x1C:0x24])
            spell_type, source = struct.unpack('<I', raw[0x2C:0x30])[0], struct.unpack('<I', raw[0x3C:0x40])[0]
            ei = eis.get(effect_item)
            setting = struct.unpack('<I', ei[0x14:0x18])[0] if ei else 0
            av = struct.unpack('<I', ei[0x10:0x14])[0] if ei else None
            effects.append({'ptr': '0x%08X' % eff, 'item': item, 'setting': setting, 'source': source,
                            'active': bool(active), 'applied': bool(applied), 'terminated': bool(terminated),
                            'magnitude': magnitude, 'duration': duration, 'elapsed': elapsed, 'spellType': spell_type,
                            'actorValue': av if av is not None and av < len(fnv_facts.ACTOR_VALUES) else None})
        # Magic items are subobjects of their forms: RTTI gives the offset back to the TESForm.
        item_ptrs = [e['item'] for e in effects]
        info = self.rtti(item_ptrs)
        forms = self.form_ids([e['item'] - info.get(e['item'], {}).get('subobjectOffset', 0) if e['item'] else 0
                               for e in effects] + [e['setting'] for e in effects] + [e['source'] for e in effects])
        n = len(effects)
        ids = [f['formId'] for f in forms if f]
        named = self.names(ids)
        for k, e in enumerate(effects):
            item, setting, source = forms[k], forms[n + k], forms[2 * n + k]
            e['item'] = item['formId'] if item else None
            e['itemClass'] = info.get(item_ptrs[k], {}).get('class')
            e['itemName'] = named.get(e['item'])
            e['effect'] = setting['formId'] if setting else None
            e['effectName'] = named.get(e['effect'])
            e['source'] = source['formId'] if source else None
            e['sourceName'] = named.get(e['source'])
            e['actorValueName'] = fnv_facts.ACTOR_VALUES[e['actorValue']] if e['actorValue'] is not None else None
            del e['setting']
        return {'formId': r['formId'], 'count': n, 'effects': effects,
                'uses': ['MagicTarget at Actor+0x94, virtual GetEffectList (JIP LN layout, decomp sizes)',
                         'ActiveEffect/EffectItem layout (JIP LN GameEffects.h/GameForms.h)', 'MSVC RTTI']}

    def crosshair(self):
        self.in_game()
        result = self.link.eval('GetCrosshairRef')
        element = result.get('result') or {}
        if element.get('type') != 'form' or not element.get('ptr'):
            return {'ref': None}
        ptr = int(element['ptr'], 16)
        player = self.resolve('player')
        v = self.values(['GetBaseObject', 'GetName', 'GetDistance player'], ptr)
        return {'ref': element.get('formId'), 'base': v[0], 'name': v[1], 'distance': v[2],
                'class': self.rtti([ptr])[ptr]['class'], 'player': player['formId'], 'uses': ['xNVSE GetCrosshairRef']}

    def inventory(self, ref='player', limit=300):
        self.in_game()
        r = self.resolve(ref)
        count = fnv_link.value_of(self.link.eval('GetNumItems', r['ptr']))
        if count is None:
            raise LiveError('NO_INVENTORY', '%s has no inventory' % ref)
        n = min(int(count), limit)
        objs = self.link.batch([('eval', hexs('GetInventoryObject %d' % i), word(r['ptr'])) for i in range(n)])
        ids = [(o.get('result') or {}).get('formId') for o in objs]
        live = [i for i in ids if i]
        per = ['GetItemCount %s', 'GetEquipped %s', 'GetName %s']
        values = self.values([e % self.form_expr(i) for i in live for e in per], r['ptr']) if live else []
        items = []
        for k, form in enumerate(live):
            c, eq, name = values[3 * k:3 * k + 3]
            items.append({'formId': form, 'name': name, 'count': c, 'equipped': bool(eq)})
        return {'formId': r['formId'], 'count': int(count), 'items': items, 'truncated': int(count) > limit,
                'uses': ['xNVSE GetNumItems/GetInventoryObject', 'vanilla GetItemCount/GetEquipped']}

    def formid_live(self, plugin):
        self.ready()
        v = self.values(['IsModLoaded "%s"' % plugin, 'GetModIndex "%s"' % plugin])
        loaded = bool(v[0])
        return {'plugin': plugin, 'loaded': loaded, 'index': '%02X' % int(v[1]) if loaded and v[1] is not None else None,
                'formIdPrefix': ('%02X' % int(v[1])) if loaded else None}

    def events(self, since=0):
        self.ready()
        return self.link.events(since)

    def script_errors(self, since='last'):
        """Script errors from the console log (with script, mod and message) since the last call."""
        logs = self.game.logs(['console'], since=since if since != 'last' else 'last', max_lines=2000)
        lines = logs.get('console', {}).get('lines', [])
        errors, current = [], None
        for line in lines:
            m = re.match(r'Error in script ([0-9A-Fa-f]{8})(?: \((.*?)\))? in mod (.*)$', line.strip())
            if m:
                current = {'script': m.group(1).upper(), 'scriptName': m.group(2), 'mod': m.group(3), 'message': []}
                errors.append(current)
            elif current is not None and line.strip() and not line.startswith(('GetActorValue', 'GetPos')):
                current['message'].append(line.strip())
                if len(current['message']) >= 6:
                    current = None
        for e in errors:
            e['message'] = '\n'.join(e['message'])
        return {'errors': errors, 'count': len(errors), 'log': logs.get('console', {}).get('path'),
                'uses': ['console log (scof) written by the engine companion']}

    # ================================================================= milestone 5/6: console and scripts
    def run_console(self, line, ref=None):
        self.ready()
        if '\n' in line or len(line) > 500:
            raise ValueError('One console line of at most 500 characters')
        r = self.resolve(ref) if ref else None
        self.changing('console: ' + line)
        result = self.console(line, r['ptr'] if r else 0)
        self.journal.add({'kind': 'console', 'line': line, 'ref': r['formId'] if r else None, 'undo': None,
                          'output': result['output'][:20]})
        return result

    def run_script(self, source, ref=None):
        self.ready()
        r = self.resolve(ref) if ref else None
        self.changing('script')
        text = source.strip()
        single = '\n' not in text and not text.lower().startswith('begin')
        if single:
            result = self.link.eval(text, r['ptr'] if r else 0)
        else:
            if not text.lower().startswith('begin'):
                text = 'Begin Function { }\n%s\nEnd' % text
            result = self.link.run(text, r['ptr'] if r else 0)
        substituted = {}
        if result.get('error') == 'compile_failed' or not result.get('ok'):
            # The running game keeps few EditorIDs, so its compiler cannot resolve most base forms by name:
            # retry with the EditorIDs it does not know replaced by their runtime form ids.
            retry, substituted = self.substitute_editor_ids(text)
            if substituted:
                result = self.link.eval(retry, r['ptr'] if r else 0) if single else self.link.run(retry, r['ptr'] if r else 0)
        self.journal.add({'kind': 'script', 'source': source[:2000], 'ref': r['formId'] if r else None, 'undo': None})
        return {'compiled': result.get('error') != 'compile_failed', 'ok': result.get('ok'),
                'result': element_value(result.get('result') or {}), 'resultType': (result.get('result') or {}).get('type'),
                'scriptErrors': result.get('scriptErrors'), 'output': result.get('output'), 'mode': 'expression' if single else 'script',
                'substitutedEditorIds': substituted or None}

    KEYWORDS = {'begin', 'end', 'if', 'elseif', 'else', 'endif', 'set', 'to', 'let', 'return', 'short', 'int', 'long',
                'float', 'ref', 'reference', 'array_var', 'string_var', 'player', 'playerref', 'while', 'loop', 'foreach',
                'function', 'call', 'scn', 'scriptname', 'gamemode', 'menumode'}

    def substitute_editor_ids(self, text):
        """Replace EditorIDs found in the offline indexes (not commands, keywords or quoted text) with their runtime
        form ids, which the in-game compiler accepts. Returns (new text, {EditorID: form id})."""
        commands = getattr(self, '_command_names', None)
        if commands is None:
            commands = set()
            try:
                for c in self.engine().commands:
                    commands.add(c['name'].lower())
                    if c.get('short'):
                        commands.add(c['short'].lower())
            except Exception:
                pass
            self._command_names = commands
        out, subs = [], {}
        # The script's own name (scn) and its declared variables are names, not forms.
        protected = set()
        for line in text.replace('\r', '').split('\n'):
            words = line.split(';', 1)[0].split()
            if len(words) >= 2 and words[0].lower() in ('scn', 'scriptname', 'short', 'int', 'long', 'float', 'ref',
                                                        'reference', 'array_var', 'string_var'):
                protected.update(w.lower().rstrip(',') for w in words[1:])
        for k, part in enumerate(re.split(r'("[^"]*")', text)):
            if k % 2:  # quoted string
                out.append(part)
                continue

            def repl(m):
                token = m.group(0)
                if token.lower() in self.KEYWORDS or token.lower() in commands or token.lower() in protected or \
                        FORM_ID.match(token) and len(token) == 8:
                    return token
                if token not in subs:
                    found = self.editor_id(token)
                    if not found:
                        return token
                    subs[token] = found['formId']
                return subs[token]
            out.append(re.sub(r'(?<![\w.])[A-Za-z_][A-Za-z0-9_]{2,127}(?![\w.])', repl, part))
        return ''.join(out), subs

    # ================================================================= milestone 6: memory and engine
    def _layout(self, cls):
        if not cls:
            return None
        try:
            return self.engine().class_layout(cls)[1]
        except fnv_engine.EngineMapError:
            return None

    def _fields(self, cls, base_offset=0, depth=0):
        """All fields of a class including base classes, with absolute offsets (bases laid out in order)."""
        layout = self._layout(cls)
        if not layout or depth > 8:
            return []
        out, offset = [], base_offset
        for base in layout.get('bases') or []:
            out += self._fields(base, offset, depth + 1)
            size = (self._layout(base) or {}).get('size') or 0
            offset += size
        for f in layout.get('fields') or []:
            if f.get('offset') is not None:
                out.append({**f, 'absolute': base_offset + f['offset'], 'class': cls})
        return out

    def locate(self, target, path=None, cls=None):
        """target (address | ref spec) + dotted field path -> (address, type, class chain)."""
        steps = []
        if isinstance(target, str) and re.match(r'^0x[0-9A-Fa-f]{1,8}$', target):
            address = int(target, 16)
        else:
            address = self.resolve(target)['ptr']
        if cls:
            current_class = cls
        else:
            current_class = self.rtti([address])[address]['class'] if address else None
        ftype = current_class
        parts = [p for p in (path or '').split('.') if p]
        for position, part in enumerate(parts):
            if not current_class:
                raise LiveError('NO_LAYOUT', 'No class known at 0x%08X to look up %r' % (address, part))
            fields = self._fields(current_class)
            match = next((f for f in fields if f['name'] == part), None) or \
                next((f for f in fields if f['name'].lower() == part.lower()), None)
            if not match:
                raise LiveError('NO_FIELD', '%s has no field %r' % (current_class, part),
                                {'fields': [f['name'] for f in fields][:200]})
            steps.append({'field': match['name'], 'class': match['class'], 'offset': '0x%X' % match['absolute'],
                          'type': match['type'], 'checked': match.get('checked'), 'span': match.get('span')})
            field_address = address + match['absolute']
            ftype = match['type'].strip()
            if ftype.endswith('*') and position < len(parts) - 1:
                pointer = self.u32s([field_address])[0]
                if not pointer:
                    raise LiveError('NULL_POINTER', '%s is null' % match['name'], {'steps': steps})
                address = pointer
                pointee = ftype[:-1].strip()
                runtime = self.rtti([pointer])[pointer]['class']
                current_class = runtime if runtime and self._layout(runtime) else pointee
            else:
                address = field_address
                current_class = ftype if self._layout(ftype) else None
        return address, ftype, steps

    def mem_read(self, target, path=None, as_type=None, length=None, cls=None):
        self.ready()
        address, ftype, steps = self.locate(target, path, cls)
        kind = as_type or ftype
        span = steps[-1].get('span') if steps else None
        if not as_type and kind not in SIMPLE_TYPES and not (kind or '').endswith('*') and not self._layout(kind) \
                and span in (1, 2, 4):
            kind = {1: 'u8', 2: 'u16', 4: 'u32'}[span]   # enum or typedef: an integer of the field's size
        out = {'address': '0x%08X' % address, 'type': kind, 'declaredType': ftype, 'path': steps}
        if kind in SIMPLE_TYPES or (kind and kind.endswith('*')):
            fmt, size = SIMPLE_TYPES.get(kind, ('<I', 4))
            raw = self.link.read(address, size)
            value = struct.unpack(fmt, raw)[0]
            out.update(value=('0x%08X' % value) if kind.endswith('*') or kind == 'ptr' else value, hex=raw.hex())
            if kind.endswith('*') and value:
                out['pointee'] = self.rtti([value])[value]
        elif kind in ('cstr', 'char*'):
            text = self.link.one('rs', word(address), '1000')
            out.update(value=text.get('text'))
        else:
            size = length or (self._layout(kind) or {}).get('size') or 64
            raw = self.link.read(address, min(int(size), 4096))
            out.update(hex=raw.hex(), size=len(raw))
            fields = self._fields(kind) if self._layout(kind) else []
            decoded = {}
            for f in fields:
                t = f['type'].strip()
                spec = SIMPLE_TYPES.get(t) or (('<I', 4) if t.endswith('*') else None)
                if spec and f['absolute'] + spec[1] <= len(raw):
                    v = struct.unpack(spec[0], raw[f['absolute']:f['absolute'] + spec[1]])[0]
                    decoded[f['name']] = ('0x%08X' % v) if t.endswith('*') else v
            if decoded:
                out['fields'] = decoded
        out['uses'] = ['engine map class layouts (decomp headers)', 'MSVC RTTI for runtime classes']
        return out

    def _encode(self, kind, value):
        if kind in SIMPLE_TYPES or kind.endswith('*'):
            fmt, _ = SIMPLE_TYPES.get(kind, ('<I', 4))
            if fmt in ('<f', '<d'):
                return struct.pack(fmt, float(value))
            if fmt == '<?':
                return struct.pack(fmt, bool(value))
            number = int(value, 0) if isinstance(value, str) else int(value)
            return struct.pack(fmt, number)
        if kind == 'bytes':
            return bytes.fromhex(str(value))
        raise ValueError('Cannot write a value of type %r (give as_type: u8/u16/u32/i32/f32/f64/ptr/bytes)' % kind)

    def mem_write(self, target, value, path=None, as_type=None, cls=None, note=None):
        self.ready()
        address, ftype, steps = self.locate(target, path, cls)
        kind = as_type or ftype
        span = steps[-1].get('span') if steps else None
        if not as_type and kind not in SIMPLE_TYPES and not (kind or '').endswith('*') and not self._layout(kind) \
                and span in (1, 2, 4):
            kind = {1: 'u8', 2: 'u16', 4: 'u32'}[span]
        data = self._encode(kind, value)
        self.changing('memory write')
        old = self.link.write(address, data)
        entry = self.journal.add({'kind': 'memory', 'address': '0x%08X' % address, 'type': kind, 'old': old.hex(),
                                  'new': data.hex(), 'target': target, 'path': path, 'note': note,
                                  'undo': {'op': 'write', 'address': '0x%08X' % address, 'hex': old.hex()},
                                  'pid': (self.link.hello or {}).get('pid')})
        return {'address': '0x%08X' % address, 'type': kind, 'old': old.hex(), 'new': data.hex(), 'path': steps,
                'journalId': entry['id'], 'changed': old != data}

    def form_edit(self, form, field, value, as_type=None):
        result = self.mem_write(form, value, path=field, as_type=as_type, note='form edit')
        return {**result, 'form': form, 'field': field}

    def _signature(self, function):
        engine = self.engine()
        address = engine.resolve(function)
        record = engine.summary(address)
        fn = engine.functions.get(address, {})
        return address, record, fn.get('signature') or '', fn.get('proof')

    def engine_call(self, function, this=None, args=None, returns='int', convention=None, allow_unproven=False):
        self.ready()
        if re.match(r'^0x[0-9A-Fa-f]{6,8}$', function or ''):
            address = int(function, 16)
            record, signature, proof = {'address': function, 'status': 'raw address'}, '', None
            try:
                address, record, signature, proof = self._signature(function)
            except Exception:
                pass
        else:
            address, record, signature, proof = self._signature(function)
        status = record.get('status')
        if status not in ('proven', 'class_proven') and not allow_unproven:
            raise LiveError('UNPROVEN_FUNCTION', '%s is %s, not proven: pass allow_unproven=true to call it anyway'
                            % (record.get('name', function), status), {'function': record})
        conv = convention or next((c for c in ('thiscall', 'fastcall', 'stdcall', 'cdecl') if '__' + c in signature), None) \
            or ('thiscall' if '::' in (record.get('name') or '') and this is not None else 'cdecl')
        words = []
        this_ptr = 0
        if this is not None:
            this_ptr = int(this, 16) if isinstance(this, str) and this.lower().startswith('0x') else self.resolve(str(this))['ptr']
        for arg in args or []:
            kind, value = (arg.get('type', 'int'), arg.get('value')) if isinstance(arg, dict) else ('int', arg)
            if kind in ('int', 'uint', 'bool'):
                words.append(int(value, 0) if isinstance(value, str) else int(value))
            elif kind == 'ptr':
                words.append(int(str(value), 16) if str(value).lower().startswith('0x') else int(value))
            elif kind in ('ref', 'form'):
                words.append(self.resolve(str(value))['ptr'])
            elif kind == 'float':
                words.append(struct.unpack('<I', struct.pack('<f', float(value)))[0])
            elif kind == 'double':
                lo, hi = struct.unpack('<II', struct.pack('<d', float(value)))
                words += [lo, hi]
            elif kind == 'string':
                words.append(self._game_string(str(value)))
            else:
                raise ValueError('Argument type %r: use int, bool, ptr, ref, form, float, double or string' % kind)
        ecx = edx = 0
        if conv == 'thiscall':
            ecx = this_ptr
        elif conv == 'fastcall':
            regs = ([this_ptr] if this is not None else []) + words
            ecx, edx = (regs + [0, 0])[:2]
            words = regs[2:]
        elif this is not None:
            words = [this_ptr] + words
        self.changing('engine call %s' % (record.get('name') or function))
        ret = {'float': 'f', 'double': 'f', 'void': 'v'}.get(returns, 'i')
        result = self.link.one('call', word(address), ret, word(ecx), word(edx), *[word(w) for w in words])
        if not result.get('ok'):
            raise LiveError('CALL_REJECTED', 'The companion refused the call: %s' % result.get('error'), result)
        eax = int(result['eax'], 16)
        value = {'int': struct.unpack('<i', struct.pack('<I', eax))[0], 'uint': eax, 'bool': bool(eax & 0xFF),
                 'ptr': '0x%08X' % eax, 'float': result.get('st0'), 'double': result.get('st0'), 'void': None}.get(returns, eax)
        if returns in ('form', 'ref'):
            value = self.form_ids([eax])[0]
        entry = self.journal.add({'kind': 'engine_call', 'function': record.get('name') or function,
                                  'address': '0x%08X' % address, 'convention': conv, 'words': ['0x%X' % w for w in words],
                                  'this': '0x%08X' % this_ptr if this is not None else None, 'undo': None})
        return {'function': record, 'convention': conv, 'signature': signature or None, 'proof': proof,
                'result': value, 'eax': result['eax'], 'st0': result.get('st0'), 'scriptErrors': result.get('scriptErrors'),
                'journalId': entry['id']}

    def _game_string(self, text):
        """Copy a string into game memory with FormHeap_Allocate (0x00401000, proven) and return its address."""
        data = text.encode('cp1252') + b'\0'
        call = self.link.one('call', '401000', 'i', '0', '0', word(len(data)))
        address = int(call['eax'], 16) if call.get('ok') else 0
        if not address:
            raise LiveError('ALLOC_FAILED', 'FormHeap_Allocate returned null')
        self.link.write(address, data)
        return address

    def setting_set(self, name, value):
        self.ready()
        is_ini = ':' in name
        if not re.match(r'^[A-Za-z][A-Za-z0-9_ ]{1,80}(:[A-Za-z0-9_ ]{1,40})?$', name):
            raise ValueError('Setting names look like fCombatDistance or bAlwaysActive:General')
        string = name[0] == 's'
        if string:
            if is_ini:
                raise ValueError('String INI settings are not supported; numeric INI settings and game settings are')
            get, put = 'GetStringGameSetting "%s"' % name, 'SetGS %s "%s"' % (name, str(value).replace('"', ''))
        elif is_ini:
            get, put = 'GetNumericIniSetting "%s"' % name, 'SetNumericIniSetting "%s" %s' % (name, float(value))
        else:
            get, put = 'GetNumericGameSetting "%s"' % name, 'SetNumericGameSetting "%s" %s' % (name, float(value))
        old = fnv_link.value_of(self.link.eval(get))
        if old is None:
            raise LiveError('NO_SETTING', 'The running game has no setting %r' % name)
        self.changing('setting ' + name)
        result = self.console(put)
        new = fnv_link.value_of(self.link.eval(get))
        undo_value = old if string else repr(float(old))
        undo = put.replace(str(value) if string else str(float(value)), str(undo_value)) if not string else \
            'SetGS %s "%s"' % (name, old)
        entry = self.journal.add({'kind': 'setting', 'name': name, 'old': old, 'new': new, 'undo': {'op': 'console', 'line': undo}})
        info = None
        try:
            info = self.engine().statics_find(name.split(':')[0], 'Setting', 1)
        except Exception:
            pass
        return {'name': name, 'old': old, 'new': new, 'accepted': result['accepted'], 'journalId': entry['id'],
                'engineMap': info, 'changed': old != new}

    def script_set(self, target, kind, value, name=None, index=None):
        """kind: stage | objective_completed | objective_displayed | variable | global."""
        self.in_game()
        if kind == 'global':
            if not EDITOR_ID.match(target):
                raise ValueError('A global is given by its EditorID')
            old = fnv_link.value_of(self.link.eval('GetGlobalValue %s' % target))
            line, undo = 'set %s to %s' % (target, value), 'set %s to %s' % (target, old)
        elif kind == 'stage':
            q = self.resolve(target)
            old = fnv_link.value_of(self.link.eval('GetStage %s' % q['expr']))
            line, undo = 'SetStage %s %d' % (q['formId'], int(value)), None
        elif kind in ('objective_completed', 'objective_displayed'):
            q = self.resolve(target)
            fn = 'Completed' if kind == 'objective_completed' else 'Displayed'
            old = fnv_link.value_of(self.link.eval('GetObjective%s %s %d' % (fn, q['expr'], int(index))))
            line = 'SetObjective%s %s %d %d' % (fn, q['formId'], int(index), 1 if value else 0)
            undo = 'SetObjective%s %s %d %d' % (fn, q['formId'], int(index), 1 if old else 0)
        elif kind == 'variable':
            if not name or not EDITOR_ID.match(name):
                raise ValueError('variable needs the variable name')
            r = self.resolve(target)
            current = {v['name'].lower(): v for v in self.variables(target)['variables'] if v['name']}
            if name.lower() not in current:
                raise LiveError('NO_VARIABLE', '%s has no variable %s' % (target, name), {'variables': sorted(current)})
            old = current[name.lower()]['value']
            if not EDITOR_ID.match(target):
                raise LiveError('NEEDS_EDITOR_ID', 'Setting a variable needs the quest or reference EditorID (console '
                                '"set X.var to")', {'formId': r['formId']})
            line, undo = 'set %s.%s to %s' % (target, name, value), 'set %s.%s to %s' % (target, name, old)
        else:
            raise ValueError('kind must be stage, objective_completed, objective_displayed, variable or global')
        self.changing('script_set')
        result = self.console(line)
        entry = self.journal.add({'kind': 'script_set', 'what': kind, 'target': target, 'name': name, 'index': index,
                                  'old': old, 'new': value, 'line': line,
                                  'undo': {'op': 'console', 'line': undo} if undo else None})
        return {'line': line, 'accepted': result['accepted'], 'output': result['output'], 'old': old,
                'undoable': bool(undo), 'journalId': entry['id']}

    ACTIONS = {'evaluate_package': 'EvaluatePackage', 'stop_combat': 'StopCombat', 'kill': 'Kill',
               'resurrect': 'Resurrect 1', 'enable': 'Enable', 'disable': 'Disable', 'remove_package': 'RemoveScriptPackage',
               'unconscious_on': 'SetUnconscious 1', 'unconscious_off': 'SetUnconscious 0', 'restrain_on': 'SetRestrained 1',
               'restrain_off': 'SetRestrained 0'}
    TARGET_ACTIONS = {'start_combat': 'StartCombat %s', 'move_to': 'MoveTo %s', 'add_package': 'AddScriptPackage %s',
                      'play_idle': 'PlayIdle %s', 'cast': 'Cast %s player', 'equip': 'EquipItem %s', 'unequip': 'UnequipItem %s',
                      'add_item': 'AddItem %s 1', 'remove_item': 'RemoveItem %s 1', 'look_at': 'Look %s', 'say_to': 'SayTo %s'}

    def actor_control(self, ref, action, target=None, value=None):
        self.in_game()
        r = self.resolve(ref)
        undo = None
        if action in self.ACTIONS:
            command = self.ACTIONS[action]
            undo = {'enable': 'Disable', 'disable': 'Enable', 'unconscious_on': 'SetUnconscious 0',
                    'restrain_on': 'SetRestrained 0'}.get(action)
        elif action in self.TARGET_ACTIONS:
            if not target:
                raise ValueError('%s needs a target' % action)
            t = target if EDITOR_ID.match(target) and not FORM_ID.match(target) else self.resolve(target)['formId']
            command = self.TARGET_ACTIONS[action] % t
            if action == 'add_item':
                command = 'AddItem %s %d' % (t, int(value or 1))
                undo = 'RemoveItem %s %d' % (t, int(value or 1))
            elif action == 'remove_item':
                command = 'RemoveItem %s %d' % (t, int(value or 1))
        elif action in ('set_av', 'force_av'):
            # SetAV changes the base value; for NPC skills the engine derives the value from SPECIAL, so only
            # ForceAV moves them. Both are read back, and the result says whether the value changed.
            if not target or target not in fnv_facts.ACTOR_VALUES:
                raise ValueError('%s needs target = actor value name' % action)
            before = self.values(['GetAV %s' % target, 'GetBaseAV %s' % target], r['ptr'])
            verb = 'SetAV' if action == 'set_av' else 'ForceAV'
            command = '%s %s %s' % (verb, target, _num(value))
            undo = '%s %s %s' % (verb, target, _num(before[1] if action == 'set_av' else before[0]))
            self.changing('actor_control')
            result = self.console(command, r['ptr'])
            after = self.values(['GetAV %s' % target, 'GetBaseAV %s' % target], r['ptr'])
            entry = self.journal.add({'kind': 'actor', 'ref': r['formId'], 'action': action, 'command': command,
                                      'old': before, 'new': after, 'undo': {'op': 'console_ref', 'ref': r['formId'], 'lines': [undo]}})
            out = {'action': action, 'command': command, 'accepted': result['accepted'], 'output': result['output'],
                   'before': {'current': before[0], 'base': before[1]}, 'after': {'current': after[0], 'base': after[1]},
                   'effective': after != before, 'undoable': True, 'journalId': entry['id']}
            if action == 'set_av' and after[0] == before[0]:
                out['hint'] = 'The current value did not change (NPC skills follow SPECIAL); use force_av'
            return out
        elif action == 'set_position':
            x, y, z = value
            old = self.values(['GetPos X', 'GetPos Y', 'GetPos Z'], r['ptr'])
            for axis, v in zip('XYZ', (x, y, z)):
                self.console('SetPos %s %s' % (axis, float(v)), r['ptr'])
            entry = self.journal.add({'kind': 'actor', 'ref': r['formId'], 'action': action, 'old': old, 'new': value,
                                      'undo': {'op': 'console_ref', 'ref': r['formId'],
                                               'lines': ['SetPos %s %s' % (a, o) for a, o in zip('XYZ', old)]}})
            return {'action': action, 'old': old, 'new': value, 'journalId': entry['id']}
        else:
            raise ValueError('Unknown action %r; use one of %s' % (action, ', '.join(
                sorted(list(self.ACTIONS) + list(self.TARGET_ACTIONS) + ['set_av', 'force_av', 'set_position']))))
        self.changing('actor_control')
        result = self.console(command, r['ptr'])
        entry = self.journal.add({'kind': 'actor', 'ref': r['formId'], 'action': action, 'command': command,
                                  'undo': {'op': 'console_ref', 'ref': r['formId'], 'lines': [undo]} if undo else None})
        return {'action': action, 'command': command, 'accepted': result['accepted'], 'output': result['output'],
                'undoable': bool(undo), 'journalId': entry['id']}

    def input(self, action, control=None, key=None, duration_ms=0, target=None):
        """Bounded player input: tap/hold a control or DirectInput key (released by the companion even if the
        bridge goes away), aim at a ref, or activate a ref as the player."""
        self.in_game()
        if action == 'aim_at':
            t = self.resolve(target)
            heading = fnv_link.value_of(self.link.eval('player.GetHeadingAngle %s' % t['expr']))
            angle = fnv_link.value_of(self.link.eval('player.GetAngle Z'))
            if heading is None or angle is None:
                raise LiveError('AIM_FAILED', 'Could not compute the heading to %s' % target)
            new = (angle + heading) % 360
            self.changing('input')
            self.console('player.SetAngle Z %.3f' % new)
            entry = self.journal.add({'kind': 'input', 'action': action, 'target': t['formId'], 'old': angle, 'new': new,
                                      'undo': {'op': 'console', 'line': 'player.SetAngle Z %.3f' % angle}})
            return {'action': action, 'heading': new, 'turnedBy': heading, 'journalId': entry['id']}
        if action == 'activate':
            t = self.resolve(target)
            self.changing('input')
            result = self.console('Activate player', t['ptr'])
            self.journal.add({'kind': 'input', 'action': action, 'target': t['formId'], 'undo': None})
            return {'action': action, 'target': t['formId'], 'accepted': result['accepted'], 'output': result['output']}
        if action not in ('tap', 'hold'):
            raise ValueError('action must be tap, hold, aim_at or activate')
        if control is not None:
            code = fnv_facts.CONTROLS.get(str(control).lower(), control)
            scan, mouse = self.values(['GetControl %d' % int(code), 'GetAltControl %d' % int(code)])
            if scan is None or scan in (255, -1):
                # Bound to a mouse button only (attack, aim): xNVSE key commands take 256 + button.
                if mouse is None or mouse in (255, -1):
                    raise LiveError('UNBOUND_CONTROL', 'Control %s has no key or mouse button bound' % control)
                scan = 256 + int(mouse)
            scan = int(scan)
        elif key is not None:
            scan = int(key)
        else:
            raise ValueError('Give control (name or code) or key (DirectInput scan code)')
        duration_ms = int(duration_ms or 0)
        if action == 'hold' and not 0 < duration_ms <= 10000:
            raise ValueError('hold needs 1..10000 ms')
        self.changing('input')
        if action == 'tap':
            result = self.link.batch([('con', hexs('TapKey %d' % scan), '0')])[0]
        else:
            result = self.link.batch([('con', hexs('HoldKey %d' % scan), '0'),
                                      ('sched', word(duration_ms), hexs('ReleaseKey %d' % scan))])[0]
        self.journal.add({'kind': 'input', 'action': action, 'scan': scan, 'durationMs': duration_ms, 'undo': None})
        return {'action': action, 'scanCode': scan, 'durationMs': duration_ms, 'accepted': result.get('accepted'),
                'releaseScheduled': action == 'hold'}

    # ================================================================= journal and undo
    def journal_list(self, limit=100, include_undone=True):
        entries = self.journal.entries()
        if not include_undone:
            entries = [e for e in entries if not e['undone']]
        return {'count': len(entries), 'entries': entries[-limit:], 'path': str(self.journal.path)}

    def _apply_undo(self, entry):
        undo = entry.get('undo')
        if not undo:
            return {'id': entry['id'], 'undone': False, 'reason': 'not undoable (%s)' % entry['kind']}
        pid = (self.link.hello or self.link.ping()).get('pid')
        if entry.get('pid') and entry['pid'] != pid:
            return {'id': entry['id'], 'undone': False, 'reason': 'made in another game process (%s)' % entry['pid']}
        if undo['op'] == 'write':
            self.link.write(int(undo['address'], 16), bytes.fromhex(undo['hex']))
        elif undo['op'] == 'console':
            self.console(undo['line'])
        elif undo['op'] == 'swap_script':
            from .fnv_data import swap_script_code
            swap_script_code(self, int(undo['target'], 16), int(undo['holder'], 16))
        elif undo['op'] == 'console_ref':
            try:
                ref = self.resolve(undo['ref'])
            except LiveError as e:
                if e.code != 'REF_NOT_FOUND':
                    raise
                # A load or a cell reset removed the ref: there is nothing left to undo.
                return {'id': entry['id'], 'undone': True, 'kind': entry['kind'],
                        'note': 'the ref %s no longer exists' % undo['ref']}
            for line in undo['lines']:
                self.console(line, ref['ptr'])
        return {'id': entry['id'], 'undone': True, 'kind': entry['kind']}

    def undo(self, count=1, journal_id=None):
        self.ready()
        entries = [e for e in self.journal.entries() if not e['undone']]
        if journal_id:
            entries = [e for e in entries if e['id'] == journal_id]
            if not entries:
                raise LiveError('NOT_FOUND', 'No undoable journal entry %s' % journal_id)
        else:
            entries = [e for e in entries if e.get('undo')][-int(count):]
        results = [self._apply_undo(e) for e in reversed(entries)]
        self.journal.mark_undone([r['id'] for r in results if r['undone']])
        return {'results': results, 'changed': any(r['undone'] for r in results)}

    def revert_all(self, since=None):
        """Undo every undoable change of this game process (since: only entries after that journal position)."""
        self.ready()
        pid = (self.link.hello or self.link.ping()).get('pid')
        entries = [e for k, e in enumerate(self.journal.entries()) if not e['undone'] and e.get('undo') and
                   (not e.get('pid') or e['pid'] == pid) and (since is None or k >= since)]
        results = [self._apply_undo(e) for e in reversed(entries)]
        self.journal.mark_undone([r['id'] for r in results if r['undone']])
        skipped = [e['id'] for e in self.journal.entries() if not e['undone'] and not e.get('undo')]
        return {'reverted': sum(r['undone'] for r in results), 'results': results, 'notUndoable': skipped,
                'changed': any(r['undone'] for r in results)}
