"""Offline engine knowledge: queries over engine_map.json and the decomp checkout it was generated from.

Read-only. Names are labelled with their proof status: only 'proven' names (fnvsym verified.csv) are
evidence; 'decomp' names are the decompilation's own, often machine-made, and 'unnamed' are placeholders.
"""
import json
import os
import re
import threading
from bisect import bisect_right
from collections import OrderedDict, deque
from pathlib import Path

GAME_BUILD = '1.4.0.525'
EXE_RANGE = (0x00400000, 0x01300000)
STATUS_RANK = {'proven': 0, 'class_proven': 1, 'probable': 2, 'decomp': 3, 'rejected': 4, 'unnamed': 5}
PLACEHOLDER_REF = re.compile(r'\b(?:FUN|sub|thunk_FUN)_([0-9a-fA-F]{8})\b')
ADDRESS = re.compile(r'^(?:0x)?([0-9a-fA-F]{1,8})$')


class EngineMapError(Exception):
    def __init__(self, code, message, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence


def parse_address(text):
    if isinstance(text, int):
        return text
    m = ADDRESS.match(str(text).strip().replace('`', ''))
    if not m:
        return None
    return int(m.group(1), 16)


def hx(value):
    return '0x%08X' % value


def _words(text):
    """CamelCase and separators -> lowercase words: 'ApplyLimbDamage' -> ['apply','limb','damage']."""
    text = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', text)
    text = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', text)
    return [w for w in re.split(r'[^A-Za-z0-9]+', text.lower()) if w]


class EngineMap:
    """engine_map.json loaded once, with indexes for lookups."""

    def __init__(self, path, decomp_root=None):
        self.path = Path(path)
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if data.get('schema') != 1:
            raise EngineMapError('ENGINE_MAP_SCHEMA', 'Unsupported engine_map schema %r' % data.get('schema'))
        if data.get('game', {}).get('build') != GAME_BUILD:
            raise EngineMapError('ENGINE_MAP_BUILD', 'engine_map is for build %r, expected %s'
                                 % (data.get('game', {}).get('build'), GAME_BUILD))
        self.data = data
        self.generated = data.get('generated', {})
        root = decomp_root or (self.generated.get('decomp') or {}).get('path')
        self.decomp_root = Path(root) if root else None
        self.functions = {}
        for f in data['functions']:
            self.functions[int(f['address'], 16)] = f
        self.addresses = sorted(self.functions)
        self.calls = {int(a, 16): [int(t, 16) for t in ts] for a, ts in data.get('calls', {}).items()}
        self.tails = {int(a, 16): [int(t, 16) for t in ts] for a, ts in data.get('tailCalls', {}).items()}
        self.callers = {}
        for graph, kind in ((self.calls, 'call'), (self.tails, 'jump')):
            for caller, targets in graph.items():
                for t in targets:
                    self.callers.setdefault(t, []).append((caller, kind))
        self.by_name = {}
        for address, f in self.functions.items():
            names = {f['name'], f.get('decompName')}
            proof = f.get('proof') or {}
            if proof.get('name'):
                names.add(proof['name'])
            for n in names:
                if n:
                    self.by_name.setdefault(n.lower(), set()).add(address)
                    self.by_name.setdefault(n.split('::')[-1].lower(), set()).add(address)
        self.classes = data.get('classes', {})
        self.classes_lower = {k.lower(): k for k in self.classes}
        self.commands = data.get('commands', [])
        self.statics = data.get('statics', [])
        self.statics_by_address = {int(s['address'], 16): s for s in self.statics if s.get('address')}
        self.rtti = data.get('rtti') or {}
        self.rtti_lower = {k.lower(): k for k in self.rtti}
        self.vtables = {}
        self.derived = {}
        for cls, info in self.rtti.items():
            for vt in info.get('vtables', []):
                self.vtables[int(vt['address'], 16)] = (cls, vt)
            for base in info.get('directBases', []):
                self.derived.setdefault(base, []).append(cls)
        self.vtable_starts = sorted(self.vtables)
        self._files = OrderedDict()
        self._lock = threading.Lock()

    # ----------------------------------------------------------------------------- basics
    def identity(self):
        g = self.generated
        return {'engineMap': str(self.path), 'gameBuild': GAME_BUILD,
                'generatedAt': g.get('at'), 'decompCommit': (g.get('decomp') or {}).get('commit'),
                'verifiedSha256': (g.get('verified') or {}).get('sha256'),
                'exeImageCheck': ((g.get('exe') or {}).get('imageCheck') or {}).get('ok'),
                'callGraph': g.get('callGraph')}

    def summary(self, address, with_location=True):
        f = self.functions[address]
        out = {'address': f['address'], 'name': f['name'], 'status': f['status']}
        if f['decompName'] != f['name']:
            out['decompName'] = f['decompName']
        if with_location and f.get('file'):
            out['location'] = '%s:%s' % (f['file'], (f.get('lines') or [f.get('marker') or '?'])[0])
        return out

    def resolve(self, ref):
        """A function by address or by name (proven, decomp or placeholder). Raises on ambiguity."""
        address = parse_address(ref)
        if address is not None and address in self.functions:
            return address
        if address is not None and re.fullmatch(r'(0x)?[0-9a-fA-F]{6,8}', str(ref).strip()):
            hit = self.containing(address)
            if hit:
                return hit[0]
            raise EngineMapError('NOT_FOUND', 'No function contains address %s' % hx(address))
        m = PLACEHOLDER_REF.fullmatch(str(ref).strip())
        if m and int(m.group(1), 16) in self.functions:
            return int(m.group(1), 16)
        key = str(ref).strip().lower()
        hits = self.by_name.get(key, set())
        if not hits:
            raise EngineMapError('NOT_FOUND', 'No function named %r; try fnv_func_find' % ref)
        if len(hits) > 1:
            ranked = sorted(hits, key=lambda a: (STATUS_RANK.get(self.functions[a]['status'], 9),
                                                 self.functions[a]['name'].lower() != key, a))
            exact = [a for a in ranked if self.functions[a]['name'].lower() == key
                     and self.functions[a]['status'] == 'proven']
            if len(exact) == 1:
                return exact[0]
            raise EngineMapError('AMBIGUOUS', '%d functions match %r; pass an address' % (len(hits), ref),
                                 {'candidates': [self.summary(a) for a in ranked[:25]]})
        return next(iter(hits))

    def containing(self, address):
        """(function address, offset) for an address inside the exe's functions, or None."""
        i = bisect_right(self.addresses, address) - 1
        if i < 0:
            return None
        start = self.addresses[i]
        size = self.functions[start].get('size') or 0
        if start <= address < start + max(size, 1):
            return start, address - start
        return None

    # ----------------------------------------------------------------------------- source
    def _lines(self, rel):
        if self.decomp_root is None:
            raise EngineMapError('DECOMP_UNAVAILABLE', 'No decomp checkout configured (fnv.decomp_root)')
        with self._lock:
            if rel in self._files:
                self._files.move_to_end(rel)
                return self._files[rel]
        path = self.decomp_root / rel
        if not path.exists():
            raise EngineMapError('DECOMP_UNAVAILABLE', 'Decomp file missing: %s' % path)
        lines = path.read_text(encoding='utf-8', errors='replace').split('\n')
        with self._lock:
            self._files[rel] = lines
            while len(self._files) > 24:
                self._files.popitem(last=False)
        return lines

    def code(self, address, max_lines=400, annotate=True):
        f = self.functions[address]
        if not f.get('file'):
            raise EngineMapError('NO_SOURCE', 'The decomp has no source for %s' % f['address'])
        lines = self._lines(f['file'])
        if f.get('lines'):
            start, end = f['lines']
            first = start
            # keep the marker and comments right above the definition
            m = f.get('marker')
            if m and m < start and start - m <= 40:
                first = m
            text = lines[first - 1:end]
            exact = True
        elif f.get('marker'):
            first = f['marker']
            text = lines[first - 1:first + 30]
            exact = False
        else:
            raise EngineMapError('NO_SOURCE', 'No // @fnv marker for %s in %s' % (f['address'], f['file']))
        truncated = len(text) > max_lines
        if truncated:
            text = text[:max_lines]
        body = '\n'.join(text)
        names = {}
        if annotate:
            def repl(m):
                target = int(m.group(1), 16)
                t = self.functions.get(target)
                if t and t['status'] == 'proven' and not t['name'].endswith(m.group(0)):
                    names[m.group(0)] = t['name']
                    return '%s/*%s*/' % (m.group(0), t['name'])
                return m.group(0)
            body = PLACEHOLDER_REF.sub(repl, body)
        return {'file': f['file'], 'firstLine': first, 'lastLine': first + len(text) - 1,
                'exactExtent': exact, 'truncated': truncated, 'code': body, 'resolvedNames': names}

    # ----------------------------------------------------------------------------- search
    def find_by_name(self, query, statuses=None, limit=25, offset=0):
        words = _words(query)
        if not words:
            raise ValueError('query has no letters or digits')
        joined = ''.join(words)
        hits = []
        for address, f in self.functions.items():
            if statuses and f['status'] not in statuses:
                continue
            proof_name = (f.get('proof') or {}).get('name') or ''
            hay = ' '.join(_words(f['name']) + _words(f.get('decompName') or '') + _words(proof_name))
            flat = hay.replace(' ', '')
            if not all(w in flat for w in words):
                continue
            leaf = f['name'].split('::')[-1].lower()
            score = (STATUS_RANK.get(f['status'], 9),
                     0 if leaf == joined or f['name'].lower() == query.lower() else 1,
                     0 if joined in flat else 1, len(f['name']))
            hits.append((score, address))
        hits.sort()
        return len(hits), [self.summary(a) for _, a in hits[offset:offset + limit]]

    def find_in_code(self, pattern, regex=False, statuses=None, limit=25, offset=0, max_matches_per_function=3):
        if self.decomp_root is None:
            raise EngineMapError('DECOMP_UNAVAILABLE', 'No decomp checkout configured (fnv.decomp_root)')
        rx = re.compile(pattern if regex else re.escape(pattern), re.I)
        by_file = {}
        for address, f in self.functions.items():
            if f.get('file') and f.get('lines') and (not statuses or f['status'] in statuses):
                by_file.setdefault(f['file'], []).append(address)
        hits = []
        for rel in sorted(by_file):
            path = self.decomp_root / rel
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            if not rx.search(text):
                continue
            lines = text.split('\n')
            for address in by_file[rel]:
                start, end = self.functions[address]['lines']
                matches = []
                for n in range(start, end + 1):
                    if rx.search(lines[n - 1]):
                        matches.append({'line': n, 'text': lines[n - 1].strip()[:200]})
                        if len(matches) >= max_matches_per_function:
                            break
                if matches:
                    hits.append((STATUS_RANK.get(self.functions[address]['status'], 9), address, matches))
        hits.sort(key=lambda h: (h[0], h[1]))
        out = []
        for _, address, matches in hits[offset:offset + limit]:
            s = self.summary(address)
            s['matches'] = matches
            out.append(s)
        return len(hits), out

    # ----------------------------------------------------------------------------- graph
    def walk(self, address, direction, depth=1, limit=200):
        graph_out = (lambda a: [(t, 'call') for t in self.calls.get(a, [])] +
                     [(t, 'jump') for t in self.tails.get(a, [])])
        graph_in = lambda a: self.callers.get(a, [])
        step = graph_out if direction == 'callees' else graph_in
        seen = {address}
        out, queue, capped = [], deque([(address, 0)]), False
        while queue:
            node, d = queue.popleft()
            if d >= depth:
                continue
            for other, kind in sorted(step(node), key=lambda x: (STATUS_RANK.get(
                    self.functions.get(x[0], {}).get('status'), 9), x[0])):
                if other in seen:
                    continue
                seen.add(other)
                if len(out) >= limit:
                    capped = True
                    break
                entry = self.summary(other) if other in self.functions else {'address': hx(other)}
                entry.update({'depth': d + 1, 'via': hx(node), 'edge': kind})
                out.append(entry)
                queue.append((other, d + 1))
        return out, capped

    # ----------------------------------------------------------------------------- symbolize
    def symbolize(self, address, modules=None):
        """Function + offset inside the game exe, a known static object, or a module + offset."""
        vt = self.vtable_at(address)
        if vt:
            return vt
        static = self.statics_by_address.get(address)
        if static:
            return {'address': hx(address), 'module': 'FalloutNV.exe', 'kind': 'static',
                    'name': static.get('setting') or static['name'], 'staticKind': static['kind']}
        for m in modules or []:
            base, size = m['base'], m.get('size') or 0
            if base <= address < base + size and not m['name'].lower().startswith('falloutnv'):
                return {'address': hx(address), 'module': m['name'], 'kind': 'module',
                        'offset': '0x%X' % (address - base), 'path': m.get('path')}
        hit = self.containing(address)
        if hit:
            start, off = hit
            out = {'address': hx(address), 'module': 'FalloutNV.exe', 'kind': 'function',
                   'offset': '0x%X' % off}
            out.update(self.summary(start))
            out['functionAddress'] = out.pop('address')
            out['address'] = hx(address)
            return out
        if EXE_RANGE[0] <= address < EXE_RANGE[1]:
            i = bisect_right(self.addresses, address) - 1
            near = self.addresses[i] if i >= 0 else None
            out = {'address': hx(address), 'module': 'FalloutNV.exe', 'kind': 'unknown_exe_address'}
            if near is not None and address - near < 0x10000:
                out['after'] = self.summary(near, with_location=False)
                out['afterOffset'] = '0x%X' % (address - near)
            return out
        return {'address': hx(address), 'kind': 'unknown', 'note': 'outside FalloutNV.exe and the given modules'}

    def vtable_at(self, address):
        """A virtual table (or one of its slots) containing this address, from the exe's RTTI."""
        i = bisect_right(self.vtable_starts, address) - 1
        if i < 0:
            return None
        start = self.vtable_starts[i]
        cls, vt = self.vtables[start]
        if not start <= address < start + 4 * len(vt['slots']):
            return None
        slot, rem = divmod(address - start, 4)
        out = {'address': hx(address), 'module': 'FalloutNV.exe', 'kind': 'vtable', 'class': cls,
               'vtable': vt['address'], 'thisOffset': '0x%X' % vt['thisOffset']}
        if address != start or rem:
            out['slot'] = slot
            target = int(vt['slots'][slot], 16)
            if target in self.functions:
                out['slotFunction'] = self.summary(target, with_location=False)
        return out

    def rtti_class(self, name):
        key = self.rtti_lower.get(name.strip().lower())
        if not key:
            words = _words(name)
            close = sorted((k for k in self.rtti if all(w in k.lower() for w in words)), key=len)[:20]
            raise EngineMapError('NOT_FOUND', 'No RTTI class %r (the map needs --exe for RTTI)' % name,
                                 {'similar': close})
        return key, self.rtti[key]

    def descendants(self, cls, limit=300):
        out, queue, seen = [], deque(self.derived.get(cls, [])), set()
        while queue and len(out) < limit:
            c = queue.popleft()
            if c in seen:
                continue
            seen.add(c)
            out.append(c)
            queue.extend(self.derived.get(c, []))
        return out, bool(queue)

    # ----------------------------------------------------------------------------- classes, commands
    def class_layout(self, name):
        key = self.classes_lower.get(name.lower()) or self.classes_lower.get(name.replace(' ', '').lower())
        if not key:
            words = _words(name)
            close = sorted((k for k in self.classes if all(w in k.lower() for w in words)), key=len)[:20]
            raise EngineMapError('NOT_FOUND', 'No class layout for %r' % name, {'similar': close})
        return key, self.classes[key]

    def command(self, name):
        key = name.strip().lower()
        return [c for c in self.commands if c['name'].lower() == key or (c.get('short') or '').lower() == key]

    def find_commands(self, query, limit=25):
        words = _words(query)
        hits = [c for c in self.commands
                if all(w in (c['name'] + ' ' + (c.get('short') or '')).lower() for w in words)]
        hits.sort(key=lambda c: (len(c['name']), c['name']))
        return hits[:limit]

    def statics_find(self, query, kind=None, limit=25):
        words = _words(query)
        out = []
        for s in self.statics:
            if kind and s['kind'] != kind:
                continue
            text = ' '.join([s['name'], s.get('setting') or '']).lower()
            if all(w in text for w in words):
                out.append(s)
        out.sort(key=lambda s: (len(s['name']), s['name']))
        return out[:limit]


_cache = {}
_cache_lock = threading.Lock()


def load(path, decomp_root=None):
    """Cached EngineMap for a path, reloaded when the file changes."""
    path = Path(path)
    if not path.exists():
        raise EngineMapError('ENGINE_MAP_UNAVAILABLE', 'engine_map.json not found at %s' % path,
                             {'next': 'Generate it: python -m engine_map.generate --decomp <your fnv_decomp checkout> '
                                      '[--verified fnvsym/proofs/verified.csv] [--exe unpacked FalloutNV.exe] '
                                      '--out %s' % path})
    stamp = (str(path.resolve()), path.stat().st_mtime_ns, str(decomp_root or ''))
    with _cache_lock:
        cached = _cache.get('map')
        if cached and cached[0] == stamp:
            return cached[1]
    engine = EngineMap(path, decomp_root)
    with _cache_lock:
        _cache['map'] = (stamp, engine)
    return engine


def default_paths(project):
    """(engine_map path, decomp root or None) from the project's fnv settings, env vars and defaults."""
    fnv = getattr(project, 'fnv', None)
    engine_map = (fnv.engine_map if fnv and fnv.engine_map else None) or os.environ.get('FNV_ENGINE_MAP')
    decomp = (fnv.decomp_root if fnv and fnv.decomp_root else None) or os.environ.get('FNV_DECOMP')
    if not engine_map:
        engine_map = Path(project.state_dir) / 'engine_map.json'
    return Path(engine_map), Path(decomp) if decomp else None
