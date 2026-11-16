"""fnv_* MCP tools: engine knowledge from the decomp (offline) and the crash doctor.

Every tool here is read-only and offline: it reads engine_map.json, the decomp checkout and log files,
and never touches the game, the GECK or plugin files. Results use the bridge's envelope.
"""
import time
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import fnv_engine, fnv_symdb
from .fnv_engine import EngineMapError, hx, parse_address

SEARCH_SECONDS = 20     # fnv_func code and regex searches stop after this (release audit L4)

Status = Literal['proven', 'class_proven', 'probable', 'decomp', 'rejected', 'unnamed']
FunctionRef = Annotated[str, Field(min_length=1, max_length=300,
                                   description='Function address (0x00573090) or name (TESObjectREFR::GetDismembered, '
                                               'Cmd_IsLimbGone_Eval, FUN_005a3db0)')]

FNV_TOOLS = {
    'fnv_func': 'offline read: engine functions found by name words, address, or text/regex in their decomp code (find); the decomp source of one, placeholder calls annotated with proven names (code); direct callers or callees from the exe call graph, several levels (callers, callees); functions whose decomp code contains a string (strings)',
    'fnv_symbolize': 'offline read: addresses -> exe function + offset, static object, or module + offset',
    'fnv_symbols': 'offline read: compiler symbol database: source line per code address, named locals, exact type layouts',
    'fnv_name_status': 'offline read: is a name proven, by which evidence (fnvsym verified.csv)',
    'fnv_class': 'offline read: an engine class: fields with offsets, types and checks from the decomp headers (layout); base and derived classes from the game RTTI (tree); vtable slots with implementing functions and header declarations (vtable)',
    'fnv_command_info': 'offline read: console/script command parameters, handlers and what they call',
    'fnv_script_explain': 'offline read: what the commands in a script line really do in the engine (handler, calls, settings, code)',
    'fnv_global_find': 'offline read: settings and static objects with addresses (proven initializers)',
    'fnv_setting_info': 'offline read: one game/INI setting, its address and the functions that use it',
    'fnv_save_inspect': 'offline read: a .fos save decoded with the game save code: header, plugins, location, globals, changed forms',
    'fnv_save_diff': 'offline read: two saves compared: changed forms, quest stages, positions, globals',
}


class Module(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=260)
    base: str = Field(pattern=r'^(0x)?[0-9A-Fa-f]{1,8}$')
    size: str | None = Field(default=None, pattern=r'^(0x)?[0-9A-Fa-f]{1,8}$')


