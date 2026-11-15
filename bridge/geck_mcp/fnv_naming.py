"""Naming functions from the running game: probe harvests, call-site resolution and the function dossier
(NAMING-PLAN.md components C and D), shared by the oracle, experiments and the fnvsym export.

Everything runtime is kept in state/atlas.sqlite next to the atlas runs:
  harvests  one row per harvest of the armed probe set (run id, time, counts)
  samples   decoded probe samples: this (and its class at sample time), edx, caller, the first six stack
            words, argument classes, the result (int or st0 float), thread, frame, action tag, and the form
            id / form type of `this` when it was still the same live object at harvest time
  callers   return address -> calls, per probed function (the companion's caller tables)
"""
import csv
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from .fnv_probe import CALLER_SLOTS, WORDS, ProbeError, call_kind, decode_sample, vtable_class



class ProofFolderError(Exception):
    code = 'NO_PROOF_FOLDER'


def fnvsym_root(required=True):
    """The fnvsym checkout (names, proofs, evidence, traces) from FNVSYM_ROOT. There is no default: the naming tools
    are an optional extra (decision D1). required=False answers None when it is not set."""
    root = os.environ.get('FNVSYM_ROOT')
    if root:
        return Path(root)
    if required:
        raise ProofFolderError('The fnv_name_* tools need a fnvsym checkout: set the FNVSYM_ROOT environment variable '
                               'to its folder (in the MCP server registration, "env": {"FNVSYM_ROOT": "C:/path/to/'
                               'fnvsym"}) and restart the server.')
    return None
RUNTIME_SCHEMA = '''
    CREATE TABLE IF NOT EXISTS harvests (id INTEGER PRIMARY KEY, run INTEGER, at REAL, probes INTEGER,
        functions INTEGER, samples INTEGER, callers INTEGER, notes TEXT);
    CREATE TABLE IF NOT EXISTS samples (harvest INTEGER, address INTEGER, this INTEGER, vt INTEGER, cls TEXT,
        edx INTEGER, caller INTEGER, stack TEXT, arg0_cls TEXT, arg1_cls TEXT, state INTEGER, eax INTEGER,
        f REAL, ticks INTEGER, thread INTEGER, main INTEGER, frame INTEGER, action INTEGER, form_id TEXT,
        form_type INTEGER, ret_cls TEXT);
    CREATE INDEX IF NOT EXISTS samples_address ON samples(address);
    CREATE TABLE IF NOT EXISTS harvest_callers (harvest INTEGER, address INTEGER, return_address INTEGER,
        calls INTEGER);
    CREATE INDEX IF NOT EXISTS harvest_callers_address ON harvest_callers(address);
    CREATE TABLE IF NOT EXISTS harvest_counts (harvest INTEGER, address INTEGER, calls INTEGER, sampled INTEGER,
        overflow INTEGER);
    CREATE INDEX IF NOT EXISTS harvest_counts_address ON harvest_counts(address);
'''
FORM_ROOTS = ('TESForm',)


def hx(a):
    return '0x%08X' % a


# ===================================================================================== class hierarchy
def ancestors(engine, cls):
    """cls and every base class (MSVC RTTI), most derived first."""
    info = (engine.rtti or {}).get(cls) or {}
    return [cls] + list(info.get('bases') or [])


def is_a(engine, cls, base):
    return bool(cls) and base in ancestors(engine, cls)


def common_ancestor(engine, classes):
    """The most derived class every observed class is (or derives from); None if they share nothing."""
    classes = [c for c in classes if c]
    if not classes:
        return None
    common = set(ancestors(engine, classes[0]))
    for c in classes[1:]:
        common &= set(ancestors(engine, c))
    if not common:
        return None
    # the deepest: the one with the most bases of its own
    return max(common, key=lambda c: (len(ancestors(engine, c)), c))


# ===================================================================================== call-site resolution
def resolve_caller(engine, image, callee, ret, calls, this_vtables=()):
    """One caller-table entry resolved: the caller function, the call instruction and, for a call through
    [reg+disp], the vtable class and slot when a sampled `this` vtable holds the callee at disp/4."""
    out = {'returnAddress': hx(ret), 'count': calls}
    hit = engine.containing(ret - 1)
    if hit:
        out['function'] = engine.summary(hit[0], with_location=False)
    k = call_kind(image, ret) or {'kind': 'unknown'}
    out['call'] = dict(k)
    if k.get('kind') == 'register' and image is not None:
        # MSVC's usual virtual call: mov edx,[eax] / mov edx,[edx+disp] / call edx
        src = register_source(image, int(k['site'], 16), k.get('operand', ''))
        if src is not None:
            out['call']['loadedFrom'] = src['operand']
            if src.get('slotIndex') is not None:
                trial = dict(k, kind='virtual', slotIndex=src['slotIndex'])
                owners = _slot_owners(engine, callee, src['slotIndex'], this_vtables)
                if owners:
                    out['call'] = dict(trial, resolved='vtable slot (through %s)' % k.get('operand'), slotOf=owners,
                                       declaredBy=slot_introducer(engine, owners, src['slotIndex'], this_vtables),
                                       loadedFrom=src['operand'])
                    return out
    if k.get('kind') == 'virtual':
        slot = k.get('slotIndex')
        owners = []
        for vt in sorted(set(this_vtables)):
            entry = engine.vtables.get(vt)
            if not entry:
                continue
            cls, info = entry
            slots = info.get('slots') or []
            if slot is not None and slot < len(slots) and int(slots[slot], 16) == callee:
                owners.append(cls)
        if owners:
            out['call']['resolved'] = 'vtable slot'
            out['call']['slotOf'] = sorted(set(owners))
            out['call']['declaredBy'] = slot_introducer(engine, owners, slot, this_vtables)
        else:
            out['call']['resolved'] = 'unconfirmed (no sampled vtable holds the callee at this slot)'
    elif k.get('kind') == 'direct':
        out['call']['resolved'] = 'direct' if int(k['target'], 16) == callee else 'direct to a thunk/other target'
    elif k.get('kind') in ('register', 'pointer'):
        out['call']['resolved'] = 'callback (function pointer)'
    return out


