"""Build the symbol database (state/symdb.sqlite) from your own fnv_decomp checkout.

Every unit of the decomp is compiled with /Z7 added, the code is checked against the game image (or against a
second compile without /Z7), and the CodeView debug sections are parsed (engine_map/codeview.py) and stored:
source lines, locals and compiler-exact type layouts. Nothing is written except the --out file. See
docs/FNV-SYMDB.md.

    python -m engine_map.symdb --decomp C:/src/fnv_decomp --out state/symdb.sqlite \\
        --image %USERPROFILE%/.fnvdecomp/FalloutNV.exe [--jobs N] [--only TEXT]... [--limit N]

The work is in three separate steps: build_unit (compile, check, parse one unit; returns a plain dict),
Store.add_unit (write one unit's result), and build (select units, process pool, progress, atomic replace).
"""
import argparse
import datetime
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = 'engine_map'

from . import symdb_schema  # noqa: E402

ROOT_VARS = {'sp1': 'FNV_MSVC9_ROOT', 'rtm': 'FNV_MSVC9_RTM_ROOT', 'vc8': 'FNV_MSVC8_ROOT'}
COUNT_KEYS = ('units', 'unitsOk', 'unitsFailed', 'unitsPartial', 'functions', 'functionsWithLines', 'functionsVerified',
              'types')
FAILED_SHOWN = 20
COMPILE_TIMEOUT = 900
REPLACE_WAIT = 30       # seconds to retry the final swap when <out> is in use
REPLACE_STEP = 1


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def tail(text, size=400):
    text = (text or '').strip()
    return text[-size:]


# --- worker: compile, check and parse one unit -------------------------------------------------------------------

def compiler_env():
    """The environment for the compiler: ours, with the RTM root taken from FNVCHECK_RTM_ROOT when unset."""
    env = dict(os.environ)
    if not env.get('FNV_MSVC9_RTM_ROOT') and env.get('FNVCHECK_RTM_ROOT'):
        env['FNV_MSVC9_RTM_ROOT'] = env['FNVCHECK_RTM_ROOT']
    return env


