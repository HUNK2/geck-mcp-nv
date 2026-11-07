"""fnv_* MCP tools: engine knowledge from the decomp (offline) and the crash doctor.

Every tool here is read-only and offline: it reads engine_map.json, the decomp checkout and log files,
and never touches the game, the GECK or plugin files. Results use the bridge's envelope.
"""
import time
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import fnv_engine
from .fnv_engine import EngineMapError, hx, parse_address

Status = Literal['proven', 'class_proven', 'probable', 'decomp', 'rejected', 'unnamed']
FunctionRef = Annotated[str, Field(min_length=1, max_length=300,
                                   description='Function address (0x00573090) or name (TESObjectREFR::GetDismembered, '
                                               'Cmd_IsLimbGone_Eval, FUN_005a3db0)')]

FNV_TOOLS = {
    'fnv_func_find': 'offline read: functions by name words, address, or text/regex in their decomp code',
    'fnv_func_code': 'offline read: decomp source of one function, placeholder calls annotated with proven names',
    'fnv_func_callers': 'offline read: direct callers (call graph from the exe), several levels',
    'fnv_func_callees': 'offline read: direct callees and import calls, several levels',
    'fnv_symbolize': 'offline read: addresses -> exe function + offset, static object, or module + offset',
    'fnv_name_status': 'offline read: is a name proven, by which evidence (fnvsym verified.csv)',
    'fnv_class_layout': 'offline read: fields with offsets, types and checks from the decomp headers',
    'fnv_class_vtable': 'offline read: vtable slots with implementing functions (exe RTTI) and header declarations',
    'fnv_class_tree': 'offline read: base and derived classes from the game RTTI',
    'fnv_command_info': 'offline read: console/script command parameters, handlers and what they call',
    'fnv_global_find': 'offline read: settings and static objects with addresses (proven initializers)',
    'fnv_setting_info': 'offline read: one game/INI setting, its address and the functions that use it',
    'fnv_string_xrefs': 'offline read: functions whose decomp code contains a string',
    'fnv_crash_latest': 'offline read: newest crash log parsed (exception, registers, call stack, modules)',
    'fnv_crash_explain': 'offline read: crash log stack in decomp names and DLL offsets, forms involved, likely culprit',
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
        path, decomp = fnv_engine.default_paths(self.project)
        return fnv_engine.load(path, decomp)

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
                                            offset=offset)
            return {'mode': 'regex' if mode == 'regex' else 'code', 'query': query, 'total': total,
                    'offset': offset, 'results': results, 'identity': e.identity()}
        return self.run('fnv_func_find', action)

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
        return self.run('fnv_func_code', action)

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

    def class_layout(self, class_name, include_virtuals=False):
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
            out['identity'] = e.identity()
            return out
        return self.run('fnv_class_layout', action)

    def class_vtable(self, class_name):
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
            return out
        return self.run('fnv_class_vtable', action)

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
        return self.run('fnv_class_tree', action)

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
        return self.run('fnv_string_xrefs', action)

    # ----------------------------------------------------------------------------- crashes
    def crash_latest(self, path=None, max_frames=40):
        from . import fnv_crash

        def action():
            log = fnv_crash.find_latest(self.project, path)
            parsed = fnv_crash.parse_file(log)
            return fnv_crash.summarise(parsed, max_frames)
        return self.run('fnv_crash_latest', action)

    def crash_explain(self, path=None, max_frames=40):
        from . import fnv_crash

        def action():
            log = fnv_crash.find_latest(self.project, path)
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
            # The engine companion's own record of the fault (fnv_crash_context), matched by address and time.
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
        return self.run('fnv_crash_explain', action)


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
        if Path(name).is_absolute():
            return fnv_save.resolve_path(name, Path(name).parent)
        return fnv_save.resolve_path(name, self.saves_dir())

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
    async def fnv_func_find(query: Annotated[str, Field(min_length=1, max_length=300)],
                            mode: Literal['auto', 'name', 'code', 'regex', 'address'] = 'auto',
                            status: list[Status] | None = None, limit: Limit = 25,
                            offset: Offset = 0) -> result_type:
        """Find Fallout: New Vegas 1.4.0.525 engine functions. mode=name matches all words of the query in the
        function's names (CamelCase aware: "limb damage" finds ApplyLimbDamage); mode=code searches the decomp
        source text of each function (case-insensitive), regex likewise with a regular expression; address
        symbolizes; auto picks address, then name, then code. Proven names rank first. Each result gives
        address, name, status (proven / class_proven / probable / decomp / unnamed) and file:line."""
        return await invoke(service.func_find, query, mode, status, limit, offset)

    @server.tool(annotations=observe)
    async def fnv_func_code(function: FunctionRef,
                            max_lines: Annotated[int, Field(ge=10, le=3000)] = 400) -> result_type:
        """Decompiled C++ of one engine function from the decomp checkout, with its signature, proof status and
        call counts. Calls to placeholder functions (FUN_xxxxxxxx) are annotated with their proven names."""
        return await invoke(service.func_code, function, max_lines)

    @server.tool(annotations=observe)
    async def fnv_func_callers(function: FunctionRef, depth: Depth = 1, limit: Limit = 100) -> result_type:
        """Functions that call this one directly (call graph read from the game exe), up to `depth` levels.
        Calls through vtables or function pointers are not visible."""
        return await invoke(service.func_graph, function, 'callers', depth, limit)

    @server.tool(annotations=observe)
    async def fnv_func_callees(function: FunctionRef, depth: Depth = 1, limit: Limit = 100) -> result_type:
        """Functions this one calls directly (and Windows/DLL imports it calls), up to `depth` levels."""
        return await invoke(service.func_graph, function, 'callees', depth, limit)

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
    async def fnv_class_layout(class_name: Annotated[str, Field(min_length=1, max_length=200)],
                               include_virtuals: bool = False) -> result_type:
        """Fields of an engine class (Actor, TESObjectREFR, BaseProcess, ...) with offsets, types and whether
        the decomp build checks each offset; class size and base classes."""
        return await invoke(service.class_layout, class_name, include_virtuals)

    @server.tool(annotations=observe)
    async def fnv_class_vtable(class_name: Annotated[str, Field(min_length=1, max_length=200)]) -> result_type:
        """Virtual table of an engine class: each slot's implementing function (name, proof status) read from
        the game's RTTI, with the declaration from the decomp headers where one exists."""
        return await invoke(service.class_vtable, class_name)

    @server.tool(annotations=observe)
    async def fnv_class_tree(class_name: Annotated[str, Field(min_length=1, max_length=200)],
                             limit: Limit = 200) -> result_type:
        """Base classes and derived classes of an engine class, from the game's RTTI."""
        return await invoke(service.class_tree, class_name, limit)

    @server.tool(annotations=observe)
    async def fnv_command_info(name: Annotated[str, Field(min_length=1, max_length=100)]) -> result_type:
        """A console/script command by name or short name (vanilla, xNVSE, JIP LN...): parameters, return type,
        help text; for vanilla commands the handler functions and what each calls."""
        return await invoke(service.command_info, name)

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

    @server.tool(annotations=observe)
    async def fnv_string_xrefs(text: Annotated[str, Field(min_length=2, max_length=200)],
                               limit: Limit = 25, offset: Offset = 0) -> result_type:
        """Engine functions whose decompiled code contains a string ("Pip-Boy", an INI key, an error text)."""
        return await invoke(service.string_xrefs, text, limit, offset)

    @server.tool(annotations=observe)
    async def fnv_crash_latest(path: Annotated[str, Field(max_length=1000)] | None = None,
                               max_frames: Annotated[int, Field(ge=5, le=400)] = 40) -> result_type:
        """Newest crash log (Yvile's Crash Logger CrashLogger.log or its Crash Logs folder; Cobb's
        CobbCrashLogger.log) parsed: exception, registers, call stack, plugins, modules. Optional path."""
        return await invoke(service.crash_latest, path, max_frames)

    @server.tool(annotations=observe)
    async def fnv_crash_explain(path: Annotated[str, Field(max_length=1000)] | None = None,
                                max_frames: Annotated[int, Field(ge=5, le=400)] = 40) -> result_type:
        """Explain the newest (or given) crash log: each stack frame as a decomp function with proof status
        or as plugin DLL + offset, the forms involved, and the likely culprit with reasons."""
        return await invoke(service.crash_explain, path, max_frames)

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
    fnv_live_tools.register(server, project, (live_read, act, control), result_type)
    return service