def _slot_owners(engine, callee, slot, this_vtables):
    owners = []
    for vt in sorted(set(this_vtables)):
        entry = engine.vtables.get(vt)
        if entry:
            slots = entry[1].get('slots') or []
            if slot < len(slots) and int(slots[slot], 16) == callee:
                owners.append(entry[0])
    return sorted(set(owners))


VTABLE_LOAD = re.compile(r'dword ptr \[(eax|ebx|ecx|edx|esi|edi)(?: \+ (0x[0-9a-f]+|\d+))?\]$')


def register_source(image, site, reg, window=40):
    """The instruction before `site` that last wrote `reg` (linear decode of the bytes before the call, from
    the start offset whose instructions end exactly at the call). For `mov reg, dword ptr [r + disp]` with a
    non-negative, 4-aligned disp, slotIndex = disp/4."""
    from .fnv_probe import disassembler
    code = image.read(site - window, window)
    if not code or reg not in ('eax', 'ebx', 'ecx', 'edx', 'esi', 'edi'):
        return None
    md = disassembler()
    best = None
    for start in range(window):
        insns = list(md.disasm_lite(code[start:], site - window + start))
        if insns and insns[-1][0] + insns[-1][1] == site and (best is None or len(insns) > len(best)):
            best = insns
    for at, _size, mnemonic, op_str in reversed(best or []):
        dest = op_str.split(',')[0].strip()
        if dest != reg and not (mnemonic in ('pop',) and op_str.strip() == reg):
            continue
        if mnemonic != 'mov':
            return {'operand': '%s %s' % (mnemonic, op_str), 'at': hx(at)}
        src = op_str.split(',', 1)[1].strip()
        m = VTABLE_LOAD.fullmatch(src)
        out = {'operand': 'mov %s' % op_str, 'at': hx(at)}
        if m:
            disp = int(m.group(2), 0) if m.group(2) else 0
            if disp % 4 == 0:
                out['slotIndex'] = disp // 4
        return out
    return None


def primary_chain(engine, cls):
    """cls, its first direct base, that one's first direct base, ...: the classes that share the object's start
    (and so its primary vtable). Other bases are separate subobjects with vtables of their own."""
    out, seen = [], set()
    while cls and cls not in seen:
        seen.add(cls)
        out.append(cls)
        bases = ((engine.rtti or {}).get(cls) or {}).get('directBases') or []
        cls = bases[0] if bases else None
    return out


def slot_introducer(engine, owners, slot, this_vtables=()):
    """The class that introduces the slot. For the primary vtable (this offset 0): the most-base ancestor whose
    primary vtable already has the slot. For a secondary vtable (a base subobject at this+N, e.g. ActorValueOwner
    inside an actor base): the base class whose primary vtable has the same number of slots, the most-base one; or
    'secondary vtable at +N' when no ancestor fits that shape (no guess)."""
    offset, length = 0, None
    for vt in this_vtables:
        entry = engine.vtables.get(vt)
        if entry and entry[0] in owners:
            offset, length = entry[1].get('thisOffset', 0), len(entry[1].get('slots') or [])
            break
    chain = ancestors(engine, owners[0])
    if offset == 0:
        best = None
        for cls in primary_chain(engine, owners[0]):
            info = (engine.rtti or {}).get(cls) or {}
            primary = next((vt for vt in info.get('vtables') or [] if vt.get('thisOffset') == 0), None)
            if primary and slot < len(primary.get('slots') or []):
                best = cls
        return best
    fits = []
    for cls in chain[1:]:
        info = (engine.rtti or {}).get(cls) or {}
        primary = next((vt for vt in info.get('vtables') or [] if vt.get('thisOffset') == 0), None)
        if primary and len(primary.get('slots') or []) == length:
            fits.append(cls)
    if not fits:
        return 'secondary vtable at +0x%X' % offset
    # the most-base of the fitting classes: one that no other fitting class derives from... i.e. that is an
    # ancestor of the others
    for cls in fits:
        if all(cls in ancestors(engine, other) for other in fits):
            return '%s (subobject at +0x%X)' % (cls, offset)
    return 'secondary vtable at +0x%X (%s)' % (offset, ' or '.join(fits[:3]))


# ===================================================================================== static facts
_SCHEMA_DONE = set()

class StaticIndex:
    """Indexes built once per engine map: vtable slots per function, fnvsym claims per address."""

    def __init__(self, engine):
        self.engine = engine
        self._slots = None
        self._claims = None
        self._names = None
        self._verified = None
        self._statics = None

    def slots(self):
        if self._slots is None:
            out = defaultdict(list)
            for cls, info in (self.engine.rtti or {}).items():
                for vt in info.get('vtables') or []:
                    for k, s in enumerate(vt.get('slots') or []):
                        out[int(s, 16)].append({'class': cls, 'vtable': vt['address'], 'thisOffset': vt.get('thisOffset', 0),
                                                'slot': k})
            self._slots = out
        return self._slots

    def claims(self, address):
        if self._claims is None:
            claims = defaultdict(list)  # filled privately, then published whole (parallel calls)
            folder = fnvsym_root() / 'evidence'
            for path in sorted(folder.glob('*.jsonl')) if folder.exists() else []:
                with open(path, encoding='utf-8') as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(d.get('addr'), int):
                            claims[d['addr']].append({k: d.get(k) for k in ('pass', 'kind', 'value', 'conf', 'why')})
            self._claims = claims
        return self._claims.get(address, [])

    def names_row(self, address):
        if self._names is None:
            self._names = self._csv(fnvsym_root() / 'names.csv')
        return self._names.get(address)

    def verified_row(self, address):
        if self._verified is None:
            self._verified = self._csv(fnvsym_root() / 'proofs' / 'verified.csv')
        return self._verified.get(address)

    @staticmethod
    def _csv(path):
        out = {}
        if path.exists():
            with open(path, encoding='utf-8') as f:
                for r in csv.DictReader(f):
                    try:
                        out[int(r['addr'], 16)] = r
                    except (KeyError, ValueError):
                        pass
        return out

    def statics(self):
        if self._statics is None:
            statics = {}
            for s in self.engine.statics:
                for key in (s.get('name'), s.get('setting')):
                    if key and len(key) > 3:
                        statics[key] = s
            self._statics = statics
        return self._statics