def compile_object(decomp, unit, work, name, z7, env):
    """Compile the unit into work/name with tools/msvc.py. Returns (ok, output tail)."""
    obj = Path(work) / name
    command = [sys.executable, str(Path(decomp) / 'tools' / 'msvc.py'), '--compiler', unit['compiler'], '/c',
               '/nologo'] + list(unit['flags']) + (['/Z7'] if z7 else []) + [
               '/I' + str(Path(decomp) / 'include'), str(Path(decomp) / unit['source']), '/Fo' + str(obj)]
    try:
        r = subprocess.run(command, cwd=str(work), env=env, capture_output=True, text=True, errors='replace',
                           stdin=subprocess.DEVNULL, timeout=COMPILE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, 'compiler timed out after %d s' % COMPILE_TIMEOUT
    except OSError as e:
        return False, 'cannot run the compiler: %s' % e
    if r.returncode or not obj.exists():
        return False, tail(r.stdout + r.stderr) or 'compiler exit code %d' % r.returncode
    return True, ''


def same_code(first, second, functions):
    """Which functions have the same code in two objects (Coff-like: named(), sections, symbols)? The 4 bytes of
    every relocation are masked, and the relocation lists (offset and kind) must be equal.
    Returns the set of listed addresses (ints) that are equal."""
    equal = set()
    for f in functions:
        try:
            views = []
            for coff in (first, second):
                s = coff.named(f['symbol'])
                sec = coff.sections[s['section']]
                start, size = s['value'], f['size']
                data = bytearray(sec['data'][start:start + size])
                relocs = sorted((o - start, k) for o, _, k in sec['relocs'] if start <= o < start + size)
                for o, _ in relocs:
                    data[o:o + 4] = b'\0\0\0\0'
                views.append((bytes(data), relocs))
            if views[0] == views[1] and len(views[0][0]) == f['size']:
                equal.add(int(f['address'], 16))
        except (ValueError, KeyError, IndexError):
            continue
    return equal


def verify_image(unit, coff, pe):
    """Addresses (ints) whose code matches the game image, by the decomp's compare()."""
    from compare import compare
    manifest = dict(symbols=unit.get('symbols', {}), functions=unit['functions'],
                    literal_symbols=unit.get('literal_symbols', []))
    return {int(r['address'], 16) for r in compare(coff, pe, manifest) if r['verdict'] == 'match'}


def _parser():
    """The CodeView parser module (imported late, so tests can replace this function)."""
    from . import codeview
    return codeview


_STATE = {'decomp': None, 'pe': None, 'pe_error': None, 'image': None}


def init_worker(decomp, image):
    """Process initializer: decomp tools on sys.path, the game image loaded once."""
    _STATE.update(decomp=str(decomp), image=image, pe=None, pe_error=None)
    tools = str(Path(decomp) / 'tools')
    if tools not in sys.path:
        sys.path.insert(0, tools)
    if image:
        try:
            from compare import Pe
            _STATE['pe'] = Pe(image)
        except Exception as e:
            _STATE['pe_error'] = 'cannot load the image: %s' % e


def run_unit(unit):
    """Pool entry point: build one unit with this process's state."""
    return build_unit(unit, _STATE['decomp'], _STATE['pe'], _STATE['pe_error'])


def build_unit(unit, decomp, pe=None, pe_error=None, env=None):
    """Compile, check and parse one unit. Returns a plain dict:
    source, compiler, flags, status, error, seconds, mode ('image', 'double-compile' or None),
    verified (list of int addresses), parsed (the parser's result or None), fallback (error tail when the image
    check was tried and raised, else None). Both checks read objects with the parser's ObjectFile."""
    started = time.time()
    result = {'source': unit['source'], 'compiler': unit['compiler'], 'flags': ' '.join(unit['flags']),
              'status': 'compile-failed', 'error': None, 'seconds': 0.0, 'mode': None, 'verified': [],
              'parsed': None, 'fallback': None}
    env = env if env is not None else compiler_env()
    variable = ROOT_VARS.get(unit['compiler'])
    if not variable or not env.get(variable):
        result.update(status='no-compiler', error='%s is not set (compiler %s)' % (variable, unit['compiler']))
        return result
    work = tempfile.mkdtemp(prefix='fnvsymdb-')
    stage = 'compile'
    notes = []
    try:
        ok, text = compile_object(decomp, unit, work, 'unit.obj', True, env)
        if not ok:
            result['error'] = text
            return result
        obj = os.path.join(work, 'unit.obj')
        verified = None
        stage = 'verify'
        if pe is not None:
            try:
                verified = verify_image(unit, _parser().ObjectFile(obj), pe)
                result['mode'] = 'image'
            except Exception as e:
                result['fallback'] = tail('%s: %s' % (type(e).__name__, e), 150)
                notes.append('image check failed (%s), used a second compile' % str(e)[:150])
        elif pe_error:
            notes.append(pe_error)
        if verified is None:
            try:
                ok, text = compile_object(decomp, unit, work, 'plain.obj', False, env)
                if ok:
                    object_file = _parser().ObjectFile
                    verified = same_code(object_file(obj), object_file(os.path.join(work, 'plain.obj')),
                                         unit['functions'])
                    result['mode'] = 'double-compile'
                else:
                    notes.append('second compile failed: %s' % text)
            except Exception as e:
                notes.append('double-compile check failed: %s' % str(e)[:150])
        result['verified'] = sorted(verified or ())
        stage = 'parse'
        try:
            codeview = _parser()
            result['parsed'] = codeview.parse_object(codeview.ObjectFile(obj))
        except Exception as e:
            result.update(status='parse-error', error=tail('%s: %s' % (type(e).__name__, e)))
            return result
        listed = {int(f['address'], 16) for f in unit['functions']}
        missing = sorted(listed - set(result['verified']))
        if missing:
            notes.insert(0, '%d of %d functions not verified (first: %s)' % (
                len(missing), len(listed), ', '.join('0x%08x' % a for a in missing[:3])))
        result['status'] = 'partial' if missing else 'ok'
        if notes:
            result['error'] = tail('; '.join(notes))
        return result
    except Exception as e:
        result.update(status='parse-error' if stage == 'parse' else 'compile-failed',
                      error=tail('%s in %s: %s' % (type(e).__name__, stage, traceback.format_exc()[-300:])))
        return result
    finally:
        result['seconds'] = round(time.time() - started, 2)
        shutil.rmtree(work, ignore_errors=True)


# --- source paths -------------------------------------------------------------------------------------------------

class PathMapper:
    """Maps the compiler's lower-cased absolute source paths back to the real-case path relative to the decomp
    root. The lower-case index of the tree is built once, on first use."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.prefix = self._norm(str(self.root)).rstrip('/') + '/'
        self.index = None

    @staticmethod
    def _norm(path):
        return posixpath.normpath(path.replace('\\', '/')).lower()

    def _build(self):
        self.index = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            top = dirpath == str(self.root)
            dirnames[:] = [d for d in dirnames if not d.startswith('.') and not (top and d == 'build')]
            rel_dir = os.path.relpath(dirpath, self.root).replace('\\', '/')
            for name in filenames:
                rel = name if rel_dir == '.' else rel_dir + '/' + name
                self.index[self.prefix + rel.lower()] = rel

    def map(self, path):
        norm = self._norm(path)
        if not norm.startswith(self.prefix):
            return path.replace('\\', '/')
        if self.index is None:
            self._build()
        return self.index.get(norm, norm[len(self.prefix):])


# --- store: write one unit's result -------------------------------------------------------------------------------

def hex_int(text):
    return int(text, 16) if isinstance(text, str) else int(text)


class Store:
    """The only writer of the database. add_unit() writes one unit's result; finish() writes meta."""

    def __init__(self, connection, mapper):
        self.db = connection
        self.mapper = mapper
        self.type_ids = {}        # hash -> id
        self.next_type = 1
        self.file_ids = {}        # real path -> id
        self.rank = {}            # function address -> (verified, -order) of the row that holds it
        self.unit_keys = set()

    def file_id(self, path):
        path = self.mapper.map(path)
        found = self.file_ids.get(path)
        if found is None:
            found = self.db.execute('INSERT INTO files(path) VALUES(?)', (path,)).lastrowid
            self.file_ids[path] = found
        return found

    def type_id(self, hash_):
        return self.type_ids.get(hash_) if hash_ else None

    def add_types(self, types):
        """Store the types of one unit: new hashes are inserted, known ones get `seen` raised."""
        new = [h for h in types if h not in self.type_ids]
        for h in new:
            self.type_ids[h] = self.next_type
            self.next_type += 1
        fresh = set(new)
        known = [(self.type_ids[h],) for h in types if h not in fresh]
        ids = self.type_ids
        rows, members, bases, methods, enums, args = [], [], [], [], [], []
        for h in new:
            t, i = types[h], ids[h]
            attrs = t.get('attrs') or None
            rows.append((i, h, t['kind'], t.get('name'), t.get('size'), ids.get(t.get('target')),
                         json.dumps(attrs, sort_keys=True, separators=(',', ':')) if attrs else None))
            for n, m in enumerate(t.get('members') or ()):
                members.append((i, n, m.get('name'), m['offset'], ids.get(m.get('type')), m.get('bitPos'),
                                m.get('bitLen')))
            for n, b in enumerate(t.get('bases') or ()):
                bases.append((i, n, ids.get(b.get('type')), b.get('offset'), int(bool(b.get('virtual')))))
            for n, m in enumerate(t.get('methods') or ()):
                methods.append((i, n, m['name'], ids.get(m.get('type')), int(bool(m.get('virtual'))),
                                int(bool(m.get('static'))), m.get('vtOffset')))
            for n, (name, value) in enumerate(t.get('enumerators') or ()):
                enums.append((i, n, name, value))
            for n, a in enumerate(t.get('args') or ()):
                args.append((i, n, ids.get(a)))
        db = self.db
        db.executemany('INSERT INTO types(id, hash, kind, name, size, target_id, attrs) VALUES(?,?,?,?,?,?,?)', rows)
        db.executemany('UPDATE types SET seen = seen + 1 WHERE id = ?', known)
        db.executemany('INSERT INTO members VALUES(?,?,?,?,?,?,?)', members)
        db.executemany('INSERT INTO bases VALUES(?,?,?,?,?)', bases)
        db.executemany('INSERT INTO methods VALUES(?,?,?,?,?,?,?)', methods)
        db.executemany('INSERT INTO enums VALUES(?,?,?,?)', enums)
        db.executemany('INSERT INTO args VALUES(?,?,?)', args)

    def add_unit(self, unit, result, order=0, key=None):
        """Write one unit (its units.json entry and the worker's result). Returns the unit id."""
        db = self.db
        key = key or unit['source']
        if key in self.unit_keys:
            n = 2
            while '%s#%d' % (key, n) in self.unit_keys:
                n += 1
            key = '%s#%d' % (key, n)
        self.unit_keys.add(key)
        verified = set(result.get('verified') or ())
        listed = [(hex_int(f['address']), f) for f in unit['functions']]
        unit_id = db.execute(
            'INSERT INTO units(source, compiler, flags, status, error, functions, verified, seconds) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (key, result.get('compiler'), result.get('flags'), result['status'], result.get('error'),
             len(listed), sum(1 for a, _ in listed if a in verified), result.get('seconds'))).lastrowid
        parsed = result.get('parsed')
        by_symbol = {}
        files = []
        if parsed:
            self.add_types(parsed.get('types') or {})
            files = parsed.get('files') or []
            for p in parsed.get('functions') or ():
                by_symbol.setdefault(p.get('symbol'), p)
        func_rows, line_rows, local_rows = [], [], []
        for address, f in listed:
            is_verified = address in verified
            rank = (int(is_verified), -order)
            if address in self.rank:
                if rank <= self.rank[address]:
                    continue
                for table in ('functions', 'lines', 'locals'):
                    db.execute('DELETE FROM %s WHERE %s = ?' % (table, 'addr' if table == 'functions' else 'func_addr'),
                               (address,))
                func_rows = [r for r in func_rows if r[0] != address]
                line_rows = [r for r in line_rows if r[0] != address]
                local_rows = [r for r in local_rows if r[0] != address]
            self.rank[address] = rank
            p = next((by_symbol[s] for s in [f['symbol']] + list(f.get('fold_aliases') or ()) if s in by_symbol), None)
            func_rows.append((address, f['size'], unit_id, p['name'] if p else None, f['symbol'], int(is_verified),
                              int(p is not None), self.type_id(p.get('type')) if p else None,
                              p.get('procFlags') if p else None, p.get('frameBytes') if p else None,
                              p.get('frameFlags') if p else None, p.get('debugStart') if p else None,
                              p.get('debugEnd') if p else None))
            if not (p and is_verified):
                continue
            seen_offsets = {}
            for offset, file_index, line in p.get('lines') or ():
                if 0 <= offset < f['size'] and 0 <= file_index < len(files):
                    seen_offsets[offset] = (file_index, line)       # one row per offset: the last one wins
            for offset, (file_index, line) in sorted(seen_offsets.items()):
                line_rows.append((address, offset, self.file_id(files[file_index]), line))
            for n, loc in enumerate(p.get('locals') or ()):
                scope = loc.get('scope')
                local_rows.append((address, n, loc['name'], loc['kind'], loc.get('reg'), loc.get('offset'),
                                   self.type_id(loc.get('type')), scope[0] if scope else None,
                                   scope[1] if scope else None))
        db.executemany('INSERT INTO functions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', func_rows)
        db.executemany('INSERT INTO lines VALUES(?,?,?,?)', line_rows)
        db.executemany('INSERT INTO locals VALUES(?,?,?,?,?,?,?,?,?)', local_rows)
        symbols = unit.get('symbols') or {}
        global_rows = []
        for g in (parsed or {}).get('globals') or ():
            decorated = g.get('symbol')
            address = symbols.get(decorated) if decorated else None
            global_rows.append((g['name'], decorated, hex_int(address) if address is not None else None,
                                self.type_id(g.get('type')), unit_id, int(bool(g.get('isGlobal', True)))))
        db.executemany('INSERT INTO globals(name, decorated, addr, type_id, unit_id, is_global) VALUES(?,?,?,?,?,?)',
                       global_rows)
        return unit_id

    def counts(self):
        one = lambda sql: self.db.execute(sql).fetchone()[0]    # noqa: E731
        return {'units': one('SELECT COUNT(*) FROM units'),
                'unitsOk': one("SELECT COUNT(*) FROM units WHERE status = 'ok'"),
                'unitsFailed': one("SELECT COUNT(*) FROM units WHERE status NOT IN ('ok', 'partial')"),
                'unitsPartial': one("SELECT COUNT(*) FROM units WHERE status = 'partial'"),
                'functions': one('SELECT COUNT(*) FROM functions'),
                'functionsWithLines': one('SELECT COUNT(DISTINCT func_addr) FROM lines'),
                'functionsVerified': one('SELECT COUNT(*) FROM functions WHERE verified = 1'),
                'types': one('SELECT COUNT(*) FROM types')}

    def finish(self, meta):
        """Write meta (a dict of text-able values; None values are left out) and return the counts."""
        counts = self.counts()
        values = dict(meta, **counts)
        self.db.executemany('INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)',
                            [(k, str(v)) for k, v in values.items() if v is not None])
        self.db.commit()
        return counts


