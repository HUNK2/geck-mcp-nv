"""Find each function's definition in the decomp source from its '// @fnv 0x...' marker."""
import re
from pathlib import Path

MARKER = re.compile(r'^\s*//\s*@fnv\s+0x([0-9A-Fa-f]{8})\b', re.M)
SOURCE_SUFFIXES = ('.cpp', '.c', '.h', '.hpp', '.inl', '.cxx')


def read_lines(path):
    return Path(path).read_text(encoding='utf-8', errors='replace').split('\n')


def markers(lines):
    """{address: [0-based line index of each marker]} for one file."""
    out = {}
    for i, line in enumerate(lines):
        m = MARKER.match(line)
        if m:
            out.setdefault(int(m.group(1), 16), []).append(i)
    return out


def _is_definition(lines, i):
    """An unindented definition header: not a call site, not a declaration, a '{' within 6 lines."""
    line = lines[i] if 0 <= i < len(lines) else ''
    if line[:1] in ('', ' ', '\t', '/', '#', '}', '{') or '(' not in line or line.rstrip().endswith(';'):
        return False
    if re.match(r'^(class|struct|union|enum|namespace|typedef|template\s*<[^>]*>\s*(class|struct))\b', line):
        return False
    return any('{' in x for x in lines[i:i + 6])


def definition_start(lines, marker_index, address, short_name=None):
    """Line index where the function's definition begins, searching below its marker."""
    hexname = '%08x' % address
    first = None
    for j in range(marker_index + 1, min(len(lines), marker_index + 300)):
        if j > marker_index + 1 and MARKER.match(lines[j]):
            break
        if _is_definition(lines, j):
            low = lines[j].lower()
            if hexname in low or (short_name and re.search(r'\b%s\s*\(' % re.escape(short_name), lines[j])):
                return j
            if first is None:
                first = j
    return first


def definition_end(lines, start):
    """Line index of the closing brace of the definition starting at `start`."""
    depth, seen = 0, False
    in_block = False
    for i in range(start, min(len(lines), start + 20000)):
        line = lines[i]
        k, n = 0, len(line)
        while k < n:
            c = line[k]
            if in_block:
                end = line.find('*/', k)
                if end < 0:
                    k = n
                    continue
                in_block, k = False, end + 2
                continue
            if c == '/' and line.startswith('//', k):
                break
            if c == '/' and line.startswith('/*', k):
                in_block, k = True, k + 2
                continue
            if c in '"\'':
                j = k + 1
                while j < n and line[j] != c:
                    j += 2 if line[j] == '\\' else 1
                k = j + 1
                continue
            if c == '{':
                depth += 1
                seen = True
            elif c == '}':
                depth -= 1
                if seen and depth == 0:
                    return i
            k += 1
        if not seen and line.rstrip().endswith(';') and i > start:
            return None   # a declaration, not a definition
    return None


def locate(lines, file_markers, address, short_name=None):
    """(marker line, start line, end line) as 1-based numbers, or None."""
    for marker_index in file_markers.get(address, []):
        start = definition_start(lines, marker_index, address, short_name)
        if start is None:
            continue
        end = definition_end(lines, start)
        if end is not None:
            return marker_index + 1, start + 1, end + 1
    return None