STRING_LITERAL = re.compile(r'"((?:[^"\\]|\\.){2,200})"')
IDENT = re.compile(r'\b[A-Za-z_][A-Za-z0-9_]{3,}\b')


def body_strings(engine, statics, address):
    """String literals and known statics/settings the decompiled body uses."""
    try:
        text = engine.code(address, max_lines=400, annotate=False)
    except Exception:  # noqa: BLE001 - no decomp checkout: static strings are optional
        return {'strings': [], 'statics': [], 'available': False}
    if isinstance(text, dict):
        text = text.get('code') or text.get('text') or ''
    strings = []
    for m in STRING_LITERAL.finditer(text or ''):
        if m.group(1) not in strings:
            strings.append(m.group(1))
    used = []
    for m in IDENT.finditer(text or ''):
        s = statics.get(m.group(0))
        if s and m.group(0) not in [u['name'] for u in used]:
            used.append({'name': m.group(0), 'kind': s.get('kind'), 'address': s.get('address')})
    return {'strings': strings[:20], 'statics': used[:20], 'available': True}


# ===================================================================================== the tool
class NamingTools:
    def __init__(self, live, probes, atlas):
        self.live = live
        self.probes = probes
        self.atlas = atlas
        self._index = None
        self._index_for = None

    def db(self):
        db = self.atlas.db()
        key = db.execute('PRAGMA database_list').fetchone()[2]
        if key not in _SCHEMA_DONE:   # once per file and process: the script takes the write lock (S2.5)
            db.executescript(RUNTIME_SCHEMA)
            _SCHEMA_DONE.add(key)
        return db

    def index(self):
        engine = self.live.engine()
        if self._index is None or self._index_for is not engine:
            self._index, self._index_for = StaticIndex(engine), engine
        return self._index

    # ------------------------------------------------------------------ harvest
    def harvest(self, notes=None, run=None):
        """Copy every probed function's count, samples and caller table out of the game into atlas.sqlite, so
        the evidence outlives the probe set. Returns the harvest id."""
        info = self.probes.info()
        if not info.get('ok') or not info.get('used'):
            raise ProbeError('NO_PROBES', 'No probe set to harvest: install one with fnv_probe install first')
        session = self.probes.session()
        engine = self.live.engine()
        main = (self.live.link.hello or self.live.link.ping()).get('mainThreadId')
        used, per, limit = info['used'], info['samplesPer'], info['limit']
        memory = self.probes._memory()
        started = time.monotonic()
        try:
            counts = memory.u32s(int(info['counts'], 16), used)
            meta = memory.u32s(int(info['meta'], 16), 2 * used)
            raw = memory.u32s(int(info['samples'], 16), WORDS * per * used)
            table = memory.u32s(int(info['callers'], 16), 2 * CALLER_SLOTS * used)
            sample_rows, caller_rows, count_rows, this_ptrs, ret_ptrs = [], [], [], {}, {}
            for k, p in enumerate(session['probes'][:used]):
                address = p[0]
                count_rows.append((address, counts[k], meta[2 * k], meta[2 * k + 1]))
                for j in range(CALLER_SLOTS):
                    ret, n = table[2 * CALLER_SLOTS * k + 2 * j], table[2 * CALLER_SLOTS * k + 2 * j + 1]
                    if ret:
                        caller_rows.append((address, ret, n))
                for j in range(min(meta[2 * k], per, limit)):
                    w = raw[WORDS * (per * k + j):WORDS * (per * k + j + 1)]
                    if not w[2] and not w[16]:
                        continue
                    sample_rows.append((address, w))
                    if w[2]:
                        this_ptrs[w[2]] = w[11]
                    if w[16] in (2, 3) and 0x00400000 <= w[14] < 0x7FFF0000 and not w[14] & 3:
                        ret_ptrs[w[14]] = None
            # the same live object still there? read its vtable, form type and id now
            live_forms = {}
            for ptr, vt in this_ptrs.items():
                cls = vtable_class(engine, vt)
                if not cls or not any(is_a(engine, cls, r) for r in FORM_ROOTS):
                    continue
                try:
                    head = memory.read(ptr, 16)
                except ProbeError:
                    continue
                if int.from_bytes(head[0:4], 'little') == vt:
                    live_forms[ptr] = ('%08X' % int.from_bytes(head[12:16], 'little'), head[4])
            ret_cls = {}
            for ptr in list(ret_ptrs)[:20000]:
                try:
                    vt = int.from_bytes(memory.read(ptr, 4), 'little')
                except ProbeError:
                    continue
                cls = vtable_class(engine, vt)
                if cls:
                    ret_cls[ptr] = cls
        finally:
            memory.close()
        db = self.db()
        try:
            if run is None:
                row = db.execute('SELECT MAX(id) FROM runs').fetchone()
                run = row[0] if row else None
            cur = db.execute('INSERT INTO harvests (run, at, probes, functions, samples, callers, notes) VALUES '
                             '(?,?,?,?,?,?,?)', (run, time.time(), used, sum(1 for c in count_rows if c[1]),
                                                 len(sample_rows), len(caller_rows), notes))
            hid = cur.lastrowid
            db.executemany('INSERT INTO harvest_counts VALUES (?,?,?,?,?)', [(hid,) + c for c in count_rows if c[1]])
            db.executemany('INSERT INTO harvest_callers VALUES (?,?,?,?)', [(hid,) + c for c in caller_rows])
            rows = []
            for address, w in sample_rows:
                d = decode_sample(w, engine, main)
                ret = d.get('returned') or {}
                form = live_forms.get(w[2])
                rows.append((hid, address, w[2], w[11], d.get('thisClass'), w[3], w[4], json.dumps(list(w[5:11])),
                             d.get('arg0Class'), d.get('arg1Class'), w[16], ret.get('eax'), ret.get('float'),
                             w[18] if w[16] in (2, 3) else None, w[1], 1 if main and w[1] == main else 0, w[17], w[0],
                             form[0] if form else None, form[1] if form else None,
                             ret_cls.get(ret.get('eax')) if ret.get('eax') is not None else None))
            db.executemany('INSERT INTO samples VALUES (%s)' % ','.join('?' * 21), rows)
            db.commit()
        finally:
            db.close()
        return {'harvest': hid, 'run': run, 'probes': used, 'calledFunctions': sum(1 for c in count_rows if c[1]),
                'samples': len(sample_rows), 'callerEntries': len(caller_rows), 'liveForms': len(live_forms),
                'pointerResults': len(ret_cls), 'seconds': round(time.monotonic() - started, 1)}

    def harvests(self):
        db = self.db()
        try:
            rows = db.execute('SELECT id, run, at, probes, functions, samples, callers, notes FROM harvests ORDER BY id')
            return [{'harvest': r[0], 'run': r[1], 'at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r[2])),
                     'probes': r[3], 'calledFunctions': r[4], 'samples': r[5], 'callerEntries': r[6], 'notes': r[7]}
                    for r in rows]
        finally:
            db.close()

    # ------------------------------------------------------------------ runtime facts for one function
    def runtime(self, address, harvest=None):
        """Everything recorded at runtime for one function, merged over harvests (or one harvest)."""
        db = self.db()
        try:
            where, args = ('address = ?', [address]) if harvest is None else ('address = ? AND harvest = ?',
                                                                              [address, harvest])
            counts = db.execute('SELECT harvest, calls, sampled, overflow FROM harvest_counts WHERE ' + where, args).fetchall()
            callers = db.execute('SELECT return_address, SUM(calls) FROM harvest_callers WHERE ' + where +
                                 ' GROUP BY return_address ORDER BY SUM(calls) DESC', args).fetchall()
            samples = db.execute('SELECT this, vt, cls, edx, caller, stack, arg0_cls, arg1_cls, state, eax, f, ticks, '
                                 'thread, main, frame, action, form_id, form_type, ret_cls, harvest FROM samples WHERE '
                                 + where, args).fetchall()
            fits = db.execute('SELECT run, action, kind, per_unit, r2, idle, acted, driver FROM fits WHERE address = ? '
                              'ORDER BY run, action', (address,)).fetchall()
            seconds = db.execute('SELECT SUM(s.seconds), SUM(c.calls) FROM counts c JOIN segments s ON s.id = c.segment '
                                 "WHERE c.address = ? AND s.action = 'idle'", (address,)).fetchone()
        finally:
            db.close()
        return {'counts': counts, 'callers': callers, 'samples': samples, 'fits': fits, 'idle': seconds}

    def _export_path(self, path):
        """Where the trace goes: a .json under fnvsym/traces or <state>/traces, checked on the resolved path (X1: any
        path was written, under a read-only annotation; review I3: not the state folder itself, which holds the engine
        map). An existing file is replaced only when it is a trace."""
        if not path:
            return fnvsym_root() / 'traces' / ('fnvmcp-%s.json' % time.strftime('%Y%m%d'))
        target = Path(path).resolve()
        fnvsym = fnvsym_root(required=False)
        roots = [(fnvsym / 'traces').resolve()] if fnvsym else []
        state = getattr(self.live.project, 'state_dir', None)
        if state:
            roots.append((Path(state) / 'traces').resolve())
        if target.suffix.lower() != '.json' or not any(target.is_relative_to(root) for root in roots):
            raise ProbeError('BAD_PATH', 'the trace goes to a .json file under %s; got %s'
                             % (' or '.join(str(r) for r in roots), path))
        if target.exists():
            try:
                old = json.loads(target.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                old = None
            if not (isinstance(old, dict) and str(old.get('format', '')).startswith('fnvsym-trace/')):
                raise ProbeError('BAD_PATH', '%s exists and is not an fnvsym trace: not replaced' % target)
        return target

    def export(self, path=None, max_callers=8):
        """Write everything recorded at runtime as fnvsym-trace/2 (a superset of fnvsym's Frida trace): per function
        the calls, the atlas fits, resolved callers, `this` classes, the result's shape, oracle matches, experiment
        verdicts and field watches, with the calibration of each method."""
        path = self._export_path(path)       # checked before any work (X1)
        engine = self.live.engine()
        image = self.live.deep._image(required=False)
        db = self.db()
        try:
            for ddl in ('CREATE TABLE IF NOT EXISTS oracle_results (at REAL, address INTEGER, mode TEXT, objects INTEGER, '
                        'oracle TEXT, meets INTEGER, result TEXT, calibration INTEGER)',
                        'CREATE TABLE IF NOT EXISTS experiments (at REAL, address INTEGER, verdict TEXT, hypothesis TEXT, '
                        'result TEXT)',
                        'CREATE TABLE IF NOT EXISTS watches (at REAL, address INTEGER, function INTEGER, access TEXT, '
                        'hits INTEGER, field TEXT, result TEXT)'):
                db.execute(ddl)
            runs = [dict(zip(('id', 'started', 'source', 'probes', 'notes'), r))
                    for r in db.execute('SELECT id, started, source, probes, notes FROM runs')]
            harvests = [dict(zip(('id', 'run', 'at', 'probes', 'functions', 'notes'), r))
                        for r in db.execute('SELECT id, run, at, probes, functions, notes FROM harvests')]
            called = {a for (a,) in db.execute('SELECT DISTINCT address FROM harvest_counts WHERE calls > 0')}
            oracle = {}
            for a, res in db.execute('SELECT address, result FROM oracle_results WHERE meets = 1 ORDER BY at'):
                oracle[a] = json.loads(res)
            experiments = defaultdict(list)
            for a, verdict, hyp in db.execute('SELECT address, verdict, hypothesis FROM experiments ORDER BY at'):
                experiments[a].append({'verdict': verdict, 'hypothesis': json.loads(hyp)})
            watches = defaultdict(list)
            for watched, fn, access, hits, field in db.execute('SELECT address, function, access, hits, field FROM watches'):
                watches[fn].append({'address': hx(watched), 'access': access, 'hits': hits, 'field': field})
        finally:
            db.close()
        functions = {}
        for a in sorted(called | set(oracle) | set(experiments) | set(watches)):
            if a not in engine.functions:
                continue
            rt = self.runtime(a)
            entry = {'name': engine.functions[a]['name'], 'status': engine.functions[a]['status'],
                     'calls': sum(c[1] for c in rt['counts'])}
            fits = [{'run': r[0], 'action': r[1], 'kind': r[2], 'perUnit': r[3], 'r2': r[4]} for r in rt['fits'] if not r[7]]
            if fits:
                entry['actions'] = fits
            if rt['callers']:
                vts = [s[1] for s in rt['samples'] if s[1]]
                cs = []
                for ret, n in rt['callers'][:max_callers]:
                    c = resolve_caller(engine, image, a, ret, n, vts)
                    k = c['call']
                    cs.append({k2: v for k2, v in (('return', c['returnAddress']), ('calls', n),
                                                   ('from', (c.get('function') or {}).get('name')),
                                                   ('fromAddress', (c.get('function') or {}).get('address')),
                                                   ('kind', k.get('kind')), ('resolved', k.get('resolved')),
                                                   ('slot', k.get('slotIndex') if k.get('slotOf') else None),
                                                   ('slotOf', k.get('slotOf')), ('declaredBy', k.get('declaredBy')))
                               if v is not None})
                entry['callers'] = cs
            observed = [s[2] for s in rt['samples'] if s[2]]
            if observed:
                entry['this'] = {'classes': dict(Counter(observed).most_common(8)), 'samples': len(observed),
                                 'distinct': len({s[0] for s in rt['samples'] if s[0]}),
                                 'commonBase': common_ancestor(engine, observed)}
                squeeze = layout_squeeze(engine, image, a, observed) if '__thiscall' in \
                    (engine.functions[a].get('signature') or '') else None
                if squeeze:
                    entry['layoutSqueeze'] = squeeze
                    # refuter (PROOF 4.5): a proven class the squeeze's owner contradicts
                    pname = proven_name(engine.functions[a])
                    if squeeze.get('owner') and pname and \
                            '::' in pname and pname.rsplit('::', 1)[0] != squeeze['owner']:
                        entry['recheck'] = ('class %s, but the layout squeeze puts the method in %s (fields %s)' %
                                            (pname.rsplit('::', 1)[0], squeeze['owner'],
                                             ', '.join(list(squeeze.get('fields') or {})[:3])))
                # refuter (PROOF 4.5): a proven Class::Method whose `this` was never that class nor derived from it
                name = proven_name(engine.functions[a]) or ''
                cls = name.rsplit('::', 1)[0] if '::' in name else None
                if cls and cls in (engine.rtti or {}) and len(observed) >= 8 \
                        and '__thiscall' in (engine.functions[a].get('signature') or '') \
                        and not any(is_a(engine, o, cls) for o in set(observed)):
                    entry['recheck'] = ('proven as %s, but `this` was %s in %d samples, none of them %s or derived '
                                        'from it' % (name, ', '.join(sorted(set(observed))[:4]), len(observed), cls))
            if rt['samples']:
                entry['returns'] = return_profile(rt['samples'], return_width(engine.functions[a].get('signature')))
                entry['threads'] = dict(Counter('main' if s[13] else 'worker' for s in rt['samples']))
            if a in oracle:
                entry['oracle'] = [{k: m.get(k) for k in ('oracle', 'source', 'meaning', 'objects', 'classes', 'distinct',
                                                          'chance', 'shuffledMatches')}
                                   for m in oracle[a]['matches'] if m.get('meetsRule42')][:4]
                entry['oracleMode'] = oracle[a].get('mode')
            if a in experiments:
                entry['experiments'] = experiments[a][-4:]
            if a in watches:
                entry['fieldWatch'] = watches[a][:4]
            functions[hx(a)] = entry
        out = {'format': 'fnvsym-trace/2', 'tool': 'fnv-mcp (geck-mcp-nv bridge)', 'game': 'FalloutNV.exe 1.4.0.525',
               'exported': time.strftime('%Y-%m-%d %H:%M:%S'), 'engineMap': engine.identity(),
               'methods': calibration_summary(self.live.project.state_dir), 'runs': runs, 'harvests': harvests,
               'functions': functions}
        if not functions:      # nothing recorded: no empty trace file in the decomp tree (T3)
            return {'changed': False, 'exported': 0, 'path': None, 'functions': 0,
                    'note': 'Nothing to export: no harvested calls, oracle results, experiments or field watches yet.'}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=1, default=str), encoding='utf-8')
        return {'changed': True, 'exported': len(functions), 'path': str(path), 'functions': len(functions),
                'withOracle': sum(1 for f in functions.values() if 'oracle' in f),
                'withExperiments': sum(1 for f in functions.values() if 'experiments' in f),
                'withFieldWatch': sum(1 for f in functions.values() if 'fieldWatch' in f),
                'withVirtualCallers': sum(1 for f in functions.values()
                                          if any(c.get('slotOf') for c in f.get('callers') or [])),
                'ownerByLayoutSqueeze': sum(1 for f in functions.values() if (f.get('layoutSqueeze') or {}).get('owner')),
                'recheck': sum(1 for f in functions.values() if f.get('recheck')),
                'methods': out['methods']}

    def callers(self, ref, harvest=None):
        engine = self.live.engine()
        address = engine.resolve(ref)
        rt = self.runtime(address, harvest)
        vts = [s[1] for s in rt['samples'] if s[1]]
        image = self.live.deep._image(required=False)
        return [resolve_caller(engine, image, address, ret, n, vts) for ret, n in rt['callers']]

    # ------------------------------------------------------------------ the dossier
    def dossier(self, ref, harvest=None, max_items=12):
        engine = self.live.engine()
        idx = self.index()
        address = engine.resolve(ref)
        f = engine.functions[address]
        static = {'function': engine.summary(address), 'size': f.get('size'), 'signature': f.get('signature')}
        if f.get('proof'):
            static['proof'] = {k: f['proof'].get(k) for k in ('status', 'name', 'identity', 'behaviour', 'by')}
        callers, capped = engine.walk(address, 'callers', 1, 60)
        callees, capped2 = engine.walk(address, 'callees', 1, 60)
        static['staticCallers'] = {'count': len(callers) if not capped else '60+',
                                   'list': [{k: c[k] for k in ('address', 'name', 'status', 'edge') if k in c}
                                            for c in callers[:max_items]]}
        static['callees'] = {'count': len(callees) if not capped2 else '60+',
                             'list': [{k: c[k] for k in ('address', 'name', 'status', 'edge') if k in c}
                                      for c in callees[:max_items]]}
        slots = idx.slots().get(address, [])
        if slots:
            classes = sorted({s['class'] for s in slots})
            lca = common_ancestor(engine, classes)
            static['vtableSlots'] = {'count': len(slots), 'classes': len(classes),
                                     'list': slots[:max_items], 'commonBase': lca,
                                     'folded': len(classes) > 1 and lca in (None, 'BaseFormComponent', 'NiRefObject',
                                                                             'NiMemObject')}
        static.update(body_strings(engine, idx.statics(), address))
        fnvsym = {'claims': idx.claims(address)[:30]}
        if idx.names_row(address):
            r = idx.names_row(address)
            fnvsym['names'] = {k: r.get(k) for k in ('proposed', 'conf', 'status', 'tags', 'evidence')}
        if idx.verified_row(address):
            r = idx.verified_row(address)
            fnvsym['verified'] = {k: r.get(k) for k in ('name', 'status', 'identity', 'behaviour', 'by')}
        static['fnvsym'] = fnvsym
        runtime = self._runtime_view(engine, address, harvest, max_items)
        return {'address': hx(address), 'static': static, 'runtime': runtime,
                'suggestions': self._suggest(engine, address, f, static, runtime)}

    def _runtime_view(self, engine, address, harvest, max_items):
        rt = self.runtime(address, harvest)
        samples = rt['samples']
        out = {'harvests': sorted({c[0] for c in rt['counts']}), 'calls': sum(c[1] for c in rt['counts']),
               'samples': len(samples)}
        if rt['idle'] and rt['idle'][0]:
            out['idleCallsPerSecond'] = round((rt['idle'][1] or 0) / rt['idle'][0], 2)
        if rt['fits']:
            out['actions'] = [{'run': r[0], 'action': r[1], 'kind': r[2], 'perUnit': r[3], 'r2': r[4], 'idle': r[5],
                               'acted': r[6], 'driver': bool(r[7])} for r in rt['fits']]
        if not samples and not rt['callers']:
            out['note'] = 'never seen running in a harvested probe set' if rt['counts'] == [] else 'no samples kept'
            return out
        vts = [s[1] for s in samples if s[1]]
        image = self.live.deep._image(required=False)
        resolved = [resolve_caller(engine, image, address, ret, n, vts) for ret, n in rt['callers'][:max_items]]
        out['callers'] = resolved
        kinds = Counter(c['call'].get('kind') for c in resolved)
        out['callKinds'] = dict(kinds)
        this_cls = Counter(s[2] or ('vtable 0x%08X (no MSVC RTTI)' % s[1] if s[1] else 'no this') for s in samples)
        out['thisClasses'] = dict(this_cls.most_common(max_items))
        observed = [s[2] for s in samples if s[2]]
        if observed:
            out['thisCommonBase'] = common_ancestor(engine, observed)
        forms = Counter(s[17] for s in samples if s[17] is not None)
        if forms:
            out['thisFormTypes'] = dict(forms.most_common(8))
        out['distinctThis'] = len({s[0] for s in samples if s[0]})
        for i, name in ((6, 'arg0Classes'), (7, 'arg1Classes')):
            c = Counter(s[i] for s in samples if s[i])
            if c:
                out[name] = dict(c.most_common(6))
        out['returns'] = return_profile(samples, return_width(engine.functions[address].get('signature')))
        threads = Counter('main' if s[13] else 'worker' for s in samples)
        out['threads'] = dict(threads)
        chains = Counter()
        for c in resolved[:3]:
            fn = (c.get('function') or {})
            caller_addr = int(fn['address'], 16) if fn.get('address') else None
            upper = self.runtime(caller_addr, harvest)['callers'][:1] if caller_addr else []
            above = engine.containing(upper[0][0] - 1) if upper else None
            chains[' <- '.join(filter(None, [engine.functions[address]['name'], fn.get('name'),
                                              engine.functions[above[0]]['name'] if above else None]))] += c['count']
        out['topChains'] = [{'chain': k, 'calls': v} for k, v in chains.most_common(3)]
        return out

    def _suggest(self, engine, address, f, static, runtime):
        out = []
        sig = f.get('signature') or ''
        r = runtime.get('returns') or {}
        if runtime.get('samples') and r.get('shape') in ('constant', 'bool', 'enum', 'int', 'float', 'pointer') and \
                ('__thiscall' in sig or runtime.get('thisCommonBase')):
            out.append({'tool': 'fnv_name_oracle', 'why': 'returns a %s from `this` (%s): compare with known values '
                        'on the same objects' % (r['shape'], runtime.get('thisCommonBase') or 'thiscall')})
        for a in runtime.get('actions') or []:
            if a['kind'] == 'per_unit' and not a['driver']:
                out.append({'tool': 'fnv_name_test', 'why': 'runs %.2f times per %s: skip it during %s and watch what '
                            'that action changes' % (a['perUnit'], a['action'], a['action'])})
        if static.get('vtableSlots', {}).get('folded'):
            out.append({'tool': 'none', 'why': 'one body in the vtables of unrelated classes: a folded body gets a '
                        'descriptive free-function name or none (PROOF 4.3)'})
        elif static.get('vtableSlots'):
            out.append({'tool': 'fnvsym family packet', 'why': 'virtual: slot %d of %s; the class comes from vtable '
                        'ownership (R)' % (static['vtableSlots']['list'][0]['slot'], static['vtableSlots']['commonBase'])})
        if runtime.get('thisCommonBase') and not static.get('vtableSlots'):
            out.append({'tool': 'layout squeeze', 'why': 'observed `this` classes share %s: an upper bound for the '
                        'owner class (PROOF 4.4); the largest field offset the body uses gives the lower bound'
                        % runtime['thisCommonBase']})
        return out