def open_building(path):
    """A fresh database at `path` with the build-time settings (no journal: a failed build is simply deleted)."""
    for suffix in ('', '-journal', '-wal', '-shm'):
        try:
            os.remove(str(path) + suffix)
        except FileNotFoundError:
            pass
    import sqlite3
    db = sqlite3.connect(str(path), isolation_level=None)
    db.executescript('PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; PRAGMA locking_mode = EXCLUSIVE; '
                     'PRAGMA temp_store = MEMORY; PRAGMA cache_size = -200000;')
    symdb_schema.create(db)      # executescript() commits, so the transaction starts after it
    db.execute('BEGIN')
    return db


# --- driver -------------------------------------------------------------------------------------------------------

def load_units(decomp):
    return json.loads((Path(decomp) / 'build' / 'units.json').read_text(encoding='utf-8'))['units']


def select_units(units, only=(), limit=None):
    """Units whose source contains any --only text (case-insensitive), then the first `limit` of them."""
    only = [t.lower().replace('\\', '/') for t in only or ()]
    chosen = [u for u in units if not only or any(t in u['source'].lower() for t in only)]
    return chosen[:limit] if limit else chosen


def unit_keys(units):
    """Unique `units.source` values: the second recipe of a source file becomes 'src/a.cpp#2'."""
    seen, keys = {}, []
    for u in units:
        n = seen[u['source']] = seen.get(u['source'], 0) + 1
        keys.append(u['source'] if n == 1 else '%s#%d' % (u['source'], n))
    return keys


