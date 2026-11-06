"""Class layouts from the decomp's C++ headers.

Reads class/struct definitions with their members, '// +0xNN' offset comments, '// slot 0xN' virtual
slots, and the FNV_STATIC_ASSERT(sizeof / offsetof) checks the decomp build compiles. An offset backed
by a static assert is reported as checked: the decomp does not build if it is wrong.
"""
import re
from pathlib import Path

HEAD = re.compile(r'^[ \t]*(?:template\s*<[^>]*>\s*)?(class|struct|union)\s+(?:FNV_ALIGN\(\s*\d+\s*\)\s+)?'
                  r'(?P<name>[A-Za-z_][\w:]*(?:<[^{};]*>)?)\s*(?:final\s*)?(?::(?P<bases>[^{};]*))?\{',
                  re.M | re.S)
SIZE_ASSERT = re.compile(r'FNV_STATIC_ASSERT\(\s*sizeof\(\s*([^)]+?)\s*\)\s*==\s*(0x[0-9A-Fa-f]+|\d+)\s*\)')
OFFSET_ASSERT = re.compile(r'FNV_STATIC_ASSERT\(\s*offsetof\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)\s*==\s*(0x[0-9A-Fa-f]+|\d+)\s*\)')
OFFSET_COMMENT = re.compile(r'//\s*\+\s*(0x[0-9A-Fa-f]+)')
SLOT_COMMENT = re.compile(r'//\s*slot\s+(0x[0-9A-Fa-f]+|\d+)')
MEMBER = re.compile(r'^(?P<type>[A-Za-z_][\w:<>,* &]*?[\w>*&])\s*(?P<ptr>[*&]*)\s*(?P<name>[A-Za-z_]\w*)\s*'
                    r'(?P<array>(?:\[[^\]]*\])*)\s*(?::\s*(?P<bits>\d+))?\s*;')
ACCESS = re.compile(r'^\s*(public|private|protected)\s*:\s*')
SKIP_TYPES = ('typedef', 'using', 'friend', 'static', 'enum', 'return')


def _num(text):
    return int(text, 16) if text.lower().startswith('0x') else int(text)


def _strip_strings(line):
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', line)


def _block_end(text, start):
    """Index just past the '}' matching the '{' at text[start]."""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == '/' and text.startswith('//', i):
            i = text.find('\n', i)
            if i < 0:
                return n
            continue
        if c == '/' and text.startswith('/*', i):
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in '"\'':
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == '\\' else 1
            i = j + 1
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _bases(text):
    if not text:
        return []
    out = []
    for part in re.split(r',(?![^<]*>)', text):
        part = re.sub(r'\b(public|private|protected|virtual)\b', '', part).strip()
        if part:
            out.append(part)
    return out


def _members(body, first_line):
    """Members and virtual slots of one class body (top level and anonymous unions/structs)."""
    fields, virtuals = [], []
    depth = 0
    pending = ''
    for index, raw in enumerate(body.split('\n')):
        line_no = first_line + index
        line = _strip_strings(raw)
        code, _, comment = line.partition('//')
        comment = '//' + comment if comment else ''
        stripped = ACCESS.sub('', code).strip()
        opens, closes = code.count('{'), code.count('}')
        nested_named = re.match(r'^(class|struct|union|enum)\s+[A-Za-z_]', stripped) and opens
        anonymous = re.match(r'^(union|struct)\s*\{', stripped)
        if depth == 0 and stripped and not nested_named and not anonymous:
            if stripped.startswith('virtual') or ('(' in stripped and stripped.endswith(';')):
                slot = SLOT_COMMENT.search(comment)
                if stripped.startswith('virtual'):
                    virtuals.append({'slot': _num(slot.group(1)) if slot else None,
                                     'declaration': re.sub(r'\s+', ' ', stripped.rstrip(';').strip()),
                                     'line': line_no})
            elif stripped.endswith(';') or pending:
                text = (pending + ' ' + stripped).strip() if pending else stripped
                if not text.endswith(';'):
                    pending = text
                    continue
                pending = ''
                if not text.split()[0] in SKIP_TYPES and '(' not in text and 'operator' not in text:
                    m = MEMBER.match(text)
                    if m:
                        offset = OFFSET_COMMENT.search(comment)
                        fields.append({'name': m.group('name'),
                                       'type': (m.group('type') + m.group('ptr')).strip() + (m.group('array') or ''),
                                       'offset': _num(offset.group(1)) if offset else None,
                                       'bits': int(m.group('bits')) if m.group('bits') else None,
                                       'line': line_no})
        depth += opens - closes
        if anonymous and depth > 0:
            depth -= 1   # members of an anonymous union/struct belong to the class
        if depth < 0:
            depth = 0
    return fields, virtuals