RETURN_WIDTH = (('bool', 8), ('char', 8), ('short', 16), ('wchar_t', 16))


def return_width(signature):
    """Bits of eax the declared return type uses (8 for bool/char, 16 for short, else 32), from the decomp
    signature's text before the calling convention."""
    head = (signature or '').split('__')[0]
    if '*' in head or '&' in head:
        return 32
    for word, bits in RETURN_WIDTH:
        if re.search(r'(?<![A-Za-z_])%s(?![A-Za-z_])' % word, head):
            return bits
    return 32


_LAYOUTS = None


def community_layouts():
    """fnvsym's evidence/layouts.json: per class, the members community headers declare, with `via` naming the
    base class that declares an inherited member."""
    global _LAYOUTS
    if _LAYOUTS is None:
        root = fnvsym_root(required=False)      # optional here: the layout tools work without it
        path = root / 'evidence' / 'layouts.json' if root else None
        _LAYOUTS = json.loads(path.read_text(encoding='utf-8')) if path and path.exists() else {}
    return _LAYOUTS


def this_offsets(engine, image, address, max_size=4000):
    """Field offsets the body reads or writes through `this` (ecx at entry, and registers copied from it until
    they are overwritten), from a linear scan of its machine code. Conservative: stops tracking a register at the
    first write to it."""
    import capstone
    from capstone import x86
    size = int(engine.functions[address].get('size') or 0)
    if not size or size > max_size or image is None:
        return []
    code = image.read(address, size)
    if not code:
        return []
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    md.detail = True
    tracked = {'ecx'}
    offs = set()
    for i in md.disasm(code, address):
        ops = i.operands
        for op in ops:
            if op.type == x86.X86_OP_MEM and op.mem.base and not op.mem.index:
                if i.reg_name(op.mem.base) in tracked and op.mem.disp >= 0:
                    offs.add(op.mem.disp)
        if i.mnemonic == 'mov' and len(ops) == 2 and ops[0].type == x86.X86_OP_REG:
            dst = i.reg_name(ops[0].reg)
            if ops[1].type == x86.X86_OP_REG and i.reg_name(ops[1].reg) in tracked:
                tracked.add(dst)
                continue
            tracked.discard(dst)
        elif ops and ops[0].type == x86.X86_OP_REG and (ops[0].access & 2):
            tracked.discard(i.reg_name(ops[0].reg))
        if i.mnemonic == 'call':
            tracked -= {'eax', 'ecx', 'edx'}
        if not tracked:
            break
    return sorted(offs)