class FnvService:
    def __init__(self, project):
        self.project = project

    # ----------------------------------------------------------------------------- envelope
    def run(self, name, action):
        started = time.monotonic()
        operation_id = str(uuid.uuid4())
        try:
            data = action()
            return {'ok': True, 'operationId': operation_id, 'operation': name,
                    'projectId': self.project.project_id, 'outcome': 'unchanged', 'data': data, 'error': None,
                    'elapsedMs': round((time.monotonic() - started) * 1000)}
        except Exception as e:  # reported, never raised to the client
            code = getattr(e, 'code', 'INVALID_ARGUMENT' if isinstance(e, ValueError) else 'FNV_ERROR')
            return {'ok': False, 'operationId': operation_id, 'operation': name,
                    'projectId': self.project.project_id, 'outcome': 'failed_before_change',
                    'data': getattr(e, 'evidence', None),
                    'error': {'code': code, 'message': str(e), 'retrySafe': True,
                              'recovery': 'Read-only tool: fix the input or configuration and call again.'},
                    'elapsedMs': round((time.monotonic() - started) * 1000)}

    def engine(self):
        return fnv_engine.load(*fnv_engine.default_paths(self.project))

    # ----------------------------------------------------------------------------- engine knowledge
    def func_find(self, query, mode='auto', status=None, limit=25, offset=0):
        def action():
            e = self.engine()
            statuses = set(status) if status else None
            chosen = mode
            address = parse_address(query) if mode in ('auto', 'address') else None
            looks_like_address = (query.strip().lower().startswith('0x') or
                                  (len(query.strip()) >= 6 and any(ch.isdigit() for ch in query)))
            if mode == 'address' or (mode == 'auto' and address is not None and looks_like_address):
                if address is None:
                    raise ValueError('not an address: %r' % query)
                return {'mode': 'address', 'query': query, 'results': [e.symbolize(address)],
                        'identity': e.identity()}
            if mode in ('auto', 'name'):
                total, results = e.find_by_name(query, statuses, limit, offset)
                chosen = 'name'
                if total or mode == 'name':
                    hints = sorted({'fnv_setting_info("%s") lists the functions that read this setting' % r['name'][13:]
                                    for r in results if r['name'].startswith('Init_Setting_')})
                    return {'mode': chosen, 'query': query, 'total': total, 'offset': offset, 'results': results,
                            'note': None if total else 'No name matches; try mode="code"',
                            'hints': hints or None, 'identity': e.identity()}
            total, results = e.find_in_code(query, regex=(mode == 'regex'), statuses=statuses, limit=limit,
                                            offset=offset, timeout=SEARCH_SECONDS)
            return {'mode': 'regex' if mode == 'regex' else 'code', 'query': query, 'total': total,
                    'offset': offset, 'results': results, 'identity': e.identity()}
        return self.run('fnv_func', action)

    def func_code(self, function, max_lines=400):
        def action():
            e = self.engine()
            address = e.resolve(function)
            f = e.functions[address]
            out = e.summary(address)
            out.update({'signature': f.get('signature'), 'size': f.get('size'),
                        'callers': len(e.callers.get(address, [])),
                        'callees': len(e.calls.get(address, [])) + len(e.tails.get(address, []))})
            if f.get('proof'):
                out['proof'] = {k: f['proof'].get(k) for k in ('status', 'by') if f['proof'].get(k)}
            out.update(e.code(address, max_lines))
            out['identity'] = e.identity()
            return out
        return self.run('fnv_func', action)

    def func_graph(self, function, direction, depth=1, limit=100):
        def action():
            e = self.engine()
            address = e.resolve(function)
            results, capped = e.walk(address, direction, depth, limit)
            out = {'function': e.summary(address), 'direction': direction, 'depth': depth,
                   'count': len(results), 'capped': capped, 'results': results}
            if direction == 'callees' and e.functions[address].get('imports'):
                out['importCalls'] = e.functions[address]['imports']
            sig = e.functions[address].get('signature') or ''
            out['note'] = ('Direct calls only: calls through vtables and function pointers are not listed'
                           + ('; this function is virtual, so most callers reach it through its vtable'
                              if 'virtual' in sig else ''))
            out['identity'] = e.identity()
            return out
        return self.run('fnv_func_' + direction, action)

    def symbolize(self, addresses, modules=None):
        def action():
            e = self.engine()
            mods = []
            for m in modules or []:
                m = m if isinstance(m, dict) else m.model_dump()
                mods.append({'name': m['name'], 'base': int(m['base'], 16),
                             'size': int(m['size'], 16) if m.get('size') else 0})
            results = []
            for text in addresses:
                address = parse_address(text)
                results.append(e.symbolize(address, mods) if address is not None
                               else {'input': text, 'kind': 'invalid'})
            return {'results': results, 'identity': e.identity()}
        return self.run('fnv_symbolize', action)

    def name_status(self, name_or_address):
        def action():
            from .fnv_naming import fnvsym_root
            fnvsym_root()          # the naming tools are an optional extra and need the fnvsym checkout (D1)
            e = self.engine()
            address = parse_address(name_or_address)
            if address is not None and address in e.functions:
                hits = [address]
            else:
                key = name_or_address.strip().lower()
                hits = sorted(e.by_name.get(key, set()))
            results = []
            for a in hits[:50]:
                f = e.functions[a]
                r = e.summary(a)
                r['proven'] = f['status'] == 'proven'
                proof = f.get('proof') or {}
                if proof:
                    r['evidence'] = {'status': proof.get('status'), 'provenName': proof.get('name'),
                                     'identity': proof.get('identity'), 'behaviour': proof.get('behaviour'),
                                     'by': proof.get('by')}
                    r['sourceKind'] = _source_kind(proof)
                r['matchedAs'] = ('address' if address is not None and a == address else
                                  'proven name' if proof.get('name', '').lower() == name_or_address.strip().lower()
                                  else 'decomp name')
                results.append(r)
            verdict = ('proven' if any(r['proven'] and r['matchedAs'] != 'decomp name' for r in results)
                       else 'not proven' if results else 'unknown name')
            return {'query': name_or_address, 'verdict': verdict, 'results': results,
                    'rule': 'Only fnvsym-proven names are evidence; decomp names are machine-made until proven.',
                    'identity': e.identity()}
        return self.run('fnv_name_status', action)

    def class_layout(self, class_name, include_virtuals=False, compiler=False):
        def action():
            e = self.engine()
            key, layout = e.class_layout(class_name)
            fields = sorted(layout['fields'], key=lambda f: (f['offset'] is None, f['offset'] or 0))
            out = {'class': key, 'kind': layout.get('kind'), 'bases': layout.get('bases'),
                   'size': '0x%X' % layout['size'] if layout.get('size') is not None else None,
                   'sizeChecked': layout.get('sizeChecked'), 'definedIn': '%s:%s' % (layout['file'], layout['line']),
                   'origin': layout.get('origin'),
                   'fields': [{'offset': '0x%X' % f['offset'] if f['offset'] is not None else None,
                               'name': f['name'], 'type': f['type'],
                               **({'span': f['span']} if f.get('span') else {}),
                               **({'checked': True} if f.get('checked') else {}),
                               **({'inferred': True} if f.get('inferred') else {}),
                               **({'bits': f['bits']} if f.get('bits') else {})} for f in fields],
                   'notes': ['checked = the decomp build asserts this offset (FNV_STATIC_ASSERT)',
                             'inferred = no offset comment; placed after the previous field by type size',
                             'span = bytes to the next known offset, an upper bound, not the type size',
                             'Fields of base classes are not repeated; look up each base.',
                             'Layouts are still moving while fnv_decomp merges shared class headers; '
                             'regenerate the map after decomp updates.']}
            if layout.get('otherDefinitionCount'):
                out['otherDefinitions'] = layout['otherDefinitionCount']
            if include_virtuals:
                out['virtuals'] = layout.get('virtuals', [])
            db = e.symdb
            built = (db.layout(key) or db.layout(class_name)) if db else None
            if built and built['kind'] != 'enum':
                out['compilerLayout'] = {'size': '0x%X' % built['size'] if built['size'] is not None else None,
                                         'members': len(built['members']), 'variants': built['variants'],
                                         'conflicts': ['0x%X' % c for c in built['conflicts']]}
                if compiler:
                    out['compilerLayout']['memberList'] = [
                        {'offset': '0x%X' % m['offset'], 'name': m['name'], 'type': m['type'], 'size': m['size'],
                         **({'bitPos': m['bitPos'], 'bitLen': m['bitLen']} if m['bitLen'] else {})}
                        for m in built['members']]
                    out['compilerLayout']['bases'] = built['bases']
            out['identity'] = e.identity()
            return out
        return self.run('fnv_class', action)

    # ----------------------------------------------------------------------------- symbol database
    def symdb(self):
        """The attached symbol database; without one an error whose evidence holds the build command."""
        path = fnv_symdb.default_path(self.project)
        db = fnv_symdb.load(path, fnv_engine.default_paths(self.project)[1])
        if db is None:
            raise fnv_symdb.SymbolDbError('SYMDB_UNAVAILABLE', 'No symbol database at %s' % path,
                                          {'available': False, 'next': self._symdb_build_command(path)})
        return db

    def _symdb_build_command(self, path):
        decomp = fnv_engine.default_paths(self.project)[1]
        return ('python -m engine_map.symdb --decomp %s --image <your unpacked FalloutNV.exe> --out %s'
                % (decomp or '<decomp>', path))

    def _symdb_engine(self):
        try:
            return self.engine()
        except EngineMapError:
            return None

    @staticmethod
    def _symdb_function(db, engine, ref):
        """Function row of the database for an address or name; names go through the engine map first."""
        if engine is not None:
            try:
                row = db.function(engine.resolve(ref))
                if row:
                    return row
            except EngineMapError as e:
                if e.code == 'AMBIGUOUS':
                    raise
        return db.resolve(ref)

    @staticmethod
    def _source_text(engine, file, line):
        if engine is None:
            return None
        try:
            return engine._lines(file)[line - 1].strip()[:200]
        except (EngineMapError, IndexError, OSError):
            return None

    def symbols(self, action, function=None, address=None, file=None, line=None, name=None, limit=50, offset=0):
        def act():
            if action == 'status':
                return self._symbols_status()
            db = self.symdb()
            engine = self._symdb_engine()
            ident = db.identity(engine.decomp_root if engine is not None else None)
            if action in ('lines', 'locals'):
                ref = function or (address if action == 'locals' else None)
                if not ref:
                    raise ValueError('action=%s needs function (address or name)%s' % (
                        action, ' or address' if action == 'locals' else ''))
                f = self._symdb_function(db, engine, ref)
                head = {'address': '0x%08X' % f['addr'], 'name': f['name'] or f['decorated'], 'size': f['size'],
                        'verified': bool(f['verified']), 'hasDebug': bool(f['has_debug'])}
                if action == 'lines':
                    return self._symbols_lines(db, engine, f, head, limit, offset, ident)
                return self._symbols_locals(db, f, head, address, ident)
            if action == 'where':
                if not file or line is None:
                    raise ValueError('action=where needs file (a path or its ending, e.g. playercharacter.cpp) '
                                     'and line')
                return self._symbols_where(db, engine, file, line, ident)
            if action == 'type':
                if not name:
                    raise ValueError('action=type needs name (a class, struct, union or enum)')
                return self._symbols_type(db, name, limit, offset, ident)
            raise ValueError('unknown action %r: status, lines, locals, where or type' % action)
        return self.run('fnv_symbols', act)

    def _symbols_status(self):
        path = fnv_symdb.default_path(self.project)
        build = self._symdb_build_command(path)
        try:
            db = fnv_symdb.load(path, fnv_engine.default_paths(self.project)[1])
        except fnv_symdb.SymbolDbError as e:
            return {'available': False, 'verdict': str(e), 'path': str(path), 'next': build}
        if db is None:
            return {'available': False, 'verdict': 'No symbol database at %s' % path, 'path': str(path),
                    'next': build}
        engine = self._symdb_engine()
        ident = db.identity(engine.decomp_root if engine is not None else None)
        cov = db.coverage()
        verdict = ('Symbol database present: %d functions, %d with source lines, %d verified; %d of %d units not ok'
                   % (cov['functions'], cov['functionsWithLines'], cov['functionsVerified'], cov['unitsNotOk'],
                      cov['units']))
        out = {'available': True, 'verdict': verdict, 'stale': ident['stale'], 'coverage': cov, 'identity': ident}
        if ident['stale']:
            out['verdict'] += '; STALE: the decomp has moved on since it was built'
            out['next'] = 'Rebuild it: ' + build
        return out

    def _symbols_lines(self, db, engine, f, head, limit, offset, ident):
        rows = db.lines_of(f['addr'])
        files = sorted({r[1] for r in rows})
        out_rows = []
        for off, path, number in rows[offset:offset + limit]:
            row = {'address': '0x%08X' % (f['addr'] + off), 'offset': '0x%X' % off, 'line': number,
                   'text': self._source_text(engine, path, number)}
            if len(files) > 1:
                row['file'] = path
            out_rows.append(row)
        if rows:
            verdict = '%s has %d line rows in %s' % (head['name'], len(rows), files[0] if len(files) == 1
                                                     else '%d files' % len(files))
        else:
            verdict = '%s has no line table: %s' % (head['name'], 'the compiler found no debug info for it'
                                                    if not f['has_debug'] else 'it was not verified against the exe')
        return {'verdict': verdict, 'function': head, 'file': files[0] if len(files) == 1 else None,
                'files': files if len(files) > 1 else None, 'total': len(rows), 'offset': offset, 'limit': limit,
                'rows': out_rows, 'identity': ident}

    def _symbols_locals(self, db, f, head, address, ident):
        at = target = None
        if address is not None:
            target = parse_address(address)
            if target is None:
                raise ValueError('not an address: %r' % address)
            if not f['addr'] <= target < f['addr'] + max(f['size'], 1):
                raise ValueError('address %s is not inside %s (%s..%s)' % (
                    hx(target), head['name'], hx(f['addr']), hx(f['addr'] + f['size'])))
            at = {'address': hx(target), 'offset': '0x%X' % (target - f['addr'])}
        found = db.locals_of(f['addr'], target - f['addr'] if at else None)
        params = sum(1 for item in found if item['param'])
        if found:
            verdict = '%s has %d parameters and %d other locals%s' % (
                head['name'], params, len(found) - params, ' in scope at %s' % at['address'] if at else '')
        else:
            verdict = '%s has no named locals in the database' % head['name']
        return {'verdict': verdict, 'function': dict(head, debugStart=f.get('debug_start'),
                                                      frameBytes=f.get('frame_bytes')), 'at': at,
                'locals': found, 'note': 'location ebp+N is a stack slot relative to the frame pointer: '
                                         'positive = parameter, negative = local.', 'identity': ident}

    def _symbols_where(self, db, engine, file, line, ident):
        files = db.files_matching(file)
        matches = db.where(file, line)
        for m in matches:
            m['address'] = '0x%08X' % m['address']
            m['function'] = '0x%08X' % m['function']
            m['offset'] = '0x%X' % m['offset']
        text = self._source_text(engine, files[0], line) if len(files) == 1 else None
        shown = files[0] if len(files) == 1 else file
        if matches:
            functions = {m['function'] for m in matches}
            verdict = '%s:%d is code at %d address%s in %d function%s' % (
                shown, line, len(matches), '' if len(matches) == 1 else 'es', len(functions),
                '' if len(functions) == 1 else 's')
        elif not files:
            verdict = 'No file ending in %r is in the symbol database' % file
        else:
            verdict = ('No code is attributed to line %d of %s (comments, declarations and optimized-away lines '
                       'have none; try a nearby line)' % (line, shown))
        out = {'verdict': verdict, 'file': files[0] if len(files) == 1 else None, 'line': line, 'text': text,
               'matches': matches[:200], 'matchCount': len(matches), 'identity': ident}
        if len(files) > 1:
            out['candidates'] = files[:25]
            out['note'] = 'Several files end with %r; pass more of the path to pick one.' % file
        return out

    def _symbols_type(self, db, name, limit, offset, ident):
        t = db.layout(name)
        if t is None:
            raise fnv_symdb.SymbolDbError('NOT_FOUND', 'No class, struct, union or enum named %r in the symbol '
                                          'database' % name)
        size = '0x%X' % t['size'] if t['size'] is not None else None
        if t['kind'] == 'enum':
            items = t['enumerators']
            return {'verdict': '%s is an enum with %d enumerators' % (t['name'], len(items)), 'name': t['name'],
                    'kind': 'enum', 'size': size, 'total': len(items), 'offset': offset, 'limit': limit,
                    'enumerators': items[offset:offset + limit], 'variants': t['variants'], 'identity': ident}
        extra = ' (also seen with size %s)' % ', '.join('0x%X' % c for c in t['conflicts']) if t['conflicts'] else ''
        members, methods = t['members'], t['methods']
        return {'verdict': '%s is a %s of size %s with %d members, %d bases, %d methods; %d definition%s%s' % (
                    t['name'], t['kind'], size, len(members), len(t['bases']), len(methods), t['variants'],
                    '' if t['variants'] == 1 else 's', extra),
                'name': t['name'], 'kind': t['kind'], 'size': size, 'variants': t['variants'],
                'conflicts': ['0x%X' % c for c in t['conflicts']], 'seen': t['seen'],
                'hasVtable': bool(t['attrs'].get('vtable')), 'bases': t['bases'],
                'total': len(members), 'offset': offset, 'limit': limit,
                'members': [dict(m, offset='0x%X' % m['offset']) for m in members[offset:offset + limit]],
                'methodsTotal': len(methods), 'methods': methods[:limit], 'identity': ident,
                'note': 'Compiler-exact layout from the debug info of the units that define this type; the best '
                        'definition (most members) is shown.'}

    def class_vtable(self, class_name, limit=80, offset=0):
        def action():
            e = self.engine()
            rtti_key = e.rtti_lower.get(class_name.strip().lower())
            layout_key = e.classes_lower.get(class_name.strip().lower())
            if not rtti_key and not layout_key:
                e.rtti_class(class_name)   # raises NOT_FOUND with similar names
            chain = [rtti_key or layout_key] + (e.rtti[rtti_key]['bases'] if rtti_key else [])
            declared = {}
            for name in chain:
                layout = e.classes.get(name) or e.classes.get(e.classes_lower.get(name.lower(), ''))
                for v in (layout or {}).get('virtuals', []):
                    if v['slot'] is not None and v['slot'] not in declared:
                        declared[v['slot']] = {'declaration': v['declaration'], 'declaredIn': name,
                                               'line': '%s:%s' % (layout['file'], v['line'])}
            out = {'class': rtti_key or layout_key, 'identity': e.identity()}
            if rtti_key:
                tables = e.rtti[rtti_key]['vtables']
                primary = next((t for t in tables if t['thisOffset'] == 0), tables[0] if tables else None)
                slots = []
                for i, text in enumerate(primary['slots'] if primary else []):
                    target = parse_address(text)
                    entry = {'slot': i, 'function': e.summary(target) if target in e.functions else {'address': text}}
                    if i in declared:
                        entry['declared'] = declared[i]
                    slots.append(entry)
                out.update({'vtable': primary['address'] if primary else None, 'slots': slots,
                            'otherVtables': [{'address': t['address'], 'thisOffset': '0x%X' % t['thisOffset'],
                                              'slots': len(t['slots'])} for t in tables if t is not primary],
                            'note': 'Slots and implementing functions read from the exe\'s RTTI and vtables; '
                                    'declarations from the decomp headers of this class or its bases. The '
                                    'slot count is read until the next non-code value, so it can run long.'})
            else:
                out.update({'slots': [dict(v, slot=k) for k, v in sorted(declared.items())],
                            'note': 'No RTTI for this class in the map (generate with --exe): header '
                                    'declarations only.'})
            slots = out.get('slots') or []
            out.update(total=len(slots), offset=offset, slots=slots[offset:offset + limit])
            if offset + limit < len(slots):
                out['more'] = 'Slots %d to %d of %d shown; call again with offset=%d for the next ones.' % (
                    offset, offset + limit - 1, len(slots), offset + limit)
            return out
        return self.run('fnv_class', action)

    def class_tree(self, class_name, limit=200):
        def action():
            e = self.engine()
            key, info = e.rtti_class(class_name)
            descendants, capped = e.descendants(key, limit)
            return {'class': key, 'directBases': info.get('directBases'), 'allBases': info.get('bases'),
                    'directDerived': sorted(e.derived.get(key, [])), 'allDerived': descendants,
                    'allDerivedCapped': capped, 'hasLayout': key.lower() in e.classes_lower,
                    'vtables': [{'address': t['address'], 'thisOffset': '0x%X' % t['thisOffset']}
                                for t in info.get('vtables', [])],
                    'note': "From the game's RTTI (base order as MSVC records it).", 'identity': e.identity()}
        return self.run('fnv_class', action)

    def command_info(self, name):
        def action():
            e = self.engine()
            matches = e.command(name)
            if not matches:
                similar = e.find_commands(name)
                raise EngineMapError('NOT_FOUND', 'No command named %r' % name,
                                     {'similar': [c['name'] for c in similar]})
            out = []
            for c in matches:
                c = dict(c)
                handlers = {}
                for role, text in (c.get('handlers') or {}).items():
                    address = parse_address(text)
                    if address in e.functions:
                        h = e.summary(address)
                        calls, capped = e.walk(address, 'callees', 1, 40)
                        h['calls'] = calls
                        h['callsCapped'] = capped
                        handlers[role] = h
                    else:
                        handlers[role] = {'address': text}
                if handlers:
                    c['handlers'] = handlers
                elif c.get('handler'):
                    c['handlerNote'] = ('Defined in the %s source (%s); its address depends on the plugin build'
                                        % (c['source'], c.get('definedIn')))
                out.append(c)
            return {'query': name, 'results': out, 'identity': e.identity()}
        return self.run('fnv_command_info', action)

    def script_explain(self, source, depth=2, code_lines=60):
        """What the commands in a script line (or a whole script, or one command name) really do in the engine."""
        import re as _re

        def action():
            e = self.engine()
            by_key = {}
            for c in e.commands:
                for key in (c['name'], c.get('short')):
                    if key:
                        by_key.setdefault(key.lower(), []).append(c)
            settings_by_fn = {}
            for s in e.statics:
                if s.get('kind') == 'Setting':
                    for a in s.get('usedBy') or []:
                        settings_by_fn.setdefault(parse_address(a), []).append(s.get('setting') or s['name'])
            words, seen = [], set()
            for n, raw in enumerate(source.replace('\r', '').split('\n'), 1):
                text = raw.split(';', 1)[0]
                text = _re.sub(r'"[^"]*"', ' ', text)
                for token in _re.findall(r'[A-Za-z_][\w.]*', text):
                    name = token.rpartition('.')[2].lower()
                    if name in by_key and name not in seen and name not in ('set', 'to', 'if', 'else', 'endif', 'begin',
                                                                            'end', 'return', 'let', 'scn'):
                        seen.add(name)
                        words.append((n, token, name))
            if not words:
                raise EngineMapError('NOT_FOUND', 'No script command found in the text',
                                     {'similar': [c['name'] for c in e.find_commands(source.strip()[:60])][:15]})
            out = []
            for line, token, name in words[:12]:
                for c in by_key[name][:2]:
                    item = {k: c.get(k) for k in ('name', 'short', 'source', 'opcode', 'params', 'returnType', 'help',
                                                  'condition', 'definedIn') if c.get(k) is not None}
                    item['line'] = line
                    if token.count('.'):
                        item['calledOn'] = token.rpartition('.')[0]
                    handlers = {}
                    for role, addr_text in (c.get('handlers') or {}).items():
                        address = parse_address(addr_text)
                        if address not in e.functions:
                            handlers[role] = {'address': addr_text}
                            continue
                        h = e.summary(address)
                        if role == 'parse' and (h.get('name') or '').endswith('StandardCompile'):
                            h['note'] = "the game's standard argument parser (shared by most commands)"
                            handlers[role] = h
                            continue
                        calls, capped = e.walk(address, 'callees', depth, 200)
                        named = [x for x in calls if x.get('status') in ('proven', 'class_proven', 'probable')]
                        h['engineCalls'] = named[:60]
                        h['engineCallsTotal'] = len(calls)
                        used = set(settings_by_fn.get(address, []))
                        for x in calls:
                            if x.get('depth', 1) > 1:
                                continue
                            a = parse_address(x.get('address', ''))
                            used.update(settings_by_fn.get(a, []))
                        h['settingsRead'] = sorted(used)[:40]
                        code = e.code(address, max_lines=code_lines)
                        if isinstance(code, dict):
                            h['code'] = code.get('code')
                            h['codeTruncated'] = code.get('truncated')
                        handlers[role] = h
                    if handlers:
                        item['handlers'] = handlers
                        item['summary'] = _command_summary(item)
                    elif c.get('source') != 'vanilla':
                        item['handlerNote'] = ('Implemented in the %s plugin (%s), not in the game exe; see that '
                                               'source for its code' % (c.get('source'), c.get('definedIn') or '?'))
                    out.append(item)
            return {'commands': out, 'more': max(0, len(words) - 12), 'identity': e.identity(),
                    'note': 'execute = what the command does when a script or the console runs it; eval = how it is '
                            'computed as a condition (dialogue, packages, perks); parse = how its arguments compile. '
                            'engineCalls are the named functions reached from the handler (depth %d); settingsRead are '
                            'game settings the handler and its direct callees read.'
                            % depth}
        return self.run('fnv_script_explain', action)

    def global_find(self, query, kind=None, limit=25):
        def action():
            e = self.engine()
            return {'query': query, 'results': e.statics_find(query, kind, limit),
                    'note': 'From proven static initializers (settings, NiRTTI, Havok class records). '
                            'Singleton pointers (player, data handler) are not in the map yet.',
                    'identity': e.identity()}
        return self.run('fnv_global_find', action)

    def setting_info(self, name, limit=40):
        def action():
            e = self.engine()
            key = name.strip().lower()
            hits = [s for s in e.statics if s['kind'] == 'Setting' and
                    (s['name'].lower() == key or (s.get('setting') or '').lower() == key or
                     (s.get('setting') or '').split(':')[0].lower() == key)]
            if not hits:
                raise EngineMapError('NOT_FOUND', 'No proven setting named %r' % name,
                                     {'similar': [s.get('setting') or s['name'] for s in e.statics_find(name, 'Setting', 15)]})
            results = []
            for s in hits:
                r = dict(s)
                if s.get('usedBy') is not None:
                    users = [parse_address(a) for a in s['usedBy']]
                    r['usedBy'] = [e.summary(a) for a in users[:limit] if a in e.functions]
                    r['usedByTotal'] = len(users)
                    r['usedByNote'] = 'functions whose code references the setting object (read from the exe)'
                elif s.get('address'):
                    ident = 'g_%08x' % int(s['address'], 16)
                    total, users = e.find_in_code(r'\b%s\b' % ident, regex=True, limit=limit)
                    r['usedBy'] = users
                    r['usedByTotal'] = total
                    r['usedByNote'] = 'functions whose decomp code names %s; the static initializer and its atexit are included' % ident
                results.append(r)
            return {'query': name, 'results': results, 'identity': e.identity()}
        return self.run('fnv_setting_info', action)

    def string_xrefs(self, text, limit=25, offset=0):
        def action():
            e = self.engine()
            total, results = e.find_in_code('"' + text, regex=False, limit=limit, offset=offset)
            if not total:
                total, results = e.find_in_code(text, regex=False, limit=limit, offset=offset)
            return {'query': text, 'total': total, 'results': results,
                    'note': 'Searches the decomp source text, so strings the decomp spells as data symbols are missed.',
                    'identity': e.identity()}
        return self.run('fnv_func', action)

    def _read_path(self, path, what='path'):
        """A caller's path, resolved, when it lies inside the project's read roots (safe_paths.read_roots); None stays
        None. A network path or one outside the roots is refused (H5)."""
        from .safe_paths import inside, read_roots
        return None if not path else inside(path, read_roots(self.project), what)

    # ----------------------------------------------------------------------------- crashes
    def crash_latest(self, path=None, max_frames=40):
        from . import fnv_crash

        def action():
            log = fnv_crash.find_latest(self.project, self._read_path(path))
            parsed = fnv_crash.parse_file(log)
            return fnv_crash.summarise(parsed, max_frames)
        return self.run('fnv_crash', action)

    def crash_explain(self, path=None, max_frames=40):
        from . import fnv_crash

        def action():
            log = fnv_crash.find_latest(self.project, self._read_path(path))
            parsed = fnv_crash.parse_file(log)
            try:
                engine = self.engine()
            except EngineMapError as e:
                engine = None
                unavailable = {'code': e.code, 'message': str(e)}
            else:
                unavailable = None
            out = fnv_crash.explain(parsed, engine, self.project, max_frames)
            if unavailable:
                out['engineMap'] = unavailable
            # The engine companion's own record of the fault (fnv_crash context), matched by address and time.
            from .fnv_deep import match_context
            site = (out.get('crashSite') or {}).get('absolute')
            context = match_context(site, out.get('modified')) if site else None
            if context:
                calls = context.get('hookedCalls') or []
                out['companionContext'] = {'file': context['file'], 'threadId': context.get('threadId'),
                                           'mainThread': context.get('mainThread'), 'hookedCalls': calls,
                                           'consoleLine': context.get('consoleLine') or None,
                                           'request': context.get('request') or None,
                                           'recentEvents': context.get('recentEvents'),
                                           'access': context.get('access'), 'accessAddress': context.get('accessAddress')}
                if calls:
                    out['runningScriptFromCompanion'] = {'script': calls[0].get('thisFormId'),
                                                         'on': calls[0].get('arg0FormId'), 'via': calls[0].get('label')}
            return out
        return self.run('fnv_crash', action)


    # ----------------------------------------------------------------------------- saves
    def saves_dir(self):
        from pathlib import Path
        fnv = getattr(self.project, 'fnv', None)
        if not fnv or not fnv.mo2_root or not fnv.mo2_profile:
            raise ValueError('Set fnv.mo2_root and fnv.mo2_profile in the project file, or pass a full path')
        return Path(fnv.mo2_root) / 'profiles' / fnv.mo2_profile / 'saves'

    def _save_path(self, name):
        from . import fnv_save
        from pathlib import Path
        if Path(name).is_absolute() or name[:1] in '\\/':
            path = self._read_path(name, 'save')
            return fnv_save.resolve_path(str(path), path.parent)
        return self._read_path(fnv_save.resolve_path(name, self.saves_dir()), 'save')

    def _namer(self):
        from . import fnv_save
        from .fnv_data import DataTools
        try:
            return fnv_save.Namer(DataTools(self.project))
        except Exception:
            return None

    def save_inspect(self, save, types=None, forms=None, limit=100, offset=0, screenshot=False):
        from . import fnv_save

        def action():
            path = self._save_path(save)
            shot = None
            if screenshot:
                from pathlib import Path
                folder = Path(self.project.state_dir) / 'fnv' / 'screenshots'
                folder.mkdir(parents=True, exist_ok=True)
                shot = folder / (path.stem + '.save.png')
            return fnv_save.inspect(path, types, forms, limit, offset, shot, namer=self._namer())
        return self.run('fnv_save_inspect', action)

    def save_diff(self, before, after, types=None, forms=None, limit=200):
        from . import fnv_save

        def action():
            return fnv_save.diff(self._save_path(before), self._save_path(after), types, forms, limit,
                                 namer=self._namer())
        return self.run('fnv_save_diff', action)