def parse_file(path, rel):
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    sizes = {_norm(n): _num(v) for n, v in SIZE_ASSERT.findall(text)}
    offsets = {}
    for cls, member, value in OFFSET_ASSERT.findall(text):
        offsets.setdefault(_norm(cls), {})[member.strip()] = _num(value)
    classes = []
    for m in HEAD.finditer(text):
        open_brace = m.end() - 1
        end = _block_end(text, open_brace)
        name = _norm(m.group('name'))
        first_line = text.count('\n', 0, open_brace) + 1
        body = text[open_brace + 1:end - 1]
        fields, virtuals = _members(body, first_line)
        checked = offsets.get(name, {})
        for f in fields:
            if f['name'] in checked:
                f['checked'] = True
                f['offset'] = checked[f['name']]
        if not fields and not virtuals and name not in sizes:
            continue
        classes.append({'name': name, 'kind': m.group(1), 'bases': _bases(m.group('bases')),
                        'size': sizes.get(name), 'sizeChecked': name in sizes,
                        'fields': fields, 'virtuals': virtuals, 'file': rel, 'line': first_line})
    return classes


def _norm(name):
    return re.sub(r'\s+', '', name)


def _score(c, preferred):
    return (preferred, c['sizeChecked'], sum(1 for f in c['fields'] if f.get('checked')),
            sum(1 for f in c['fields'] if f['offset'] is not None), len(c['fields']))


def collect(decomp_root, log=print):
    """{class name: layout} from include/ (shared headers) and src/ (local copies with offsets)."""
    root = Path(decomp_root)
    found = {}
    sources = [('header', p) for p in sorted((root / 'include').rglob('*.h'))]
    sources += [('local', p) for p in sorted((root / 'src').rglob('*')) if p.suffix in ('.h', '.hpp', '.cpp', '.inl')]
    for origin, path in sources:
        rel = path.relative_to(root).as_posix()
        try:
            classes = parse_file(path, rel)
        except (OSError, UnicodeError) as e:
            log('skip %s: %s' % (rel, e))
            continue
        for c in classes:
            if origin == 'local' and not (c['sizeChecked'] or any(f['offset'] is not None for f in c['fields'])):
                continue
            c['origin'] = origin
            found.setdefault(c['name'], []).append(c)
    layouts = {}
    for name, candidates in found.items():
        stem = re.sub(r'[^A-Za-z0-9]', '_', name)
        best = max(candidates, key=lambda c: _score(c, c['origin'] == 'header' and Path(c['file']).stem == stem))
        layout = dict(best)
        others = [c['file'] + ':' + str(c['line']) for c in candidates if c is not best]
        if others:
            layout['otherDefinitions'] = others[:20]
            layout['otherDefinitionCount'] = len(others)
        _spans(layout)
        layouts[name] = layout
    return layouts


PRIMITIVE = {'char': 1, 'signed char': 1, 'unsigned char': 1, 'bool': 1, 'UInt8': 1, 'SInt8': 1, 'uint8_t': 1,
             'short': 2, 'unsigned short': 2, 'wchar_t': 2, 'UInt16': 2, 'SInt16': 2, 'uint16_t': 2,
             'int': 4, 'unsigned int': 4, 'long': 4, 'unsigned long': 4, 'float': 4, 'UInt32': 4, 'SInt32': 4,
             'uint32_t': 4, 'int32_t': 4, 'DWORD': 4,
             'double': 8, '__int64': 8, 'unsigned __int64': 8, 'FNVInt64': 8, 'FNVUInt64': 8, 'UInt64': 8}


def type_size(text):
    """Bytes of a member type when it is a primitive or a pointer (with array extents), else None."""
    m = re.match(r'^(.*?)((?:\[[^\]]*\])*)$', text.strip())
    base, arrays = m.group(1).strip(), m.group(2)
    if base.endswith('*') or base.endswith('&'):
        size = 4
    else:
        size = PRIMITIVE.get(re.sub(r'^(const|volatile)\s+', '', base))
    if size is None:
        return None
    for extent in re.findall(r'\[([^\]]*)\]', arrays):
        try:
            size *= int(extent, 0)
        except ValueError:
            return None
    return size


def _infer_offsets(layout):
    """Give a field without an offset comment the end of the previous field when that one's size is known."""
    prev = None
    for f in layout['fields']:
        if f.get('bits'):
            prev = None
            continue
        size = type_size(f['type'])
        if f['offset'] is None and prev is not None:
            p_off, p_size = prev
            align = min(4, size or 4)
            if p_size is not None and size is not None:
                f['offset'] = (p_off + p_size + align - 1) // align * align
                f['inferred'] = True
        prev = (f['offset'], size) if f['offset'] is not None else None


def _spans(layout):
    """Bytes up to the next known offset (or the checked class size): a size bound, not a type size."""
    _infer_offsets(layout)
    known = sorted((f['offset'], i) for i, f in enumerate(layout['fields']) if f['offset'] is not None)
    for k, (offset, i) in enumerate(known):
        nxt = known[k + 1][0] if k + 1 < len(known) else layout.get('size')
        if nxt is not None and nxt > offset:
            layout['fields'][i]['span'] = nxt - offset