def layout_squeeze(engine, image, address, observed):
    """PROOF 4.4: the owner class lies between an upper bound (the common ancestor of every observed `this`
    class) and a lower bound (the most derived class that declares a field the body uses through `this`). When
    they are the same class, that class is the only one the method can belong to."""
    if len(observed) < 20:
        return None
    upper = common_ancestor(engine, observed)
    if not upper:
        return None
    layout = community_layouts().get(upper)
    if not layout:
        return {'upper': upper, 'owner': None, 'why': 'no community layout for %s' % upper}
    chain = ancestors(engine, upper)
    declared = {}
    for off in this_offsets(engine, image, address):
        hits = [m for m in layout if m.get('offset') is not None and m['offset'] <= off < m['offset'] +
                max(1, m.get('size_guess') or 1)]
        if hits:
            declared[off] = hits[0].get('via') or upper
    if not declared:
        return {'upper': upper, 'owner': None, 'why': 'the body uses no field the community layout of %s names' % upper}
    known = [c for c in declared.values() if c in chain]
    if not known:
        return {'upper': upper, 'owner': None, 'why': 'declaring classes are not in the RTTI chain of %s' % upper}
    lower = min(known, key=chain.index)  # the most derived declaring class
    return {'upper': upper, 'lower': lower, 'owner': upper if lower == upper else None,
            'fields': {'0x%X' % o: c for o, c in sorted(declared.items())},
            'samples': len(observed),
            'why': 'upper bound = lower bound' if lower == upper else
            'window %s .. %s holds more than one class' % (lower, upper)}