def _source_kind(proof):
    by = proof.get('by') or ''
    identity = proof.get('identity') or ''
    if by.startswith('mech:'):
        return 'mechanical rule (%s)' % by[5:]
    if identity.startswith('C:'):
        return 'community source (%s)' % identity[2:].strip().split(';')[0][:120]
    if identity.startswith('G:'):
        return "the game's own data (%s)" % identity[2:].strip()[:120]
    if identity.startswith('R:'):
        return 'RTTI / vtable evidence'
    if identity.startswith('S:'):
        return 'static structure (%s)' % identity[2:].strip()[:120]
    return by or 'unknown'


def _command_summary(item):
    """One plain line from a command's handler names: the main engine functions it reaches."""
    parts = []
    for role, h in item.get('handlers', {}).items():
        if h.get('note'):
            continue
        calls = [c.get('name') for c in h.get('engineCalls', []) if c.get('depth', 1) == 1 and c.get('name')]
        calls = [c for c in calls if not c.startswith(('FUN_', 'Script::', 'ExtractArgs'))][:6]
        parts.append('%s %s%s' % (role, h.get('name') or h.get('address'),
                                  (' -> ' + ', '.join(calls)) if calls else ''))
    return '; '.join(parts)


def register(server, project, observe, result_type):
    """Add the fnv_* tools to the bridge's MCP server."""
    import json
    from functools import partial

    import anyio
    from mcp.types import CallToolResult, TextContent

    service = FnvService(project)

    async def invoke(fn, *args, **kwargs):
        result = await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(result))],
                              structured_content=result, is_error=not result.get('ok', True))

    Limit = Annotated[int, Field(ge=1, le=200)]
    Offset = Annotated[int, Field(ge=0, le=100000)]
    Depth = Annotated[int, Field(ge=1, le=4)]

    @server.tool(annotations=observe)
    async def fnv_func(query: Annotated[str, Field(min_length=1, max_length=300, description=(
            'find: name words, an address, code text or a regex. code, callers, callees: a function address '
            '(0x00573090) or name (TESObjectREFR::GetDismembered, Cmd_IsLimbGone_Eval, FUN_005a3db0). strings: the '
            'text to look for'))],
                       action: Literal['find', 'code', 'callers', 'callees', 'strings'] = 'find',
                       mode: Literal['auto', 'name', 'code', 'regex', 'address'] = 'auto',
                       status: list[Status] | None = None, limit: Limit = 25,
                       offset: Offset = 0, depth: Depth = 1,
                       max_lines: Annotated[int, Field(ge=10, le=3000)] = 400) -> result_type:
        """Fallout: New Vegas 1.4.0.525 engine functions. find (default): mode=name matches all words of the query in
        the function's names (CamelCase aware: "limb damage" finds ApplyLimbDamage); mode=code searches the decomp
        source text of each function (case-insensitive), regex likewise with a regular expression; address
        symbolizes; auto picks address, then name, then code. Proven names rank first. Each result gives address,
        name, status (proven / class_proven / probable / decomp / unnamed) and file:line. code: the decompiled C++
        of one function from the decomp checkout, with its signature, proof status and call counts; calls to
        placeholder functions (FUN_xxxxxxxx) are annotated with their proven names. callers: the functions that call
        it directly (call graph read from the game exe), up to `depth` levels; calls through vtables or function
        pointers are not visible. callees: the functions it calls directly (and Windows/DLL imports it calls), up to
        `depth` levels. strings: functions whose decompiled code contains the query text ("Pip-Boy", an INI key, an
        error text)."""
        if action == 'code':
            return await invoke(service.func_code, query, max_lines)
        if action in ('callers', 'callees'):
            return await invoke(service.func_graph, query, action, depth, limit)
        if action == 'strings':
            return await invoke(service.string_xrefs, query, limit, offset)
        return await invoke(service.func_find, query, mode, status, limit, offset)

    @server.tool(annotations=observe)
    async def fnv_symbolize(addresses: Annotated[list[str], Field(min_length=1, max_length=200)],
                            modules: Annotated[list[Module], Field(max_length=400)] | None = None) -> result_type:
        """Turn addresses into names: inside FalloutNV.exe -> function + offset (or a known setting/static
        object); inside a listed module (name, base, size, as in a crash log) -> module + offset."""
        return await invoke(service.symbolize, addresses, modules)

    @server.tool(annotations=observe)
    async def fnv_name_status(name_or_address: Annotated[str, Field(min_length=1, max_length=300)]) -> result_type:
        """Is this engine function name proven? Returns each matching function with its proof status and the
        evidence (community source such as CommonLibNV/JohnnyGuitar/xNVSE, the game's own tables/strings,
        RTTI, or a mechanical rule) from fnvsym's verified.csv."""
        return await invoke(service.name_status, name_or_address)

    @server.tool(annotations=observe)
    async def fnv_class(class_name: Annotated[str, Field(min_length=1, max_length=200)],
                        action: Literal['layout', 'tree', 'vtable'] = 'layout',
                        include_virtuals: bool = False, compiler: bool = False,
                        limit: Limit | None = None, offset: Offset = 0) -> result_type:
        """An engine class (Actor, TESObjectREFR, BaseProcess, ...). layout (default): fields with offsets, types and
        whether the decomp build checks each offset; class size and base classes. When the symbol database knows the
        class, compilerLayout gives the compiler's own size, member count, number of different definitions and other
        sizes seen; compiler=true adds the compiler's full member list (see fnv_symbols action=type). tree: base
        classes and derived classes from the game's RTTI (limit default 200). vtable: each slot's implementing
        function (name, proof status) read from the game's RTTI, with the declaration from the decomp headers where
        one exists; paged, `limit` slots (default 80) from slot `offset`; `total` and `more` say how to get the
        rest."""
        if action == 'tree':
            return await invoke(service.class_tree, class_name, 200 if limit is None else limit)
        if action == 'vtable':
            return await invoke(service.class_vtable, class_name, 80 if limit is None else limit, offset)
        return await invoke(service.class_layout, class_name, include_virtuals, compiler)

    @server.tool(annotations=observe)
    async def fnv_symbols(action: Literal['status', 'lines', 'locals', 'where', 'type'],
                          function: Annotated[str | None, Field(max_length=300, description=(
                              'lines/locals: function address (0x00573090) or name'))] = None,
                          address: Annotated[str | None, Field(max_length=20, description=(
                              'locals: an address inside the function: only the locals in scope there'))] = None,
                          file: Annotated[str | None, Field(max_length=400, description=(
                              'where: source file path or its ending, e.g. playercharacter.cpp'))] = None,
                          line: Annotated[int | None, Field(ge=1, le=10000000)] = None,
                          name: Annotated[str | None, Field(max_length=200, description=(
                              'type: class, struct, union or enum name'))] = None,
                          limit: Limit = 50, offset: Offset = 0) -> result_type:
        """The compiler's symbol database for the decomp functions (built by engine_map.symdb from the decomp and
        the game exe). Use it for the exact source line of a code address, the names of a function's parameters
        and locals, or the real size and member offsets of a type. action=status: is the database there, how
        complete, is it stale (when it is not built, `next` gives the build command). action=lines: function ->
        its line table (address, offset, line, source text). action=locals: function (and optional address) ->
        parameters and locals with stack location (ebp+8 = first parameter), type and size. action=where: file +
        line -> the code addresses that line compiled to. action=type: name -> members with offsets, bases,
        methods with vtable slots, number of different definitions and conflicting sizes (enums: enumerators).
        The main answer is in `verdict`; evidence follows; `identity` says which build the answer is from."""
        return await invoke(service.symbols, action, function, address, file, line, name, limit, offset)

    @server.tool(annotations=observe)
    async def fnv_command_info(name: Annotated[str, Field(min_length=1, max_length=100)]) -> result_type:
        """A console/script command by name or short name (vanilla, xNVSE, JIP LN...): parameters, return type,
        help text; for vanilla commands the handler functions and what each calls."""
        return await invoke(service.command_info, name)

    @server.tool(annotations=observe)
    async def fnv_script_explain(source: Annotated[str, Field(min_length=1, max_length=20000, description=(
            'A script line, a whole script, or a command name'))],
                                 depth: Annotated[int, Field(ge=1, le=3)] = 2,
                                 code_lines: Annotated[int, Field(ge=0, le=400)] = 60) -> result_type:
        """What each script command in the text really does inside the game: its parameters, its handlers (execute,
        condition eval, argument parser) as engine functions with proof status, the named engine functions they
        reach, the game settings they read and the start of their decompiled code. For xNVSE / JIP LN commands it
        says which plugin implements them."""
        return await invoke(service.script_explain, source, depth, code_lines)

    @server.tool(annotations=observe)
    async def fnv_global_find(query: Annotated[str, Field(min_length=1, max_length=200)],
                              kind: Literal['Setting', 'RTTI', 'hkClass', 'hkTypeInfo'] | None = None,
                              limit: Limit = 25) -> result_type:
        """Find game settings and static objects (NiRTTI, Havok class records) with their addresses."""
        return await invoke(service.global_find, query, kind, limit)

    @server.tool(annotations=observe)
    async def fnv_setting_info(name: Annotated[str, Field(min_length=1, max_length=200)]) -> result_type:
        """A game or INI setting (fCombatDistance, bAlwaysActive:General): its address and the engine
        functions that use it."""
        return await invoke(service.setting_info, name)

    SaveName = Annotated[str, Field(min_length=1, max_length=1000,
                                    description='Save file: full path, file name in the dev profile saves folder, '
                                                'name without .fos, or a test-save name')]
    TypeList = list[Annotated[str, Field(pattern=r'^[A-Za-z_]{4}$')]] | None
    FormList = list[Annotated[str, Field(pattern=r'^(0x)?[0-9A-Fa-f]{1,8}$')]] | None

    @server.tool(annotations=observe)
    async def fnv_save_inspect(save: SaveName, types: TypeList = None, forms: FormList = None,
                               limit: Annotated[int, Field(ge=1, le=500)] = 100, offset: Offset = 0,
                               screenshot: bool = False) -> result_type:
        """Read a .fos save the way the game's save code writes it: header (name, level, location, play time),
        plugin list, player location, every global variable, and the changed-form table, each form with its
        change flags by name (moved, inventory, stages...). Reference positions and quest bodies (stages done,
        log dates, objectives, script variables) are decoded. Filter by record types (QUST, ACHR, REFR...) or
        form ids. screenshot=true writes the save's thumbnail as PNG."""
        return await invoke(service.save_inspect, save, types, forms, limit, offset, screenshot)

    @server.tool(annotations=observe)
    async def fnv_save_diff(before: SaveName, after: SaveName, types: TypeList = None, forms: FormList = None,
                            limit: Annotated[int, Field(ge=1, le=1000)] = 200) -> result_type:
        """Compare two saves exactly: header, plugins, player location, global variables that changed, and
        changed forms added, removed or different (quest stages and objectives and reference positions field by
        field, other bodies byte for byte). Shows what a test, a mod or a play session really changed in the
        saved game. Narrow with types (e.g. ["QUST"]) or forms."""
        return await invoke(service.save_diff, before, after, types, forms, limit)

    # Live tools (game control, tests, inspection, full control) on the engine companion.
    from mcp.types import ToolAnnotations

    from . import fnv_live_tools
    act = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
    control = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
    live_read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False)
    fnv_live_tools.register(server, project, (live_read, act, control), result_type, offline=service)
    return service
