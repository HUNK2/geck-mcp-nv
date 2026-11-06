"""Console/script command tables: vanilla (read from your FalloutNV.exe) and script-extender plugins
(read from their source trees, e.g. xNVSE and JIP LN NVSE).

Vanilla CommandInfo layout and table addresses for 1.4.0.525 follow xNVSE's CommandTable.h and
CommandTable.cpp (xNVSE team; see THIRD_PARTY_NOTICES.md).
"""
import re
import struct
from pathlib import Path

RECORD = 0x28
# (table, first record, last record, record count, first opcode)
VANILLA_TABLES = [
    ('console', 0x0118E8E0, 0x011908C0, None, 0x100),
    ('script', 0x01190910, 0x01196D10, None, 0x1000),
]


def vanilla(image, param_names=None):
    """Commands from the game's own CommandInfo tables."""
    out = []
    for table, first, last, count, opcode0 in VANILLA_TABLES:
        n = count or (last - first) // RECORD + 1
        for i in range(n):
            va = first + i * RECORD
            raw = image.read(va, RECORD)
            if len(raw) < RECORD:
                break
            long_p, short_p, opcode, help_p, needs_parent, nparams, params_p, ex, pa, ev, flags = \
                struct.unpack('<IIIIHHIIIII', raw)
            name = image.cstr(long_p, 80)
            if opcode != opcode0 + i or not name:
                continue
            params = []
            if params_p and nparams < 64:
                for k in range(nparams):
                    p_str, p_type, p_opt = struct.unpack('<III', image.read(params_p + 12 * k, 12))
                    params.append({'label': image.cstr(p_str, 80), 'typeId': p_type,
                                   'type': (param_names or {}).get(p_type), 'optional': bool(p_opt)})
            handlers = {role: '0x%08X' % h for role, h in (('execute', ex), ('parse', pa), ('eval', ev))
                        if h and image.is_code(h)}
            out.append({'name': name, 'short': image.cstr(short_p, 80), 'opcode': '0x%04X' % opcode,
                        'source': 'vanilla', 'table': table, 'help': image.cstr(help_p, 400),
                        'needsParent': bool(needs_parent), 'params': params, 'handlers': handlers,
                        'record': '0x%08X' % va, 'flags': flags})
    return out


def param_types(nvse_root):
    """ParamType enum values -> names, from xNVSE's CommandTable.h."""
    path = Path(nvse_root) / 'nvse' / 'nvse' / 'CommandTable.h'
    if not path.exists():
        return {}
    text = path.read_text(encoding='utf-8', errors='replace')
    m = re.search(r'enum\s+ParamType\s*\{(.*?)\};', text, re.S)
    out = {}
    if m:
        for name, value in re.findall(r'kParamType_(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)', m.group(1)):
            out.setdefault(int(value, 0), name)
    return out


def _split_args(text):
    args, depth, cur = [], 0, ''
    for c in text:
        if c in '(<[{':
            depth += 1
        elif c in ')>]}':
            depth -= 1
        if c == ',' and depth == 0:
            args.append(cur.strip())
            cur = ''
        else:
            cur += c
    if cur.strip():
        args.append(cur.strip())
    return args


def _strip_comments(text):
    text = re.sub(r'/\*.*?\*/', lambda m: '\n' * m.group(0).count('\n'), text, flags=re.S)
    return re.sub(r'//[^\n]*', '', text)


def plugin(root, label, param_names=None):
    """Commands a script-extender source tree defines (DEFINE_COMMAND* / DEFINE_CMD* macros)."""
    root = Path(root)
    files = [p for p in root.rglob('*') if p.suffix in ('.h', '.cpp', '.hpp', '.inl') and p.is_file()]
    texts, raw_texts = {}, {}
    for p in files:
        try:
            raw_texts[p] = p.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        texts[p] = _strip_comments(raw_texts[p])
    macros = {}
    for text in texts.values():
        for name, params in re.findall(r'#\s*define\s+(DEFINE_(?:COMMAND|CMD)\w*)\(([^)]*)\)', text):
            macros.setdefault(name, [p.strip() for p in params.split(',')])
    arrays = {}
    for text in texts.values():
        for name, body in re.findall(r'ParamInfo\s+(\w+)\s*\[[^\]]*\]\s*=\s*\{(.*?)\}\s*;', text, re.S):
            items = re.findall(r'\{\s*"([^"]*)"\s*,\s*(\w+)\s*,\s*(\w+)\s*\}', body)
            arrays[name] = [{'label': s, 'type': t.replace('kParamType_', ''),
                             'optional': o not in ('0', 'false')} for s, t, o in items]
    returns, opcodes = {}, {}
    for text in raw_texts.values():   # registration lines keep their /*opcode*/ comments
        for m in re.finditer(r'(?:/\*\s*(?:0x)?([0-9A-Fa-f]{3,4})\s*\*/\s*)?REG_(?:TYPED_)?CMD(?:_(\w+?))?\(\s*(\w+)', text):
            kind = {'FRM': 'form', 'STR': 'string', 'ARR': 'array', 'AMB': 'ambiguous'}.get(m.group(2) or '', 'number')
            returns.setdefault(m.group(3), kind)
            if m.group(1):
                opcodes.setdefault(m.group(3), '0x' + m.group(1).upper())
        for name, kind in re.findall(r'ADD_CMD_RET\(\s*(\w+)\s*,\s*kRetnType_(\w+)\s*\)', text):
            returns.setdefault(name, kind.lower())
        for name in re.findall(r'\bADD_CMD\(\s*(\w+)\s*\)', text):
            returns.setdefault(name, 'number')
    out, seen = [], set()
    call = re.compile(r'\b(DEFINE_(?:COMMAND|CMD)\w*)\s*\(([^;]*?)\)\s*;', re.S)
    for path, text in texts.items():
        for m in call.finditer(text):
            params = macros.get(m.group(1))
            if not params:
                continue
            args = _split_args(m.group(2))
            if len(args) < len(params) and 'description' not in params:
                continue
            values = {}
            if len(args) > len(params) and 'description' in params:
                d = params.index('description')
                extra = len(args) - len(params)
                args = args[:d] + [', '.join(args[d:d + extra + 1])] + args[d + extra + 1:]
            for key, value in zip(params, args):
                values[key] = value
            name = values.get('name')
            if not name or not re.fullmatch(r'\w+', name) or name in seen:
                continue
            seen.add(name)
            info = values.get('paramInfo', 'NULL')
            params_out = arrays.get(info, [] if info in ('NULL', 'nullptr', '0') else None)
            if params_out and param_names:
                for p in params_out:
                    p.setdefault('typeId', next((k for k, v in param_names.items() if v == p['type']), None))
            out.append({'name': name, 'short': values.get('altName', ''), 'opcode': opcodes.get(name),
                        'source': label, 'help': values.get('description', '').strip('"'),
                        'needsParent': values.get('refRequired', '0') not in ('0', 'false'),
                        'params': params_out if params_out is not None else [],
                        'paramsUnresolved': info if params_out is None else None,
                        'returns': returns.get(name),
                        'handler': 'Cmd_%s_Execute' % name,
                        'definedIn': '%s:%d' % (path.relative_to(root).as_posix(), text.count('\n', 0, m.start()) + 1)})
    return out