def proven_name(f):
    """The name (or, for class proven, the class) the proof record holds; None when nothing is proven. The engine
    map's own `name` can be the decomp's stand-in (S00d22760_Capsule), which is no claim at all."""
    proof = f.get('proof') or {}
    if f.get('status') not in ('proven', 'class_proven') or not proof.get('name'):
        return None
    name = proof['name']
    return name if '::' in name else name + '::'


def calibration_summary(state_dir):
    """The measured accuracy of each runtime method (PROOF 4.1), from the newest blind runs in state/: oracle claims
    scored by the answer key plus the independent judgement of unkeyed claims, and the experiment battery."""
    state_dir = Path(state_dir)
    out = {}
    # the oracle: the newest call-mode run plus the newest passive run (disjoint sets: passive takes only the
    # getters call mode cannot run), each claim scored by the key or, unkeyed, by the independent judgement
    runs = [sorted(state_dir.glob(p))[-1] for p in ('oracle_calibration_*.json', 'oracle_passive_*.json')
            if sorted(state_dir.glob(p))]
    if runs:
        judged = {}
        for j in sorted(state_dir.glob('oracle_judgement_*.json')):
            for row in json.loads(j.read_text(encoding='utf-8')):
                judged[row['address']] = row['verdict']
        correct = wrong = cases = control = unjudged = 0
        for path in runs:
            c = json.loads(path.read_text(encoding='utf-8'))
            correct += c['correct']
            wrong += c['wrong']
            cases += c['cases']
            control += c.get('shuffledControlClaims') or 0
            for u in c.get('unkeyed') or []:
                v = judged.get(u['address'])
                correct += v == 'correct'
                wrong += v == 'wrong'
                unjudged += v is None
        claims = correct + wrong
        out['oracle'] = {'file': ' + '.join(p.name for p in runs), 'cases': cases, 'claims': claims,
                         'correct': correct, 'wrong': wrong, 'unjudged': unjudged,
                         'precision': round(correct / claims, 4) if claims else None,
                         'shuffledControlClaims': control,
                         'passesGate41': claims >= 50 and correct / claims >= 0.98 and not control}
    bat = sorted(state_dir.glob('experiment_battery_*.json'))
    if bat:
        b = json.loads(bat[-1].read_text(encoding='utf-8'))
        claims = b['correct'] + b['wrong']
        out['experiment'] = {'file': bat[-1].name, 'cases': b['cases'], 'claims': claims, 'correct': b['correct'],
                             'wrong': b['wrong'], 'precision': b.get('precision'),
                             'passesGate41': claims >= 50 and b['correct'] / claims >= 0.98}
    sq = sorted(state_dir.glob('squeeze_calibration_*.json'))
    if sq:
        c = json.loads(sq[-1].read_text(encoding='utf-8'))
        out['layoutSqueeze'] = {k: c.get(k) for k in ('cases', 'claims', 'correct', 'wrong', 'precision',
                                                      'shuffledControlCorrectFromDifferentEvidence', 'passesGate41')}
        out['layoutSqueeze']['file'] = sq[-1].name
    out['fieldWatch'] = {'note': 'no blind calibration yet: a field watch names the function that touched the field; '
                                 'the field\'s meaning must be proven separately (PROOF 4.2)', 'passesGate41': False}
    return out