def pool_factory(jobs, initializer, initargs):
    return ProcessPoolExecutor(max_workers=jobs, initializer=initializer, initargs=initargs)


def failed_result(unit, error):
    return {'source': unit['source'], 'compiler': unit['compiler'], 'flags': ' '.join(unit['flags']),
            'status': 'compile-failed', 'error': tail(error), 'seconds': 0.0, 'mode': None, 'verified': [],
            'parsed': None, 'fallback': None}


def swap_into_place(building, out):
    """Move `building` over `out`, retrying on PermissionError (Windows: another process has `out` open).
    Returns (True, None), or (False, message) with `building` kept."""
    deadline = time.time() + REPLACE_WAIT
    while True:
        try:
            os.replace(building, out)
            return True, None
        except PermissionError as e:
            if time.time() >= deadline:
                return False, ('The database was built, but %s is in use (probably the MCP server), so it was '
                               'not replaced (%s). Stop that process, then rename %s to %s.'
                               % (out, e, building, out))
        time.sleep(REPLACE_STEP)


def build(decomp, out, image=None, jobs=1, only=(), limit=None, units=None, worker=run_unit,
          make_pool=pool_factory, progress=100):
    """Build the database into `<out>.building`, then move it over `out`. Returns the summary dict."""
    from .generate import file_sha256, git_head
    started = time.time()
    decomp = Path(decomp).resolve()
    out = Path(out)
    units = select_units(units if units is not None else load_units(decomp), only, limit)
    keys = unit_keys(units)
    out.parent.mkdir(parents=True, exist_ok=True)
    building = Path(str(out) + '.building')
    db = open_building(building)
    try:
        store = Store(db, PathMapper(decomp))
        failures = []
        st = {'done': 0, 'failed': 0, 'partial': 0, 'last_commit': 0, 'fallbacks': 0}
        checks = {'image': 0, 'double-compile': 0, 'none': 0}
        fallback_units = []

        def record(order, unit, result):
            store.add_unit(unit, result, order, keys[order])
            st['done'] += 1
            checks[result.get('mode') or 'none'] += 1
            if result.get('fallback'):
                st['fallbacks'] += 1
                fallback_units.append((order, {'unit': keys[order], 'error': tail(result['fallback'], 150)}))
            if result['status'] == 'partial':
                st['partial'] += 1
            elif result['status'] != 'ok':
                st['failed'] += 1
            if result['status'] != 'ok':
                failures.append((order, {'unit': keys[order], 'status': result['status'],
                                         'error': tail(result.get('error'), 300)}))
            if st['done'] % progress == 0 or st['done'] == len(units):
                log('%d/%d units, %d failed, %d partial, %.0f s' % (
                    st['done'], len(units), st['failed'], st['partial'], time.time() - started))
            if st['done'] - st['last_commit'] >= 200:
                db.execute('COMMIT')
                db.execute('BEGIN')
                st['last_commit'] = st['done']

        def died(e):
            return 'worker process died (%s: %s)' % (type(e).__name__, e)

        pool_args = (jobs, init_worker, (str(decomp), str(image) if image else None))
        pool = make_pool(*pool_args)
        try:
            pending, queue = {}, iter(enumerate(units))
            while True:
                broken = None
                while broken is None and len(pending) < max(2, jobs * 3):
                    nxt = next(queue, None)
                    if nxt is None:
                        break
                    try:
                        pending[pool.submit(worker, nxt[1])] = nxt
                    except BrokenProcessPool as e:
                        broken = e
                        record(nxt[0], nxt[1], failed_result(nxt[1], died(e)))
                if not pending and broken is None:
                    break
                if pending and broken is None:
                    finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                    for future in finished:
                        order, unit = pending.pop(future)
                        try:
                            result = future.result()
                        except BrokenProcessPool as e:
                            broken = e
                            result = failed_result(unit, died(e))
                        except Exception as e:       # the worker raised
                            result = failed_result(unit, '%s: %s' % (type(e).__name__, e))
                        record(order, unit, result)
                if broken is not None:
                    # the units in flight are recorded as failed (never resubmitted); go on with a new pool
                    for future, (order, unit) in list(pending.items()):
                        record(order, unit, failed_result(unit, died(broken)))
                    pending.clear()
                    pool.shutdown(wait=False, cancel_futures=True)
                    pool = make_pool(*pool_args)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        meta = {'decompPath': str(decomp), 'decompCommit': git_head(decomp),
                'builtAt': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'mode': 'image' if image else 'double-compile',
                'imageSha256': file_sha256(image) if image else None,
                'unitsDoubleCompile': checks['double-compile'],
                'seconds': round(time.time() - started, 1)}
        counts = store.finish(meta)
        db.close()
        db = None
    except BaseException:
        if db is not None:
            db.close()
        try:
            os.remove(building)
        except OSError:
            pass
        raise
    replaced, swap_error = swap_into_place(building, out)
    summary = {'schema': symdb_schema.SCHEMA_VERSION, 'gameBuild': symdb_schema.GAME_BUILD}
    summary.update(meta)
    summary.update(counts)
    summary['out'] = str(out if replaced else building)
    summary['replaced'] = replaced
    if swap_error:
        summary['error'] = swap_error
    summary['checks'] = checks
    summary['fallbacks'] = st['fallbacks']
    summary['fallbackUnits'] = [f for _, f in sorted(fallback_units, key=lambda x: x[0])][:FAILED_SHOWN]
    summary['failed'] = [f for _, f in sorted(failures, key=lambda x: x[0])][:FAILED_SHOWN]
    return summary


