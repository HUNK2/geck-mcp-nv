"""MSVC symbol names -> qualified names and signatures.

On Windows the system's own undecorator (dbghelp UnDecorateSymbolName) is used. Elsewhere a small
fallback recovers the qualified name only.
"""
import ctypes
import re
import sys

UNDNAME_COMPLETE = 0x0000
UNDNAME_NAME_ONLY = 0x1000

_undecorate = None
if sys.platform == 'win32':
    try:
        _undecorate = ctypes.windll.dbghelp.UnDecorateSymbolName
        _undecorate.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32]
        _undecorate.restype = ctypes.c_uint32
    except (AttributeError, OSError):
        _undecorate = None

_C_SYMBOL = re.compile(r'^[_@]?([A-Za-z_][A-Za-z0-9_]*)(?:@\d+)?$')
_SPECIAL = {'0': 'ctor', '1': 'dtor', '_G': "`scalar deleting destructor'", '_E': "`vector deleting destructor'"}


def _dbghelp(symbol, flags):
    buf = ctypes.create_string_buffer(4096)
    n = _undecorate(symbol.encode('latin-1'), buf, len(buf), flags)
    return buf.value.decode('latin-1') if n else None


def _fallback_name(symbol):
    """'?Name@Scope@Outer@@...' -> 'Outer::Scope::Name' (no templates, no signature)."""
    special = None
    body = symbol[1:]
    if body.startswith('?'):
        for code in ('_G', '_E', '0', '1'):
            if body[1:].startswith(code):
                special, body = code, body[1 + len(code):]
                break
        else:
            return None
    head = body.split('@@', 1)[0]
    parts = [p for p in head.split('@') if p]
    if not parts or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', p) for p in parts):
        return None
    if special:
        cls = parts[0]
        name = {'0': cls, '1': '~' + cls}.get(special, _SPECIAL[special])
        return '::'.join(list(reversed(parts)) + [name])
    return '::'.join(reversed(parts))


def demangle(symbol):
    """(qualified_name, signature or None) for one linker symbol."""
    m = _C_SYMBOL.match(symbol)
    if m and not symbol.startswith('?'):
        conv = '__fastcall' if symbol.startswith('@') else '__cdecl' if symbol.startswith('_') else None
        return m.group(1), None if conv is None else '%s %s(...)' % (conv, m.group(1))
    if symbol.startswith('?'):
        if _undecorate:
            name = _dbghelp(symbol, UNDNAME_NAME_ONLY)
            signature = _dbghelp(symbol, UNDNAME_COMPLETE)
            if name:
                return name, signature
        return _fallback_name(symbol) or symbol, None
    return symbol, None


PLACEHOLDER = re.compile(r'^(?:FUN|sub|thunk_FUN)_([0-9a-fA-F]{8})$')
SHELL_SCOPE = re.compile(r'(?:^|_)(?:FUN|fnv)_[0-9a-fA-F]{8}|_shell$|^FnvCpu_|_cls$|_Class$|_class$|^AX$|^SomeClass$')


def clean_name(qualified):
    """Strip the decomp's generated shell scopes; report whether the final name is a placeholder.

    'fnv_004302a0::ExtraDismemberedLimbs::FUN_004302a0' -> ('ExtraDismemberedLimbs::FUN_004302a0', True)
    """
    parts = [p for p in re.split(r'::(?![^<]*>)', qualified) if p]
    while len(parts) > 1 and SHELL_SCOPE.search(parts[0]):
        parts.pop(0)
    leaf = parts[-1] if parts else qualified
    placeholder = bool(PLACEHOLDER.match(leaf.lstrip('~')))
    return '::'.join(parts), placeholder