def return_profile(samples, bits=32):
    """The result's shape from samples: constant, bool, small enum, int range, float range or pointer class.
    `bits`: the declared return width; the rest of eax is whatever the function left there."""
    mask = (1 << bits) - 1
    states = Counter(s[8] for s in samples)
    floats = [s[10] for s in samples if s[8] == 3 and s[10] is not None]
    ints = [s[9] & mask for s in samples if s[8] in (2, 3) and s[9] is not None]
    out = {'withResult': states.get(2, 0) + states.get(3, 0), 'noExitSample': states.get(1, 0) + states.get(0, 0)}
    if floats and len(floats) >= max(1, len(ints) // 2):
        out.update(shape='float', min=min(floats), max=max(floats), distinct=len(set(floats)))
        return out
    if not ints:
        out['shape'] = 'unknown'
        return out
    ptr_cls = Counter(s[18] for s in samples if s[18])
    distinct = set(ints)
    if bits < 32:
        out['bits'] = bits
    if ptr_cls and bits == 32 and sum(ptr_cls.values()) >= len(ints) / 2:
        out.update(shape='pointer', classes=dict(ptr_cls.most_common(6)),
                   null=sum(1 for v in ints if v == 0))
    elif len(distinct) == 1:
        out.update(shape='constant', value=next(iter(distinct)), samples=len(ints))
    elif distinct <= {0, 1}:
        out.update(shape='bool', trueShare=round(sum(1 for v in ints if v == 1) / len(ints), 3))
    elif len(distinct) <= 16 and max(distinct) < 0x10000:
        out.update(shape='enum', values=sorted(distinct))
    else:
        signed = [v - (1 << bits) if v >= 1 << (bits - 1) else v for v in ints]
        out.update(shape='int', min=min(signed), max=max(signed), distinct=len(distinct))
    return out