def decomp_tools_missing(decomp):
    """None when the decomp's tools/compare.py (the image check, --image) imports; else what is missing and what to
    do. Checked once up front, so a build does not fall back unit by unit for a reason it could name at the start."""
    import importlib.util
    path = Path(decomp) / 'tools' / 'compare.py'
    if not path.is_file():
        return ('%s is missing: the image check (--image) uses the decomp checkout\'s own tools/compare.py. Update '
                'the checkout, or build without --image (each function is then checked by a second compile).' % path)
    try:
        spec = importlib.util.spec_from_file_location('_fnvmcp_compare_check', path)
        module = importlib.util.module_from_spec(spec)
        tools = str(path.parent)
        added = tools not in sys.path
        if added:
            sys.path.insert(0, tools)
        try:
            spec.loader.exec_module(module)
        finally:
            if added:
                sys.path.remove(tools)
    except Exception as e:  # noqa: BLE001 - any import failure is the answer
        return ('%s does not import (%s: %s). Install what it needs, or build without --image.'
                % (path, type(e).__name__, e))
    missing = [name for name in ('compare', 'Pe') if not hasattr(module, name)]
    if missing:
        return '%s has no %s: the checkout is older or newer than this tool expects.' % (path, ', '.join(missing))
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog='engine_map.symdb', description=__doc__.split('\n')[0])
    ap.add_argument('--decomp', required=True, help='fnv_decomp checkout')
    ap.add_argument('--out', required=True, help='database to write (built as <out>.building, then moved)')
    ap.add_argument('--image', help='your unpacked FalloutNV.exe: check every function against the game itself')
    ap.add_argument('--jobs', type=int, default=max(1, (os.cpu_count() or 2) - 2), help='worker processes')
    ap.add_argument('--only', action='append', default=[], help='only units whose source contains TEXT (repeatable)')
    ap.add_argument('--limit', type=int, help='stop after N units')
    args = ap.parse_args(argv)
    decomp = Path(args.decomp)
    if not (decomp / 'build' / 'units.json').exists():
        log('error: %s has no build/units.json' % decomp)
        return 2
    if args.image and not Path(args.image).is_file():
        log('error: image not found: %s' % args.image)
        return 2
    if args.image:
        missing = decomp_tools_missing(decomp)
        if missing:
            log('error NO_DECOMP_TOOLS: %s' % missing)
            return 2
    summary = build(decomp, args.out, args.image, max(1, args.jobs), args.only, args.limit)
    print(json.dumps(summary, indent=2))
    if not summary['replaced']:
        log('error: ' + summary['error'])
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
